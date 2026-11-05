"""Расчёт KPI по правилам из kpi_rules.json на нормализованных данных SQLite."""
import datetime as dt
import re
import math
import json

from .common import (business_days_between, month_add, month_bounds, parse_date,
                     today, weeks_of_month, working_days)
from .call_analysis import secretary_marks
from .employees import ABSENCE_KINDS
from .onec_receivables import state_at
from .crm_links import record_url, employee_url

SRC_B24, SRC_1C, SRC_MAN = "Битрикс24", "1С", "Оценка руководителя"
SRC_INPUT = "1С + ввод руководителя"  # данные из 1С, дополненные вручную в дашборде (app/manual.py)
SRC_HAND = "Ввод руководителя"  # записи, которые руководитель вносит в дашборде (штрафы)
REJECTED = "Отклонён"  # статус тендера 1С для KPI «Операционная дисциплина и качество подачи»
CRITERIA_TITLES = {"docs": "Документооборот", "tasks": "Задачи в Битрикс24", "quality": "Качество документации"}
EPS = 1e-9


def payout_fraction(pct):
    """Use the whole percentage shown in the UI, capped at the item's full value."""
    return min(100, max(0, math.floor(pct * 100 + 0.5))) / 100


def _in(d, first, last):
    d = parse_date(d)
    return d is not None and first <= d <= last


ORG_FORMS = re.compile(r"(?<![\w-])(ооо|оао|зао|пао|нао|ао|као|мкп|муп|гуп|фгуп|фгбу|ип|филиал|ф-л|корпорация)(?![\w-])")


def org_key(name):
    """Название компании для сравнения: без формы собственности, кавычек и знаков, «ООО "Кама"» = «КАМА»."""
    s = str(name or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„.,()]", " ", s)
    s = ORG_FORMS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_kp_letters(kps, letters, days):
    """Пары (КП из 1С, письмо реестра) об одном и том же КП: та же компания, даты не дальше days дней.
    Каждое КП и каждое письмо — не больше чем в одной паре, ближайшие по дате. {id КП: письмо}."""
    used, out = set(), {}
    for k in sorted(kps, key=lambda r: r["dt"]):
        kk, kd = org_key(k.get("client")), parse_date(k["dt"])
        if len(kk) < 3:
            continue
        best = None
        for i, l in enumerate(letters):
            lk = org_key(l.get("deal"))
            # та же компания: названия совпадают или одно целиком, по словам, входит в другое («Кама» ≠ «Камаз»)
            if i in used or len(lk) < 3 or not (f" {kk} " in f" {lk} " or f" {lk} " in f" {kk} "):
                continue
            gap = abs((parse_date(l["dt"]) - kd).days)
            if gap <= days and (best is None or gap < best[0]):
                best = (gap, i)
        if best:
            used.add(best[1])
            out[k["id"]] = letters[best[1]]
    return out


def comp(name, fact, target, unit="", op=">=", source=None, informative=False, pace=None, fmt=None, audit=None):
    """Компонент показателя: факт против нормы. pct — степень выполнения (1.0 = норма выполнена)."""
    pct = None
    if fact is not None and target is not None:
        if op == ">=":
            pct = 1.0 if target <= 0 else fact / target
        else:  # "<=" — чем меньше, тем лучше
            if fact <= target + EPS:
                pct = 1.0
            elif target <= 0:
                pct = 0.0
            else:
                pct = target / fact
    return {"name": name, "fact": fact, "target": target, "unit": unit, "op": op, "pct": pct,
            "source": source, "informative": informative, "pace": pace, "fmt": fmt,
            **({"audit": audit} if audit else {})}


class Engine:
    def __init__(self, con, rules, config):
        self.con = con
        self.rules = rules
        self.config = config
        self.holidays = config.get("holidays", [])
        self.workdays = config.get("workdays", [])
        self.min_call = config.get("bitrix24", {}).get("min_call_seconds", 120)

    # ------------------------------------------------------------------ helpers
    def q(self, sql, *args):
        return [dict(r) for r in self.con.execute(sql, args)]

    def employees(self):
        # открытые сотрудники с KPI; без признака visible (старый config.json) — все, кроме руководителей
        return [e for e in self.config["employees"] if e.get("position") and e.get("visible", e.get("role") != "manager")]

    def employee(self, login):
        for e in self.config["employees"]:
            if e["login"] == login:
                return e
        return None

    def _ctx(self, month):
        first, last = month_bounds(month)
        t = today()
        asof = min(last, t)
        wd_total = working_days(first, last, self.holidays, self.workdays)
        wd_elapsed = working_days(first, asof, self.holidays, self.workdays) if asof >= first else 0
        in_progress = first <= t <= last
        return {"month": month, "first": first, "last": last, "asof": asof, "wd": wd_total,
                "wd_elapsed": wd_elapsed, "in_progress": in_progress, "future": first > t}

    def _absent_dates(self, e, ctx, excused=True):
        """Рабочие дни месяца, попавшие в периоды отсутствия сотрудника (absences: [{"from", "to", "kind", "reason"}]).
        excused=True — уважительные (всё, кроме прогула), False — прогулы. Возвращает {дата: период}."""
        out = {}
        for a in e.get("absences") or []:
            if (a.get("kind") != "truancy") != excused:
                continue
            d = max(ctx["first"], dt.date.fromisoformat(a["from"]))
            end = min(ctx["last"], dt.date.fromisoformat(a["to"])) if a.get("to") else ctx["last"]
            while d <= end:
                if working_days(d, d, self.holidays, self.workdays):
                    out.setdefault(d.isoformat(), a)
                d += dt.timedelta(days=1)
        return out

    def _absence(self, e, ctx):
        """Отсутствия в месяце для показа: уважительные (освобождают от норм) и прогулы (не освобождают)."""
        def periods(dates):
            seen, notes = [], []
            for a in dates.values():
                if any(a is x for x in seen):
                    continue
                seen.append(a)
                f = max(ctx["first"], dt.date.fromisoformat(a["from"]))
                t = min(ctx["last"], dt.date.fromisoformat(a["to"])) if a.get("to") else ctx["last"]
                label = ABSENCE_KINDS.get(a.get("kind"), "Отсутствие")
                notes.append(f"{label} {f:%d.%m}–{t:%d.%m}" + (f" ({a['reason']})" if a.get("reason") else ""))
            return notes
        excused, truancy = self._absent_dates(e, ctx), self._absent_dates(e, ctx, excused=False)
        return {"days": min(len(excused), ctx["wd"]), "periods": periods(excused),
                "truancy_days": len(truancy), "truancy": periods(truancy)}

    def _pace(self, ctx, target):
        if not ctx["in_progress"] or target is None or not ctx["wd"]:
            return None
        return round(target * ctx["wd_elapsed"] / ctx["wd"], 1)

    def _work_dates(self, e, ctx, upto=None):
        """Рабочие дни месяца (по upto включительно), кроме дней уважительного отсутствия сотрудника."""
        away = self._absent_dates(e, ctx)  # прогул от нормы не освобождает
        out, d = [], ctx["first"]
        while d <= (upto or ctx["last"]):
            if working_days(d, d, self.holidays, self.workdays) and d.isoformat() not in away:
                out.append(d.isoformat())
            d += dt.timedelta(days=1)
        return out

    def _kp_with_letters(self, emp, ctx):
        """С месяца kp_with_letters_from (правила KPI): КП = КП из 1С + письма из реестра исходящих (темы
        letters_sheet.topics), письмо той же компании в пределах kp_letter_match_days от КП — то же КП, считается раз.
        Возвращает (число, extra, строки для «Как считается») или None, если месяц раньше."""
        since = self.rules.get("kp_with_letters_from")
        if not since or ctx["month"] < since:
            return None
        kps, letters, pairs = self._kp_inputs(emp, ctx)
        twin = {id(l) for l in pairs.values()}
        n = len(kps) + len(letters) - len(pairs)
        extra = [{"label": "КП в 1С", "value": len(kps)}, {"label": "Писем в реестре исходящих", "value": len(letters)},
                 {"label": "Совпали — учтены один раз", "value": len(pairs),
                  "hint": f"та же компания, даты не дальше {self.rules.get('kp_letter_match_days', 7)} дн."}]
        det = [[k["dt"], "КП в 1С " + (k.get("doc") or ""), k.get("client") or "—",
                f"то же, что письмо от {pairs[k['id']]['dt'][:10]}" if k["id"] in pairs else ""] for k in kps]
        det += [[l["dt"][:10], "Письмо: " + (l.get("subject") or ""), l.get("deal") or "—",
                 "совпало с КП в 1С — не считается второй раз" if id(l) in twin else ""] for l in letters]
        det.sort(key=lambda r: r[0])
        return n, extra, det

    def _kp_inputs(self, emp, ctx):
        first, last = ctx["first"].isoformat(), ctx["last"].isoformat()
        kps = self.q("SELECT * FROM kp WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt,id", emp, first, last)
        letters = self.q("SELECT * FROM kp_letters WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt,id", emp, first, last + "T23:59:59")
        return kps, letters, match_kp_letters(kps, letters, self.rules.get("kp_letter_match_days", 7))

    def _mark_secretary(self, calls):
        """С месяца secretary_by_call_analysis_from (правила KPI): звонок секретарю — по должности контакта в Б24
        или по разбору Codex; отметка руководителя к звонку важнее обоих. В r["secretary_src"] — откуда отметка."""
        since = self.rules.get("secretary_by_call_analysis_from")
        if not since:
            return
        recent = [r for r in calls if r["dt"][:7] >= since]
        marks = secretary_marks(self.con, [r["id"] for r in recent])
        for r in recent:
            m = marks.get(r["id"]) or {}
            if m.get("manual") is not None:
                r["secretary"], r["secretary_src"] = m["manual"], "руководитель"
            elif r["secretary"]:
                r["secretary_src"] = "должность в Битрикс24"
            elif m.get("ai"):
                r["secretary"], r["secretary_src"] = 1, "разбор Codex"

    def _result_calls(self, emp, ctx, require_lpr=True, one_per_contact_day=False):
        """Результативные звонки. require_lpr — нужна информация о ЛПР (тендерные регламенты);
        у ПМ регламент требует только ответ на квалифицирующий вопрос и следующий шаг.
        Возвращает (результативные звонки, воронку отбора, звонки от min_call не секретарю)."""
        rows = self.q("SELECT * FROM activities WHERE emp=? AND kind='call' AND dt>=? AND dt<=? ORDER BY dt,id",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        def uniq(rs):  # не больше одного звонка в день на один контакт
            if not one_per_contact_day:
                return rs
            seen, out_ = set(), []
            for r in rs:
                key = (r["dt"][:10], r.get("contact") or r["id"])
                if key not in seen:
                    seen.add(key)
                    out_.append(r)
            return out_
        out = [r for r in rows if r["direction"] == "out"]
        long_ = [r for r in out if (r["duration"] or 0) >= self.min_call]
        self._mark_secretary(long_)
        not_secr = [r for r in long_ if not r["secretary"]]
        lpr = [r for r in not_secr if r["is_lpr"]] if require_lpr else not_secr
        result = [r for r in lpr if r["next_step"]]
        out, long_, not_secr, lpr, result = map(uniq, (out, long_, not_secr, lpr, result))
        funnel = [("Исходящие звонки", len(out)), (f"От {self.min_call // 60} мин", len(long_)),
                  ("Не секретарь", len(not_secr))]
        if require_lpr:
            funnel.append(("Есть информация о ЛПР", len(lpr)))
        funnel.append(("Зафиксирован след. шаг", len(result)))
        return result, funnel, not_secr

    def _meetings(self, emp, ctx, need_protocol=False, same_day_close=False):
        rows = self.q("SELECT * FROM activities WHERE emp=? AND kind='meeting' AND status='done' AND dt>=? AND dt<=?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        selected = [r for r in rows if r["has_protocol"] and r["has_summary"]] if need_protocol else rows
        if same_day_close:
            selected = [r for r in selected if not r["closed_dt"] or r["closed_dt"][:10] <= r["dt"][:10]]
        return selected, rows

    def activity_audit(self, login, month, code, kind):
        """Only active, explicitly exposed component details may be loaded."""
        from .kpi_activity_audit import build
        employee = self.employee(login)
        position = self.rules["positions"].get((employee or {}).get("position"), {})
        item = next((i for i in self.items_for(position, month) if i["code"] == code), None) if position else None
        if position.get("draft") or not item or code not in ("base_development", "operational_activity"):
            raise ValueError("Расшифровка этого показателя недоступна")
        ctx = self._ctx(month)
        if ctx["wd"] and self._absence(employee, ctx)["days"] >= ctx["wd"]:
            raise ValueError("В этом месяце сотрудник не работал, KPI не рассчитывается")
        result = getattr(self, "calc_" + code)(employee, ctx, item.get("params", {}))
        component = next((c for c in result["components"] if c.get("audit") == kind), None)
        if not component:
            raise ValueError("Расшифровка этой части KPI недоступна")
        return build(self, employee, ctx, item, component, kind)

    def _manual(self, emp, month, criterion):
        weeks = weeks_of_month(month)
        rows = self.q(f"SELECT * FROM manual_scores WHERE emp=? AND criterion=? AND week IN ({','.join('?' * len(weeks))})",
                      emp, criterion, *weeks)
        by_week = {r["week"]: r for r in rows}
        vals = [r["score"] for r in rows if r["score"] is not None]
        avg = sum(vals) / len(vals) if vals else None
        return avg, [{"week": w, "score": by_week.get(w, {}).get("score"), "comment": by_week.get(w, {}).get("comment")}
                     for w in weeks]

    def _default_plan(self, p):
        """План по умолчанию: доля общего плана отдела (sales_team_plan) или default_plan пункта."""
        team = self.rules.get("sales_team_plan")
        if p.get("team_plan") and team:
            n = len([x for x in self.employees() if x.get("position") in team["positions"]])
            return round(team["monthly_total"] / n) if n else None
        return p.get("default_plan")

    # ------------------------------------------------------------------ calculators
    def _missing_datasets(self, ctx, required):
        state = {r["dataset"]: r for r in self.q("SELECT * FROM dataset_state")}
        first, asof = ctx["first"].isoformat(), min(ctx["last"], today()).isoformat()
        return [name for name in required if name not in state or
                not state[name]["date_from"] <= first <= asof <= state[name]["date_to"]]

    def _order_rows(self, emp, ctx, role):
        raw = self.q("SELECT * FROM conversion_orders WHERE emp=? AND role=? AND dt>=? AND dt<=? "
                     "AND status='ТТК_ВЗакуп' ORDER BY dt,doc", emp, role,
                     ctx["first"].isoformat(), ctx["last"].isoformat())
        return list({r["order_key"] or r["id"]: r for r in raw}.values())

    def _order_conversion(self, e, ctx, p, role):
        """Orders use their own date; denominator documents independently use their own period."""
        emp, first, last = e["login"], ctx["first"].isoformat(), ctx["last"].isoformat()
        required = ("conversion_orders", "supply_funnel") if role == "supply" else ("conversion_orders", "kp", "tenders_1c")
        missing = self._missing_datasets(ctx, required)
        known = not missing
        orders = self._order_rows(emp, ctx, role)
        kps, tenders, requests = [], [], []
        if role == "supply":
            requests = self.q("SELECT * FROM supply_funnel WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt,doc", emp, first, last)
        else:
            kps = self.q("SELECT * FROM kp WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt,doc", emp, first, last)
            tenders = self.q("SELECT * FROM tenders_1c WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt,doc", emp, first, last)
        den = len(requests) if role == "supply" else len(kps) + len(tenders)
        value = len(orders) / den if known and den else None
        target, rule = p.get("target"), None
        by_base = "growth_vs_last_year" in p
        if by_base:
            base = self.q("SELECT value FROM baselines WHERE emp=? AND metric='conversion_last_year'", emp)
            if base and base[0]["value"]:
                growth = p["growth_vs_last_year"]
                target = base[0]["value"] * (1 + growth)
                rule = f"Базовый уровень прошлого года {base[0]['value'] * 100:.1f}% + {growth * 100:.0f}%"
            else:
                rule = "Базовый уровень прошлого года не задан — используется целевая конверсия из регламента"
        denominator = "все заявки в ОС" if role == "supply" else "(КП + тендеры)"
        extra = [{"label": "Заказы клиента «В закуп»", "value": len(orders), "hint": "По дате заказа; каждый заказ один раз"}]
        extra += ([{"label": "Все заявки в ОС", "value": len(requests)}] if role == "supply" else [
            {"label": "КП за период", "value": len(kps)}, {"label": "Тендеры за период", "value": len(tenders)},
            {"label": "Всего КП + тендеров", "value": den}])
        if rule:
            extra.append({"label": "Как задана цель", "value": rule})
        details = [[r["doc"], "Заказ клиента", r["client"], r["dt"], r["status"],
                    "Числитель" + (": " + r["request_docs"] if r.get("request_docs") else "")] for r in orders]
        for kind, rows in (("КП", kps), ("Тендер", tenders), ("Заявка в ОС", requests)):
            details += [[r.get("doc"), kind, r.get("client"), r["dt"], r.get("status") or r.get("stage") or "", "Знаменатель"]
                        for r in rows]
        warnings = []
        if not known:
            labels = {"conversion_orders": "заказы клиента", "kp": "КП", "tenders_1c": "тендеры", "supply_funnel": "заявки в ОС"}
            warnings.append("Для конверсии нет полной выгрузки за выбранный период: " +
                            ", ".join(labels[name] for name in missing) + ". Обновите данные.")
        elif not den:
            warnings.append(f"Конверсия не определена: за выбранный период отсутствуют {denominator}.")
        return {"components": [comp(f"Конверсия: заказы «В закуп» / {denominator}", value, target, "%", source=SRC_1C)],
                "extra": extra, "baseline": by_base, "warnings": warnings,
                "details": {"columns": ["Документ", "Вид", "Клиент", "Дата", "Статус", "Участие в расчёте"], "rows": details}}

    def calc_conversion_tender(self, e, ctx, p):
        return self._order_conversion(e, ctx, p, "sales")

    def call_funnel(self, login, date_from, date_to):
        """Воронка отбора результативных звонков сотрудника за произвольный период (вкладка «Воронки») —
        по правилам его KPI: тот же отбор, что в пункте KPI со звонками. Если звонков в KPI нет — для справки:
        исходящие → от min_call → не секретарь → следующий шаг. None — сотрудника нет в дашборде."""
        e = self.employee(login)
        if not e:
            return None
        item = None
        for it in self.rules["positions"].get(e.get("position"), {}).get("items", []):
            p = it.get("params", {})
            if it["code"] == "operational_activity" or \
                    (it["code"] == "base_development" and ("touches" in p or "result_calls_per_day" in p)):
                item = it
                break
        p = item.get("params", {}) if item else {}
        require_lpr = p.get("require_lpr", item["code"] == "base_development") if item else False
        result, steps, _ = self._result_calls(login, {"first": date_from, "last": date_to}, require_lpr,
                                              p.get("one_call_per_contact_day", False))
        return {"login": login, "name": e["name"], "steps": steps, "result": len(result),
                "kpi": f"KPI {item.get('n')}. {item['title']}" if item else None,
                "one_per_contact_day": bool(p.get("one_call_per_contact_day")), "require_lpr": require_lpr}

    def calc_base_development(self, e, ctx, p):
        emp = e["login"]
        result_calls, call_funnel, plain_calls = self._result_calls(emp, ctx, p.get("require_lpr", True),
                                                                    p.get("one_call_per_contact_day", False))
        meetings, _ = self._meetings(emp, ctx)
        comps, extra = [], []
        if "touches" in p:
            t = p["touches"].get(ctx["month"], p["touches"]["default"])
            touches = len(result_calls) + len([m for m in meetings if m["is_lpr"]])
            comps.append(comp("Результативные касания", touches, t, "шт", source=SRC_B24, pace=self._pace(ctx, t)))
        if "result_calls_per_day" in p:
            # норма на месяц: N звонков × рабочие дни без дней уважительного отсутствия (прогул не вычитается)
            n = p["result_calls_per_day"]
            days, so_far = len(self._work_dates(e, ctx)), len(self._work_dates(e, ctx, upto=ctx["asof"]))
            t = n * days
            away = ctx["wd"] - days
            basis = f"{n} в день × {days} раб. дн." + (f", без {away} дн. отсутствия" if away else "")
            if p.get("calls_plain"):  # звонок: от 2 минут, не секретарю; ЛПР и следующий шаг — уже для касаний
                name, counted = f"Звонки от {self.min_call // 60} мин, не секретарю", plain_calls
            else:
                name, counted = "Результативные звонки", result_calls
            comps.append(comp(f"{name} ({basis})", len(counted), t, "шт", source=SRC_B24,
                              pace=n * so_far if ctx["in_progress"] else None, audit="calls"))
        if "meetings_min" in p:
            comps.append(comp("Встречи / ВКС", len(meetings), p["meetings_min"], "шт", source=SRC_B24,
                              pace=self._pace(ctx, p["meetings_min"]), audit="meetings"))
        kp_sum = self._kp_with_letters(emp, ctx) if "kp_out_of_tender" in p else None
        kp_det = []
        if kp_sum:
            n, kp_extra, kp_det = kp_sum
            comps.append(comp("КП вне тендера: 1С + реестр исходящих", n, p["kp_out_of_tender"], "шт",
                              source="1С + журнал исходящих писем", pace=self._pace(ctx, p["kp_out_of_tender"]), audit="kp"))
            extra += kp_extra
        elif "kp_out_of_tender" in p:
            if self.config.get("letters_sheet", {}).get("url"):
                # КП вне тендера — письма тем из letters_sheet.topics в журнале исходящих
                n = self.q("SELECT COUNT(*) c FROM kp_letters WHERE emp=? AND dt>=? AND dt<=?",
                           emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")[0]["c"]
                src = "журнал исходящих писем"
            else:
                r = self.q("SELECT COUNT(*) c FROM kp WHERE emp=? AND out_of_tender=1 AND dt>=? AND dt<=?",
                           emp, ctx["first"].isoformat(), ctx["last"].isoformat())[0]
                # признак «вне тендера» в 1С может не вестись: тогда «нет данных», а не 0
                known = self.q("SELECT 1 FROM kp WHERE out_of_tender IS NOT NULL LIMIT 1")
                n, src = (r["c"] if known else None), SRC_1C
            comps.append(comp("КП вне тендера", n, p["kp_out_of_tender"], "шт", source=src,
                              pace=self._pace(ctx, p["kp_out_of_tender"]), audit="kp"))
        if "active_clients" in p:
            from .manual import client_approvals
            # KPI месяца — клиенты, которые стали активными именно в этом месяце. Текущее состояние сравниваем
            # со снимком прошлого месяца, чтобы уже засчитанные клиенты не переносились в следующий месяц.
            has_snapshot = self.q("SELECT 1 FROM clients_monthly WHERE month=? LIMIT 1", ctx["month"])
            if has_snapshot and not ctx["in_progress"]:
                rows = self.q("SELECT * FROM clients_monthly WHERE emp=? AND month=? AND need=1 AND strategy=1",
                              emp, ctx["month"])
                state_hint = "состояние на конец месяца"
            else:
                rows = self.q("SELECT * FROM clients WHERE emp=? AND need=1 AND strategy=1", emp)
                state_hint = "состояние на сегодня" if ctx["in_progress"] or ctx["future"] else \
                    "снимка на конец месяца нет — показано текущее состояние CRM"
            prev_month = month_add(ctx["month"], -1)
            has_prev_snapshot = self.q("SELECT 1 FROM clients_monthly WHERE month=? LIMIT 1", prev_month)
            if has_prev_snapshot:
                prev_ids = {r["id"] for r in self.q(
                    "SELECT id FROM clients_monthly WHERE emp=? AND month=? AND need=1 AND strategy=1",
                    emp, prev_month)}
                previous_approvals = client_approvals(self.con,emp,prev_month)
                prev_ids = {ref for ref in prev_ids if previous_approvals.get(ref) != 0}
                rows = [r for r in rows if r["id"] not in prev_ids]
                hint = f"стали активными в месяце; {state_hint}"
            else:
                hint = f"первый месяц учёта; {state_hint}"
            approvals = client_approvals(self.con,emp,ctx["month"])
            candidates = rows
            rows = [r for r in candidates if approvals.get(r["id"]) != 0]
            comps.append(comp("Клиенты, ставшие активными в месяце", len(rows), p["active_clients"], "шт",
                              source=SRC_B24))
            links = [{"label": r["name"], "url": record_url(self.config,"clients",r)} for r in rows]
            extra.append({"label": "Новые активные клиенты", "value": ", ".join(r["name"] for r in rows[:12]) or "—",
                          "links": links[:12], "hint": hint})
            if e.get("position") == "tender_lead":
                extra[-1]["client_approvals"] = [
                    {"ref":r["id"],"label":r["name"],"url":record_url(self.config,"clients",r),
                     "value":approvals.get(r["id"])} for r in candidates]
            if not ("touches" in p or "result_calls_per_day" in p):  # KPI только по клиентам: показать, из чего он
                allc = self.q("SELECT need, strategy FROM clients WHERE emp=?", emp)
                extra += [{"label": "Компаний, где ответственный", "value": len(allc), "hint": "сейчас в CRM"},
                          {"label": "С выявленной потребностью", "value": sum(1 for c in allc if c["need"])},
                          {"label": "Со стратегией и датами каждого пункта", "value": sum(1 for c in allc if c["strategy"])}]
                return {"components": comps, "extra": extra, "funnel": None,
                        "details": {"columns": ["Клиент, ставший активным в месяце"], "rows": [[r["name"]] for r in rows],
                                    "links": [[link["url"]] for link in links]}}
        return {
            "components": comps, "extra": extra,
            "funnel": {"title": "Как отбираются результативные звонки", "steps": call_funnel},
            "details": {"columns": ["Дата", "Клиент", "Длительность, мин", "Тема"],
                        "rows": [[r["dt"][:16].replace("T", " "), r["client"], round((r["duration"] or 0) / 60, 1),
                                  r["subject"]] for r in result_calls[:200]] +
                                [[d, client, "", kind + (f" — {note}" if note else "")] for d, kind, client, note in kp_det]},
        }

    def calc_tender_operations(self, e, ctx, p):
        """Тендеры 1С (ТТК_Тендер) сотрудника как ведущего менеджера в статусе «Отклонён» с датой тендера в месяце
        (onec_report.fetch_tenders → tenders_1c). Даты отклонения в 1С нет: «Дата итогов» почти всегда пуста. Норма — 0."""
        rows = self.q("SELECT * FROM tenders_1c WHERE emp=? AND status=? AND dt>=? AND dt<=? ORDER BY dt",
                      e["login"], REJECTED, ctx["first"].isoformat(), ctx["last"].isoformat())
        return {
            "components": [comp("Тендеры в статусе «Отклонён»", len(rows), p.get("failures_max", 0),
                                "шт", op="<=", source=SRC_1C)],
            "details": {"columns": ["Тендер", "Клиент", "Дата тендера", "Лот"],
                        "rows": [[r["doc"], r["client"], r["dt"], r["title"]] for r in rows]},
        }

    def calc_exec_discipline(self, e, ctx, p):
        emp = e["login"]
        if p.get("assessment_period") == "month":
            from urllib.parse import urlencode
            from .crm_links import portal_origin, object_url
            from .task_efficiency import report
            scores = self.q("SELECT score,comment FROM monthly_scores WHERE emp=? AND month=? AND criterion='docs'", emp,ctx["month"])
            score = scores[0] if scores else {"score":None,"comment":""}
            snapshot = (report(self.con,e["b24_user_id"],ctx["month"]) if e.get("b24_user_id") else None) or {}
            value = snapshot.get("percent")
            origin = portal_origin(self.config)
            url = f"{origin}/company/personal/user/{int(e['b24_user_id'])}/tasks/effective/" if origin and e.get("b24_user_id") else None
            if url:
                url += "?" + urlencode({"apply_filter":"Y","DATETIME_datesel":"RANGE",
                    "DATETIME_from":ctx["first"].strftime("%d.%m.%Y"),"DATETIME_to":ctx["last"].strftime("%d.%m.%Y")})
            result = {"components":[comp(CRITERIA_TITLES["docs"],score["score"],100,"балл",source=SRC_MAN),
                                   comp("Эффективность задач Б24 за месяц",value/100 if value is not None else None,1.0,"%",source=SRC_B24)],
                    "manual":{"period":"month","month":ctx["month"],"criteria":["docs"],"scores":{"docs":score}},
                    "extra":[{"label":"Всего задач в работе за месяц","value":snapshot.get("total")},
                             {"label":"Завершено за месяц","value":snapshot.get("completed")},
                             {"label":"Замечаний по срокам","value":snapshot.get("violations")},
                             {"label":"Расчёт эффективности","value":(f"100 − ({snapshot['violations']} / {snapshot['total']}) × 100 ≈ {value:g}%" if snapshot.get("total") else "Нет задач — 100%") if snapshot else "Данные задач ещё не загружены",
                              "links":[{"label":"Открыть эффективность в Б24","url":url}] if url else [],
                              "hint":f"Период: {ctx['first']:%d.%m.%Y}–{ctx['last']:%d.%m.%Y}. " +
                                     (f"Задачи и история обновлены: {snapshot.get('fetched')}" if snapshot else "Ожидается загрузка задач и истории за этот период.")}],
                    "details":{"columns":["Задача","Роль","Создана","Завершена","Замечаний","Нарушенные сроки"],
                               "rows":[[t["title"],t["role"],t["created"],t["closed"],len(t["violations"]),
                                         ", ".join(x["deadline"].replace("T"," ") for x in t["violations"]) or "—"] for t in snapshot.get("rows",[])],
                               "links":[[object_url(self.config,"task",t["id"],e["b24_user_id"]),None,None,None,None,None] for t in snapshot.get("rows",[])]}}
            if p.get("docs_source") == "contract_files":
                from .supply_documents import report as document_report
                documents = document_report(self.con,emp,ctx["first"],ctx["last"],ctx["asof"])
                percent = documents["percent"]
                result["components"][0] = comp(CRITERIA_TITLES["docs"],percent / 100 if percent is not None else None,1,"%",source=SRC_1C)
                result.pop("manual")
                result["extra"][0:0] = [
                    {"label":"Заказов поставщикам за месяц","value":documents["total"]},
                    {"label":"Заказов с файлом договора","value":documents["matched"]},
                    {"label":"Расчёт документооборота","value":f"{documents['matched']} / {documents['total']} × 100%" if documents["total"] else "Нет заказов для расчёта"}]
                result["entry"] = {
                    "table":"supply_contracts", "title":"Договоры к заказам поставщикам", "readonly":True,
                    "note":"Учитываются файлы карточки договора, указанного в заказе. Подходят названия с «договор», «договора», «дог.» без учёта регистра. Один заказ учитывается один раз. Ответственный — из связанной заявки в отдел снабжения. Период — по дате заказа поставщику.",
                    "columns":["Заказ поставщику","Дата заказа","Поставщик","Заявка в ОС","Договор","Файлы договора","Зачтён"],
                    "fields":[],
                    "rows":[{"ref":r["id"],"cells":[r["doc"],r["dt"],r["supplier"],r["request_docs"],r["contract_name"] or "—",
                              ", ".join(json.loads(r["files"])) or "—","Да" if r["has_contract"] else "Нет"]} for r in documents["rows"]]}
                if not documents["known"]:
                    result["warnings"] = ["Документооборот снабжения — файлы договоров за выбранный период ещё не загружены из 1С."]
            return result
        comps, weeks_all = [], {}
        for c in p.get("criteria", ["docs", "tasks"]):
            avg, weeks = self._manual(emp, ctx["month"], c)
            weeks_all[c] = weeks
            comps.append(comp(CRITERIA_TITLES[c], avg, 100, "балл", source=SRC_MAN))
        tasks = self.q("SELECT * FROM tasks WHERE emp=? AND deadline>=? AND deadline<=?",
                       emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        due = [t for t in tasks if parse_date(t["deadline"]) <= ctx["asof"]]
        on_time = [t for t in due if t["closed"] and t["closed"][:19] <= t["deadline"][:19]]
        share = len(on_time) / len(due) if due else None
        comps.append(comp("Задачи Б24 закрыты в срок (справочно)", share, 1.0, "%", source=SRC_B24, informative=True))
        overdue = [t for t in due if t not in on_time]
        return {
            "components": comps,
            "manual": {"criteria": p.get("criteria", ["docs", "tasks"]), "weeks": weeks_all},
            "extra": [{"label": "Задач со сроком в месяце", "value": len(tasks)},
                      {"label": "Просрочено / закрыто с опозданием", "value": len(overdue)}],
            "details": {"columns": ["Задача", "Срок", "Закрыта"],
                        "rows": [[t["title"], (t["deadline"] or "")[:10], (t["closed"] or "не закрыта")[:10]] for t in overdue],
                        "links": [[record_url(self.config,"tasks",t),None,None] for t in overdue]},
        }

    def calc_tender_receivables(self, e, ctx, p):
        emp = e["login"]
        g = self.q("SELECT * FROM guarantees WHERE emp=?", emp)
        contract = [r for r in g if r["kind"] == "contract" and _in(r["return_date"], ctx["first"], ctx["last"])]
        days = [(parse_date(r["return_date"]) - parse_date(r["acts_date"])).days for r in contract if r["acts_date"]]
        avg_days = sum(days) / len(days) if days else None
        apps = [r for r in g if r["kind"] == "application" and _in(r["unblock_date"], ctx["first"], ctx["last"])]
        ok = [r for r in apps if r["return_date"] and
              business_days_between(parse_date(r["unblock_date"]), parse_date(r["return_date"]),
                                    self.holidays, self.workdays) <= 1]
        share = len(ok) / len(apps) if apps else None
        first, last = ctx["first"].isoformat(), ctx["last"].isoformat()
        pen = self.q("SELECT dt, amount, reason FROM penalties WHERE emp=? AND dt>=? AND dt<=? UNION ALL "
                     "SELECT dt, amount, reason FROM manual_penalties WHERE emp=? AND dt>=? AND dt<=?",
                     emp, first, last, emp, first, last)
        # штрафы вносит руководитель: пока за месяц нет ни штрафа, ни отметки «штрафов нет», пункт ждёт ввода
        from_1c = bool(self.q("SELECT 1 FROM penalties LIMIT 1"))
        checked = bool(self.q("SELECT 1 FROM penalty_checks WHERE emp=? AND month=?", emp, ctx["month"]))
        pen_fact = len(pen) if (pen or checked or from_1c) else None
        rows = [["Обеспечение контракта", r["ref"], r["amount"], r["acts_date"], r["return_date"],
                 f"{(parse_date(r['return_date']) - parse_date(r['acts_date'])).days} дн."] for r in contract if r["acts_date"]]
        rows += [["Обеспечение заявки", r["ref"], r["amount"], r["unblock_date"], r["return_date"] or "не возвращено",
                  "в срок" if r in ok else "просрочено"] for r in apps]
        rows += [["Штраф", r["reason"], r["amount"], r["dt"], "", ""] for r in pen]
        return {
            "components": [
                comp("Среднее время возврата обеспечения контракта", avg_days, p["contract_return_days_max"], "дн", op="<=", source=SRC_1C),
                comp("Обеспечения заявок возвращены за 1 рабочий день", share, p["unblock_share_min"], "%", source=SRC_1C),
                comp("Штрафы за нарушение фин. дисциплины", pen_fact, p["penalties_max"], "шт", op="<=",
                     source=SRC_1C if from_1c else SRC_HAND),
            ],
            "details": {"columns": ["Тип", "Основание", "Сумма, ₽", "Дата события", "Возврат", "Итог"], "rows": rows},
        }

    def calc_conversion_kp_tenders(self, e, ctx, p):
        return self._order_conversion(e, ctx, p, "sales")

    def calc_conversion_sales(self, e, ctx, p):
        return self._order_conversion(e, ctx, p, "sales")

    def calc_gross_profit(self, e, ctx, p):
        """revenue — сумма заказов «В закуп» по дате заказа; ключ сохранён для существующих планов.
        gross_profit — валовая прибыль по месяцу подписания УПД/акта обеими сторонами."""
        emp = e["login"]
        metric = p.get("metric", "gross_profit")
        if metric == "revenue":
            return self._sales_order_plan(e, ctx, p)
        first, last = ctx["first"].isoformat(), ctx["last"].isoformat()
        counted = self.q("SELECT * FROM sales WHERE emp=? AND signed=1 AND COALESCE(signed_date, dt)>=? "
                         "AND COALESCE(signed_date, dt)<=?", emp, first, last)
        # документы месяца, не подписанные к концу месяца: засчитаются в месяце подписания
        pending = self.q("SELECT * FROM sales WHERE emp=? AND dt>=? AND dt<=? AND "
                         "(signed=0 OR signed IS NULL OR COALESCE(signed_date, dt)>?)", emp, first, last, last)

        def gp(r):
            return None if r["cost"] is None else (r["revenue"] or 0) - r["cost"]

        value = sum((r["revenue"] or 0) if metric == "revenue" else (gp(r) or 0) for r in counted)
        # себестоимость из 1С не пришла: ВП была бы равна выручке, поэтому «нет данных»
        no_cost = metric == "gross_profit" and any(r["cost"] is None for r in counted + pending)
        if no_cost:
            value = None
        plan_row = self.q("SELECT value FROM plans WHERE emp=? AND month=? AND metric=?", emp, ctx["month"], metric)
        plan = plan_row[0]["value"] if plan_row else self._default_plan(p)
        name = "Выручка без НДС" if metric == "revenue" else "Валовая прибыль"
        rows = [[r["doc"], r["client"], r["dt"], r["revenue"], r["cost"], gp(r),
                 (r["signed_date"] or r["dt"]) if r["signed"] else "нет"] for r in counted]
        rows += [[r["doc"], r["client"], r["dt"], r["revenue"], r["cost"], gp(r),
                  f"{r['signed_date']} — засчитано в месяце подписания" if r["signed"] and r["signed_date"]
                  else "не подписан"] for r in pending]
        res = {
            "components": [comp(f"{name} по УПД/актам, подписанным в месяце", value, plan, "₽", source=SRC_1C,
                                pace=self._pace(ctx, plan))],
            "plan": {"metric": metric, "value": plan, "is_default": not plan_row},
            "extra": [{"label": "Выручка (подписано в месяце)", "value": sum(r["revenue"] or 0 for r in counted), "fmt": "rub"},
                      {"label": "ВП (подписано в месяце)", "value": sum(gp(r) or 0 for r in counted), "fmt": "rub"},
                      {"label": "Документы месяца без подписи на конец месяца (ВП)", "value": sum(gp(r) or 0 for r in pending),
                       "fmt": "rub", "hint": "засчитываются в месяце подписания обеими сторонами"}],
            "details": {"columns": ["Документ", "Клиент", "Дата документа", "Выручка, ₽", "Себестоимость, ₽", "ВП, ₽",
                                    "Подписан обеими сторонами"], "rows": rows},
        }
        if no_cost:
            for x in res["extra"][1:]:
                x["value"] = None
            res["extra"].append({"label": "Себестоимость", "value": "нет данных",
                                 "hint": "в 1С не рассчитана: валовая прибыль появится, когда будет источник себестоимости"})
        return res

    def _sales_order_plan(self, e, ctx, p):
        orders = self._order_rows(e["login"], ctx, "sales")
        missing = self._missing_datasets(ctx, ("conversion_orders",))
        unknown = sum(r["amount"] is None for r in orders)
        value = None if missing or unknown else round(sum(r["amount"] for r in orders), 2)
        plan_row = self.q("SELECT value FROM plans WHERE emp=? AND month=? AND metric='revenue'", e["login"], ctx["month"])
        plan = plan_row[0]["value"] if plan_row else self._default_plan(p)
        warnings = []
        if missing:
            warnings.append("Для плана продаж нет полной выгрузки заказов клиента за выбранный период. Обновите данные.")
        elif unknown:
            warnings.append(f"У {unknown} заказов отсутствует поле «Сумма». Обновите данные из 1С; факт плана пока не определён.")
        return {"components": [comp("Сумма заказов клиента в статусе «В закуп»", value, plan, "₽", source=SRC_1C,
                                    pace=self._pace(ctx, plan))],
                "plan": {"metric": "revenue", "value": plan, "is_default": not plan_row},
                "extra": [{"label": "Заказы клиента «В закуп»", "value": len(orders)},
                          {"label": "Сумма заказов", "value": value, "fmt": "rub",
                           "hint": "Поле «Сумма» заказа клиента в 1С; по дате заказа и его менеджеру"}],
                "warnings": warnings,
                "details": {"columns": ["Заказ клиента", "Клиент", "Дата заказа", "Статус", "Сумма, ₽"],
                            "rows": [[r["doc"], r["client"], r["dt"], r["status"], r["amount"]] for r in orders]}}

    def calc_qualified_leads(self, e, ctx, p):
        from .manual import lead_approvals
        approvals = lead_approvals(self.con,e["login"],ctx["month"])
        rows = self.q("SELECT * FROM leads WHERE emp=? AND qualified_date>=? AND qualified_date<=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        names = [("need", "потребность"), ("budget", "бюджет и сроки"), ("lpr", "ЛПР"), ("solvency", "платёжеспособность"),
                 ("next_step", "следующий шаг")]
        good, det = [], []
        for r in rows:
            miss = [n for k, n in names if not r[k]]
            if not miss and approvals.get(r["id"]) != 0:
                good.append(r)
            det.append([r["title"], (r["qualified_date"] or "")[:10], "целевой" if not miss else "не заполнено: " + ", ".join(miss)])
        # лиды в работе, у которых выполнены не все условия, — подсказка менеджеру (срез на сегодня)
        open_ = self.q("SELECT * FROM leads WHERE emp=? AND qualified_date IS NULL", e["login"]) if ctx["in_progress"] else []
        partial = [r for r in open_ if any(r[k] for k, _ in names)]
        det += [[r["title"], "—", "не хватает: " + ", ".join(n for k, n in names if not r[k])] for r in partial]
        for row, lead in zip(det, rows + partial):
            value = approvals.get(lead["id"])
            row.append(value)
            if value == 0:
                row[2] += " · не согласовано администратором"
        return {
            "components": [comp("Новые целевые лиды", len(good), p["min_leads"], "шт", source=SRC_B24,
                                pace=self._pace(ctx, p["min_leads"]))],
            "extra": [{"label": "Лидов с выполненными 5 условиями в месяце", "value": len(good)},
                      {"label": "Лидов в работе, где выполнена часть условий", "value": len(partial),
                       "hint": "засчитаются, когда будут выполнены все пять"}],
            "details": {"columns": ["Лид", "Квалифицирован", "Статус", "Согласовано"], "rows": det,
                        "links": [[record_url(self.config,"leads",r),None,None,None] for r in rows + partial],
                        "lead_approval": {"column":3,"rows":[{"ref":r["id"],"value":approvals.get(r["id"])} for r in rows + partial]}},
        }

    def calc_operational_activity(self, e, ctx, p):
        emp = e["login"]
        first, last = ctx["first"].isoformat(), ctx["last"].isoformat()
        result_calls, call_funnel, _ = self._result_calls(emp, ctx, p.get("require_lpr", False))
        wd = len(self._work_dates(e, ctx))  # рабочие дни месяца без дней уважительного отсутствия
        if "result_calls_per_day" in p:
            calls_target = p["result_calls_per_day"] * wd
            calls_name = f"Результативные исходящие звонки ({p['result_calls_per_day']} в день × {wd} раб. дн.)"
        else:
            calls_target = p["result_calls_month"]
            calls_name = "Результативные исходящие звонки"

        kp_all = self.q("SELECT * FROM kp WHERE emp=? AND dt>=? AND dt<=?", emp, first, last)
        kp_target = p["kp_per_day"] * wd
        extra, kp_det = [], []
        kp_sum = self._kp_with_letters(emp, ctx)
        if kp_sum:
            n, extra, kp_det = kp_sum
            kp_comp = comp(f"КП: 1С + реестр исходящих ({p['kp_per_day']} в день × {wd} раб. дн.)", n, kp_target, "шт",
                           source="1С + журнал исходящих писем", pace=self._pace(ctx, kp_target), audit="kp")
        elif p.get("kp_confirmation", "b24") == "b24":
            # регламент: КП загружено в 1С, а скан исходящего письма прикреплён к карточке сделки в Б24
            letters = self.q("SELECT * FROM kp_letters WHERE emp=? AND dt>=? AND dt<=?", emp, first, last + "T23:59:59")
            kp_fact = min(len(kp_all), len(letters))
            # письма берутся из журнала исходящих (Google-таблица), если он подключён, иначе из сделок Б24
            sheet = bool(self.config.get("letters_sheet", {}).get("url"))
            where, src = ("в журнале исходящих", "журнал исходящих писем") if sheet else ("в сделке Б24", SRC_B24)
            kp_comp = comp(f"КП в 1С, подтверждённые письмом {where} ({p['kp_per_day']} в день × {wd} раб. дн.)",
                           kp_fact, kp_target, "шт", source=f"{SRC_1C} + {src}", pace=self._pace(ctx, kp_target), audit="kp")
            extra += [{"label": "КП загружено в 1С", "value": len(kp_all)},
                      {"label": "Исходящих писем в журнале" if sheet else "Исходящих писем с КП и вложением в сделках Б24",
                       "value": len(letters), "hint": "засчитывается меньшее из двух чисел"}]
        else:
            kp_ok = [k for k in kp_all if k["confirmed"]]
            kp_comp = comp(f"КП в 1С со сканом письма ({p['kp_per_day']} в день × {wd} раб. дн.)", len(kp_ok),
                           kp_target, "шт", source=SRC_1C, pace=self._pace(ctx, kp_target), audit="kp")
            extra.append({"label": "КП без подтверждения отправки", "value": len(kp_all) - len(kp_ok)})

        meetings, meetings_all = self._meetings(emp, ctx, need_protocol=True)
        # регламент: встреча закрывается в день проведения
        late_close = [m for m in meetings if m["closed_dt"] and m["dt"] and m["closed_dt"][:10] > m["dt"][:10]]
        if p.get("meeting_same_day_close"):
            meetings, _ = self._meetings(emp, ctx, need_protocol=True, same_day_close=True)
        no_protocol = [m for m in meetings_all if not (m["has_protocol"] and m["has_summary"])]
        extra += [{"label": "Встречи без протокола/резюме", "value": len(no_protocol)},
                  {"label": "Встречи закрыты не в день проведения", "value": len(late_close),
                   "hint": "не засчитываются" if p.get("meeting_same_day_close") else "справочно"}]
        det = [[d, client, kind, note] for d, kind, client, note in kp_det]
        det += [[m["dt"][:10], m["client"], "Встреча", "нет протокола или резюме"] for m in no_protocol]
        det += [[m["dt"][:10], m["client"], "Встреча", f"закрыта {m['closed_dt'][:10]}, не в день проведения"]
                for m in late_close]
        return {
            "components": [
                comp(calls_name, len(result_calls), calls_target, "шт", source=SRC_B24, pace=self._pace(ctx, calls_target), audit="calls"),
                kp_comp,
                comp("Встречи с протоколом и резюме", len(meetings), p["meetings_min"], "шт", source=SRC_B24,
                     pace=self._pace(ctx, p["meetings_min"]), audit="meetings"),
            ],
            "funnel": {"title": "Как отбираются результативные звонки", "steps": call_funnel},
            "extra": extra,
            "details": {"columns": ["Дата", "Клиент", "Тип", "Комментарий"], "rows": det},
        }

    def _receivable_rows(self, emp, from_1c=False):
        """Документы со сроком оплаты: этапы оплаты заказов клиентов из 1С (receivables, только from_1c — пункт
        с октября 2026; прошлые месяцы остаются на ручном вводе) или реализации со сроком, введённым руководителем."""
        if from_1c and self.has_1c_receivables():
            return self.q("SELECT * FROM receivables WHERE emp=?", emp), SRC_1C
        # в 1С сроки оплаты не ведутся: срок и дату оплаты реализаций вводит руководитель
        return self.q("SELECT id, doc, client, dt AS ship_date, due_date, paid_date, revenue AS amount FROM sales "
                      "WHERE emp=? AND due_date IS NOT NULL", emp), SRC_INPUT

    def has_1c_receivables(self):
        return bool(self.q("SELECT 1 FROM receivables LIMIT 1"))

    def calc_receivables_overdue(self, e, ctx, p):
        result = self._receivables_overdue_data(e, ctx, p)
        if not p.get("penalties_zero"):
            return result
        first, last = ctx["first"].isoformat(), ctx["last"].isoformat()
        fines = self.q("SELECT dt,amount,reason FROM penalties WHERE emp=? AND dt>=? AND dt<=? UNION ALL "
                       "SELECT dt,amount,reason FROM manual_penalties WHERE emp=? AND dt>=? AND dt<=?",
                       e["login"], first, last, e["login"], first, last)
        checked = bool(self.q("SELECT 1 FROM penalty_checks WHERE emp=? AND month=?", e["login"], ctx["month"]))
        source_known = not self._missing_datasets(ctx, ("penalties",))
        known = bool(fines or checked or source_known)
        result["components"].append(comp("Штрафы за нарушение финансовой дисциплины", len(fines) if known else None,
                                         0, "шт", op="<=", source=SRC_1C if source_known else SRC_HAND))
        result["uses_penalties"] = True
        result["penalties_source_known"] = source_known
        result["payout_pending"] = not known or any(c["pct"] is None for c in result["components"])
        result["penalty_zero"] = "Есть штраф за выбранный месяц: KPI 5 полностью обнулён." if fines else None
        if result.get("extra") and self.rules.get("payout", {}).get("mode") != "proportional":
            result["extra"][0]["label"] = "Выплата по шкале просрочки (до штрафов)"
        result.setdefault("extra", []).append({"label": "Проверка штрафов", "value":
            "Есть штраф — выплата по пункту 0 ₽" if fines else "Штрафов нет" if known else "Ожидает подтверждения руководителя"})
        return result

    def _receivables_overdue_data(self, e, ctx, p):
        """С октября 2026: самая большая просрочка оплаты за месяц в рабочих днях → доля выплаты пункта по шкале
        scale [[до N раб. дн. включительно, доля], …]; больше последнего порога — 0. Весь KPI не обнуляется.
        Сроки и оплаты — из 1С по заказам клиентов (правила оплаты и расчёты с клиентами)."""
        rows, src = self._receivable_rows(e["login"], from_1c=True)
        scale = p.get("scale", [[7, 1.0], [14, 0.5]])
        name = "Самая большая просрочка оплаты"
        if not rows and src != SRC_1C:  # сроки не введены: «просрочки 0 дн.» было бы ложным выполнением
            # (в выгрузке 1С отсутствие строк значит, что отгруженных неоплаченных заказов нет)
            return {"components": [comp(name, None, scale[0][0], "раб. дн", op="<=", source=src)]}
        worst, det, edits = 0, [], []
        overrides = {(r["ref"], r["field"]): r["value"] for r in self.q(
            "SELECT m.ref,m.field,m.value FROM manual_fields m JOIN receivables r ON r.id=m.ref "
            "WHERE m.tbl='receivables' AND m.field IN ('due_date','paid_date') AND r.emp=?", e["login"])} if src == SRC_1C else {}
        for raw in rows:
            r = state_at(raw, ctx["asof"]) if src == SRC_1C else raw
            source_paid = r["paid_date"]
            if (raw["id"], "due_date") in overrides:
                r = {**r, "due_date": overrides[(raw["id"], "due_date")]}
            payment = overrides.get((raw["id"], "paid_date"))
            if payment is not None:
                r = {**r, "paid_date": None if payment == "unpaid" else payment}
                if source_paid and not r["amount"] and r.get("ship_date"):
                    r["amount"] = r.get("planned_amount") or raw["amount"]
            due = parse_date(r["due_date"])
            paid = parse_date(r["paid_date"])
            created = parse_date(r.get("order_date"))
            shipped = parse_date(r.get("ship_date"))
            if not due or (paid and paid < ctx["first"]) or (created and created > ctx["asof"]):
                continue
            paid_now = paid and paid <= ctx["asof"]
            has_debt = bool((r["amount"] or 0) > 0 and (not shipped or shipped <= ctx["asof"]))
            end = paid if paid_now else ctx["asof"]
            od = business_days_between(due, end, self.holidays, self.workdays) if has_debt else 0
            worst = max(worst, od)
            if paid_now:
                status = "Оплачен с просрочкой" if od else "Оплачен"
            elif not has_debt:
                status = "Не отгружен" if not shipped or shipped > ctx["asof"] else "Нет задолженности"
            else:
                status = "Просрочен" if od else "Срок не наступил" if due > ctx["asof"] else "Не просрочен"
            if od > 0 or src == SRC_1C:
                det.append([r["doc"], r["client"], r.get("planned_amount") or r["amount"], r["due_date"],
                            paid.isoformat() if paid_now else "не оплачено", od,
                            0 if paid_now or not has_debt else r["amount"], status,
                            "—" if paid_now else "До полной оплаты"])
                edits.append({"ref": raw["id"], "manual": (raw["id"], "due_date") in overrides, "source_value": raw["due_date"],
                              "paid": {"ref": raw["id"], "manual": payment is not None, "source_value": source_paid,
                                       "effective_value": r["paid_date"]}})
        ordered = sorted(zip(det, edits), key=lambda x: (-x[0][5], x[0][3]))
        det, edits = [r for r, _ in ordered], [meta for _, meta in ordered]
        paid_edits = [meta.pop("paid") for meta in edits]
        share = next((s for days, s in scale if worst <= days), 0.0)
        bands = "; ".join(f"до {d} — {round(s * 100)}%" for d, s in scale) + f"; больше {scale[-1][0]} — 0%"
        proportional = self.rules.get("payout", {}).get("mode") == "proportional"
        performance = comp(name, worst, scale[0][0], "раб. дн", op="<=", source=src)
        return {
            "components": [performance],
            **({"payout_share": share} if not proportional else {}),
            "extra": [{"label": "Доля стоимости KPI по выполнению" if proportional else "Выплата по шкале",
                       "value": f"{payout_fraction(performance['pct']) * 100:.0f}%" if proportional else f"{round(share * 100)}%",
                       "hint": "Стоимость пункта × процент выполнения, максимум 100%; штраф обнуляет этот пункт" if proportional else bands},
                      {"label": "Состояние на дату", "value": ctx["asof"].isoformat()}],
            "details": {"columns": ["Заказ клиента" if src == SRC_1C else "Документ", "Клиент", "Сумма, ₽", "Срок оплаты",
                                    "Оплачено полностью", "Просрочка, раб. дн", "Остаток на дату, ₽", "Статус",
                                    "Контроль в следующих месяцах"], "rows": det,
                        **({"date_edit": {"table": "receivables", "field": "due_date", "column": 3, "rows": edits},
                            "paid_edit": {"table": "receivables", "field": "paid_date", "column": 4, "rows": paid_edits}}
                           if src == SRC_1C else {})},
        }

    def calc_receivables_pm(self, e, ctx, p):
        rows, src = self._receivable_rows(e["login"])
        if not rows:
            # сроков оплаты нет: «просрочки 0 дн.» было бы ложным выполнением
            return {"components": [comp("Оплачено в срок (по отгрузкам со сроком в месяце)", None, 1.0, "%", source=src),
                                   comp("Максимальная просрочка", None, p["overdue_days_max"], "дн", op="<=", source=src)]}
        due_in = [r for r in rows if _in(r["due_date"], ctx["first"], min(ctx["last"], ctx["asof"]))]
        on_time = [r for r in due_in if r["paid_date"] and parse_date(r["paid_date"]) <= parse_date(r["due_date"])]
        share = len(on_time) / len(due_in) if due_in else None
        # максимальная просрочка на конец месяца (или на сегодня) по всем документам
        worst, det = 0, []
        for r in rows:
            due = parse_date(r["due_date"])
            if not due or due > ctx["asof"]:
                continue
            paid = parse_date(r["paid_date"])
            if paid and paid < ctx["first"]:
                continue
            end = paid if paid and paid <= ctx["asof"] else ctx["asof"]
            od = (end - due).days
            if od > 0:
                worst = max(worst, od)
                det.append([r["doc"], r["client"], r["amount"], r["due_date"], r["paid_date"] or "не оплачено", od])
        det.sort(key=lambda x: -x[5])
        res = {
            "components": [
                comp("Оплачено в срок (по отгрузкам со сроком в месяце)", share, 1.0, "%", source=src),
                comp("Максимальная просрочка", worst, p["overdue_days_max"], "дн", op="<=", source=src),
            ],
            "details": {"columns": ["Документ", "Клиент", "Сумма, ₽", "Срок оплаты", "Оплачено", "Просрочка, дн"], "rows": det},
        }
        if worst > p["overdue_days_max"] and p.get("zero_all_if_overdue"):
            res["zero_all"] = f"Просрочка дебиторской задолженности {worst} дн. (более {p['overdue_days_max']}). " \
                              f"По регламенту KPI не выплачивается."
        return res

    def calc_on_time_delivery(self, e, ctx, p):
        from .supply_delivery import delivery_groups, delivery_overrides
        current_day = today().isoformat()
        cutoff = min(ctx["last"], ctx["asof"]).isoformat()
        rows = [r for r in delivery_groups(self.q("SELECT * FROM supplier_orders WHERE emp=? ORDER BY dt,doc,id", e["login"]), delivery_overrides(self.con))
                if r["planned_date"] and ctx["first"].isoformat() <= r["planned_date"] <= ctx["last"].isoformat()]
        det, ok, awaiting = [], 0, 0
        for r in rows:
            reasons = []
            actual = r["customer_actual_date"]
            if r["planned_date"] >= current_day and (not actual or actual > cutoff):
                awaiting += 1
                continue
            if not actual or actual > ctx["asof"].isoformat():
                reasons.append("не поставлено на дату расчёта")
            elif actual > r["planned_date"]:
                reasons.append(f"опоздание {(parse_date(actual) - parse_date(r['planned_date'])).days} дн.")
            if actual and not r["complete"]:
                reasons.append("недопоставка")
            for supplier in r["rows"]:
                if not supplier.get("defect"):
                    continue
                fixed = supplier.get("defect_fixed_date")
                label = supplier.get("doc") or supplier["id"]
                if not fixed or fixed > ctx["asof"].isoformat():
                    reasons.append(f"{label}: брак не исправлен на дату расчёта")
                elif fixed > r["planned_date"]:
                    days = (parse_date(fixed) - parse_date(r["planned_date"])).days
                    reasons.append(f"{label}: брак исправлен с опозданием {days} дн.")
            if not reasons:
                ok += 1
            else:
                det.append([r["doc"], ", ".join(x["doc"] or x["id"] for x in r["rows"]), r["planned_date"], actual or "—", ", ".join(reasons)])
        late_share = len(det) / len(rows) if rows else None
        component = comp("Доля просроченных поставок", late_share, p["late_share_max"], "%", op="<=", source=SRC_INPUT)
        if late_share is not None and late_share > p["late_share_max"]:
            component["pct"] = max(0, (1 - late_share) / (1 - p["late_share_max"]))
        return {
            "components": [component],
            "extra": [{"label": "Заказов клиента с плановой датой в месяце", "value": len(rows)},
                      {"label": "Вовремя и в полном объёме", "value": ok},
                      {"label": "Просрочено или с нарушениями", "value": len(det)},
                      {"label": "Ожидают поставки в пределах срока", "value": awaiting}],
            "details": {"columns": ["Заказ клиента", "Заказы поставщикам", "Плановая дата поставки", "Фактическая дата поставки", "Нарушение"], "rows": det},
        }

    def calc_deferred_payment(self, e, ctx, p):
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND dt>=? AND dt<=? AND amount_novat>=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat(), p["threshold"])
        good = [r for r in rows if (r["deferred_share"] or 0) >= p["deferred_share_min"] - EPS
                and (r["deferral_days"] or 0) >= p["deferral_days_min"]]
        if p.get("basis") == "amount":
            tot = sum(r["amount_novat"] for r in rows)
            share = sum(r["amount_novat"] for r in good) / tot if tot else None
        else:
            share = len(good) / len(rows) if rows else None
        # условия отсрочки вводит руководитель: пока они не введены по всем заказам, пункт ждёт ввода
        pending = [r for r in rows if r["deferred_share"] is None or r["deferral_days"] is None]
        if pending:
            share = None
        splits = self._split_orders(e["login"], ctx, p["threshold"])
        res = {
            "components": [comp("Доля заказов с отсрочкой", share, p["target"], "%", source=SRC_INPUT)],
            "extra": [{"label": f"Заказов от {p['threshold']:,.0f} ₽ без НДС".replace(",", " "), "value": len(rows)},
                      {"label": "С отсрочкой ≥ 30% суммы на ≥ 14 дней", "value": len(good)},
                      {"label": "Возможное дробление заказов", "value": len(splits),
                       "hint": "мелкие заказы одного лота или заказа клиента у одного поставщика в сумме выше порога"},
                      {"label": "Ждут ввода условий отсрочки", "value": len(pending)}],
            "details": {"columns": ["Заказ", "Поставщик", "Сумма без НДС, ₽", "Доля отсрочки", "Дней отсрочки", "Засчитан"],
                        "rows": [[r["doc"], r["supplier"], r["amount_novat"], r["deferred_share"], r["deferral_days"],
                                  "ждёт ввода" if r in pending else "да" if r in good else "нет"] for r in rows] +
                                [[", ".join(x["doc"] for x in items), items[0]["supplier"], total, None, None,
                                  "возможное дробление"] for _, items, total in splits],
                        "pct_cols": [3]},
        }
        if splits:
            res["warnings"] = ["KPI 2: возможное дробление заказа — " + ", ".join(x["doc"] for x in items) +
                               f" на {total:,.0f} ₽ без НДС".replace(",", " ") for _, items, total in splits]
        return res

    def _split_orders(self, emp, ctx, threshold):
        """Заказы ниже порога, которые вместе (один лот или один заказ клиента у одного поставщика) превышают порог.
        Регламент запрещает дробить заказ, чтобы уйти из-под порога 500 000 ₽."""
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND dt>=? AND dt<=? AND amount_novat<?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat(), threshold)
        groups = {}
        for r in rows:
            if r["lot"]:
                key = ("lot", r["lot"])
            elif r["customer_order"]:
                key = ("order", r["customer_order"], r["supplier"])
            else:
                continue
            groups.setdefault(key, []).append(r)
        out = []
        for key, items in groups.items():
            total = sum(i["amount_novat"] or 0 for i in items)
            if len(items) > 1 and total >= threshold:
                out.append((key, items, total))
        return out

    def calc_competitive_purchases(self, e, ctx, p):
        """Закупки из той же базы заказов поставщикам, что KPI 2. Один заказ — одна закупка; заказы с одинаковым
        явно указанным лотом объединяются и проверяются по общей сумме без НДС. Проект/заказ клиента сам по себе
        лотом не считается: несколько заказов по 150 тыс. попадут в KPI только при общем лоте от порога."""
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND dt>=? AND dt<=? ORDER BY dt, doc",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat())
        grouped = {}
        for r in rows:
            lot = (r["lot"] or "").strip()
            key = ("lot", lot.casefold()) if lot else ("order", r["id"])
            grouped.setdefault(key, []).append(r)

        def flag(items, field):
            values = [r[field] for r in items if r[field] is not None]
            return int(any(values)) if values else 0

        purchases = []
        for key, items in grouped.items():
            amount = sum(r["amount_novat"] or 0 for r in items)
            if amount < p["threshold"]:
                continue
            counts = [r["kp_count"] for r in items if r["kp_count"] is not None]
            purchase = {
                "key": key, "items": items, "amount": amount,
                "lot": (items[0]["lot"] or "").strip(),
                "docs": ", ".join(r["doc"] for r in items),
                "suppliers": ", ".join(dict.fromkeys(r["supplier"] or "—" for r in items)),
                # Источник поля kp_count — не число КП: загрузчик 1С записывает сюда количество поставщиков
                # (Поставщик 1…10) в связанной заявке в отдел снабжения.
                "supplier_count": max(counts) if counts else None,
                **{f: flag(items, f) for f in ("single_source", "emergency", "justified")},
            }
            purchases.append(purchase)

        excluded, pending, good = [], [], []
        for r in purchases:
            if r["single_source"]:
                r["result"] = "исключена: монополия подтверждена письмом"
                excluded.append(r)
                continue
            if r["emergency"]:
                r["result"] = "исключена: аварийная закупка"
                excluded.append(r)
                continue
            if r["supplier_count"] is None:
                r["result"] = "нет данных о поставщиках в заявке"
                pending.append(r)
                continue
            competitive = r["supplier_count"] >= p["supplier_min"]
            valid = competitive or r["justified"]
            if valid:
                good.append(r)
                r["result"] = "да — не менее 3 поставщиков" if competitive else "да — утверждённое обоснование"
            else:
                r["result"] = "нет: меньше 3 поставщиков и нет утверждённого обоснования"
        base = [r for r in purchases if r not in excluded]
        share = None if pending else (len(good) / len(base) if base else None)
        # Количество поставщиков приходит только из заявки 1С и вручную не подменяется: если связи/данных нет,
        # это «нет данных», а не ожидание оценки руководителя.
        component_source = SRC_1C if pending or not base else SRC_INPUT
        return {
            "components": [comp("Доля конкурентных или обоснованных закупок", share, p["target"], "%",
                                source=component_source)],
            "extra": [
                {"label": f"Закупок от {p['threshold']:,.0f} ₽ без НДС".replace(",", " "), "value": len(purchases)},
                {"label": "Не менее 3 поставщиков или есть обоснование", "value": len(good)},
            ],
            "details": {
                "columns": ["Лот / заказы", "Поставщики по заказам", "Сумма без НДС, ₽",
                            "Поставщиков в заявке отдела снабжения", "Итог"],
                "rows": [[r["lot"] or r["docs"], r["suppliers"], r["amount"], r["supplier_count"], r["result"]]
                         for r in purchases],
            },
        }

    def calc_supply_funnel(self, e, ctx, p):
        return self._order_conversion(e, ctx, p, "supply")

    # ------------------------------------------------------------------ assembly
    @staticmethod
    def items_for(pos, month):
        """Пункты KPI должности, действующие в месяце ГГГГ-ММ: from / until — первый и последний месяц действия
        (включительно), weights — {«с какого месяца»: вес} поверх weight. Номера n — по порядку действующих пунктов:
        прошлые месяцы считаются по правилам того времени, а не задним числом."""
        out = []
        for it in pos["items"]:
            if (it.get("from") and month < it["from"]) or (it.get("until") and month > it["until"]):
                continue
            w = it["weight"]
            for since, v in sorted((it.get("weights") or {}).items()):
                if month >= since:
                    w = v
            out.append({**it, "weight": w})
        for n, it in enumerate(out, 1):
            it["n"] = n
        return out

    def compute(self, login, month):
        e = self.employee(login)
        pos = self.rules["positions"][e["position"]]
        ctx = self._ctx(month)
        payout_cfg = self.rules.get("payout", {})
        draft = bool(pos.get("draft"))
        bonus = pos["kpi_bonus"] or 0
        issues = list(pos.get("setup_issues") or []) if draft else []
        items, zero_all, warnings = [], [], issues[:1]
        absence = self._absence(e, ctx)
        absent = ctx["wd"] > 0 and absence["days"] >= ctx["wd"]  # весь месяц не работал: KPI не считается
        for it in self.items_for(pos, month):
            if absent and not draft:
                items.append({**{k: it[k] for k in ("code", "n", "title", "weight", "period", "target_text", "description")},
                              "amount": round(bonus * it["weight"]), "pct": None, "status": "absent", "payout": 0, "components": []})
                continue
            if draft:
                r = {"components": []}
            else:
                fn = getattr(self, "calc_" + it["code"])
                r = fn(e, ctx, it.get("params", {}))
            comps = r["components"]
            scored = [c for c in comps if not c["informative"]]
            pcts = [c["pct"] for c in scored if c["pct"] is not None]
            missing = [c for c in scored if c["pct"] is None]
            pct = min(pcts) if pcts else None
            amount = round(bonus * it["weight"])
            awaiting_manual = any(c["source"] in (SRC_MAN, SRC_INPUT, SRC_HAND) for c in missing)
            proportional = payout_cfg.get("mode") == "proportional"
            close_pct = 0.8
            if pct is None:
                status = "awaiting" if awaiting_manual else "nodata"
                payout = amount if not proportional and payout_cfg.get("pay_when_no_data") else 0
            else:
                met = pct >= 1 - EPS
                if proportional:
                    payout = amount * payout_fraction(pct) if not missing else 0
                else:
                    payout = amount if met else 0
                status = "met" if met else ("close" if pct >= close_pct - EPS else "fail")
                if proportional and 0 < pct < 1 - EPS:
                    status = "partial"
                if missing:
                    status = "awaiting" if awaiting_manual else "nodata" if proportional else status
                if not proportional and r.get("payout_share") is not None and not missing:  # legacy binary scale
                    share = r["payout_share"]
                    pct, payout = share, amount * share
                    status = "met" if share >= 1 - EPS else ("partial" if share > EPS else "fail")
            if ctx["in_progress"] and status in ("fail", "close", "nodata"):
                status = "progress_" + status
            if draft:
                status, pct, payout = "draft", None, 0
            if r.get("payout_pending"):
                payout, status = 0, "awaiting"
            if r.get("penalty_zero"):
                payout, pct, status = 0, 0, "penalty_zero"
            if r.get("zero_all"):
                zero_all.append(r["zero_all"])
            warnings += r.get("warnings", [])
            items.append({**{k: it[k] for k in ("code", "n", "title", "weight", "period", "target_text", "description")},
                          "amount": amount, "pct": pct, "status": status, "payout": round(payout),
                          **{k: v for k, v in r.items() if k not in ("zero_all", "warnings")}})
        total = sum(i["payout"] for i in items)
        if zero_all:
            total = 0
        return {
            "login": login, "name": e["name"], "url": employee_url(self.config,login), "position": e["position"], "position_title": pos["title"],
            "department": pos["department"], "salary": pos["salary"] or 0, "kpi_bonus": bonus, "month": month,
            "draft": draft, "setup_issues": issues,
            "working_days": ctx["wd"], "working_days_elapsed": ctx["wd_elapsed"], "in_progress": ctx["in_progress"],
            "payout_mode": payout_cfg.get("mode", "binary"),
            "items": items, "zero_all": zero_all, "warnings": warnings, "payout": total, "payout_before_zero": sum(i["payout"] for i in items),
            "share": total / bonus if bonus else 0,
            "met_count": len([i for i in items if i["status"] == "met"]), "item_count": len(items),
            "absent": absent, "absence": absence if absence["days"] or absence["truancy_days"] else None,
        }

    def summary(self, login, month):
        r = self.compute(login, month)
        return {"month": month, "payout": r["payout"], "share": r["share"],
                "items": [{"n": i["n"], "pct": i["pct"], "status": i["status"], "payout": i["payout"]} for i in r["items"]]}

    def trend(self, login, month, n=6):
        return [self.summary(login, month_add(month, -k)) for k in range(n - 1, -1, -1)]

    def overview(self, month):
        groups = {}
        alerts = []
        for e in self.employees():
            r = self.compute(e["login"], month)
            r["trend"] = [{"month": t["month"], "share": t["share"], "payout": t["payout"]} for t in self.trend(e["login"], month)]
            for i in r["items"]:
                i.pop("details", None)
                i.pop("funnel", None)
                i.pop("entry", None)
            groups.setdefault(e["position"], []).append(r)
            for z in r["zero_all"]:
                alerts.append({"login": e["login"], "name": e["name"], "level": "critical", "text": z})
            for w in r["warnings"]:
                alerts.append({"login": e["login"], "name": e["name"], "level": "warning", "text": w})
            for i in r["items"]:
                if i["status"] == "awaiting":
                    alerts.append({"login": e["login"], "name": e["name"], "level": "info",
                                   "text": f"KPI {i['n']} «{i['title']}»: нет оценки руководителя за месяц"})
        out = []
        for key, pos in self.rules["positions"].items():
            if key in groups:
                out.append({"position": key, "title": pos["title"], "department": pos["department"],
                            "columns": [{"n": i["n"], "title": i["title"], "weight": i["weight"]}
                                        for i in self.items_for(pos, month)],
                            "employees": groups[key]})
        all_emps = [x for g in out for x in g["employees"] if not x["absent"] and not x["draft"]]
        return {
            "month": month,
            "groups": out,
            "alerts": alerts,
            "totals": {
                "fund": sum(x["kpi_bonus"] for x in all_emps),
                "payout": sum(x["payout"] for x in all_emps),
                "items": sum(x["item_count"] for x in all_emps),
                "items_met": sum(x["met_count"] for x in all_emps),
                "full": len([x for x in all_emps if x["share"] >= 1 - EPS]),
                "employees": len(all_emps),
            },
        }
