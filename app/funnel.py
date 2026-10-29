"""Воронки сделок и лидов: текущий срез открытых записей CRM, разложенный по секциям из config.deal_funnel / lead_funnel."""
import datetime as dt

from .common import load_rules

DEFAULT_SECTIONS = {
    "deals": [
        {"key": "work", "title": "В работе", "all_open": True},
        {"key": "precontract", "title": "Преддоговорные сделки", "stages": ["PREPARATION", "PREPAYMENT_INVOICE"]},
        {"key": "contract", "title": "Договорные сделки", "stages": ["EXECUTING", "FINAL_INVOICE"]},
    ],
    "leads": [
        {"key": "new", "title": "Распределено", "stages": ["NEW"]},
        {"key": "work", "title": "В работе", "stages": ["IN_PROCESS"]},
        {"key": "processed", "title": "Обработано", "stages": ["PROCESSED"]},
    ],
}
# вид воронки → значение crm_deals.entity и блок настроек в config.json
KINDS = {"deals": ("deal", "deal_funnel"), "leads": ("lead", "lead_funnel")}
# по какой дате отбирать записи за период → столбец crm_deals
DATE_FIELDS = {"created": "created", "stage": "stage_since"}


def _days(since, now):
    try:
        return max(0, (now - dt.datetime.fromisoformat(str(since)[:19])).days)
    except (TypeError, ValueError):
        return None


# «Факт»: лишние этапы вокруг этапов из настроек — «Создано» в начале и итоги закрытия в конце
FACT_FIRST = {"key": "_created", "title": "Создано в периоде", "created": True}
FACT_LAST = [{"key": "_won", "title": "Закрыто успешно", "semantic": "S"}, {"key": "_lost", "title": "Закрыто неуспешно", "semantic": "F"}]


def _fact_rows(con, entity, date_from, date_to):
    """Записи, с которыми что-то произошло в периоде (создание или переход на стадию), и сами переходы периода.
    У записи _new — создана в периоде."""
    recs = {r["id"]: dict(r) for r in con.execute("SELECT * FROM fact_records WHERE entity = ?", (entity,))}
    inside = lambda ts: bool(ts) and (not date_from or ts[:10] >= date_from) and (not date_to or ts[:10] <= date_to)
    moves = [dict(r) for r in con.execute("SELECT * FROM stage_moves WHERE entity = ? ORDER BY ts", (entity,))
             if inside(r["ts"]) and r["rid"] in recs]
    by_rec = {}
    for m in moves:
        by_rec.setdefault(m["rid"], []).append(m)
    for r in recs.values():
        r["_new"] = inside(r["created"])
    touched = {i for i, r in recs.items() if r["_new"] or i in by_rec}
    return [recs[i] for i in touched], by_rec


def _fact_pick(sc, rows, by_rec):
    """Записи этапа в «Факте»: «Создано» — созданные в периоде; остальные этапы — записи, перешедшие на их стадии в периоде
    (стадия и время — последнего такого перехода)."""
    ids = set(sc.get("stages") or [])
    names = {x.strip().lower() for x in sc.get("stage_names") or []}
    only_cats = {str(c) for c in sc.get("category_ids") or []}
    out = []
    for r in rows:
        if sc.get("created"):
            if r["_new"]:
                out.append(r)
            continue
        hit = None
        for m in by_rec.get(r["id"], []):
            if only_cats and m["category_id"] not in only_cats:
                continue
            if sc.get("semantic"):
                ok = m["semantic"] == sc["semantic"]
            elif sc.get("all_open"):
                ok = m["semantic"] not in ("S", "F")
            else:
                ok = m["stage_id"] in ids or (m["stage"] or "").strip().lower() in names
            if ok:
                hit = m
        if hit:
            out.append({**r, "category": hit["category"] or r["category"], "stage": hit["stage"],
                        "stage_sort": hit["stage_sort"] or 0, "stage_since": hit["ts"]})
    return out


def deal_funnel(con, config, login=None, category=None, kind="deals", date_from=None, date_to=None, date_field="created",
                departments=None, mode="date"):
    """login — ключ ответственного (логин сотрудника или «id:<ID в Б24>») либо список таких ключей.
    date_from / date_to (ГГГГ-ММ-ДД, включительно) оставляют в срезе записи, у которых дата date_field попадает
    в период: created — дата создания, stage — дата перехода на текущую стадию.
    mode="fact" — «Факт»: что произошло в периоде по истории стадий (создано, перешло на стадии этапов, закрыто),
    включая уже закрытые записи; date_field в этом режиме не используется.
    departments — отделы ответственных из раздела «Сотрудники» (по логину, а без KPI — по ID в Б24);
    «» — ответственные, которых в разделе нет («Без отдела»)."""
    entity, cfg_key = KINDS[kind]
    fcfg = config.get(cfg_key, {})
    sections_cfg = fcfg.get("sections") or DEFAULT_SECTIONS[kind]
    fact = mode == "fact"
    if fact:
        all_rows, by_rec = _fact_rows(con, entity, date_from, date_to)
        sections_cfg = [FACT_FIRST, *sections_cfg, *FACT_LAST]
    else:
        all_rows = [dict(r) for r in con.execute("SELECT * FROM crm_deals WHERE COALESCE(entity, 'deal') = ?", (entity,))]
        if not fcfg.get("include_closed"):
            all_rows = [r for r in all_rows if r["semantic"] not in ("S", "F")]
    snap = max((r["snapshot"] for r in all_rows if r["snapshot"]), default=None)

    by_login = {e["login"]: e.get("department") or "" for e in config.get("employees", [])}
    by_b24 = {str(e["b24_user_id"]): e.get("department") or "" for e in config.get("employees", []) if e.get("b24_user_id")}
    dep_of = lambda r: by_login.get(r["emp"]) if r["emp"] in by_login else by_b24.get(str(r["responsible_id"] or ""), "")
    titles = load_rules().get("departments", {})
    deps_chosen = None if departments is None else set(departments)

    # варианты фильтров — по всему срезу (чтобы выбранный вариант не пропадал из списка), счётчики — по периоду
    deps = {}
    for r in all_rows:
        d = dep_of(r)
        deps.setdefault(d, {"value": d, "name": titles.get(d, d) if d else "Без отдела", "count": 0})
    resp = {}
    for r in all_rows:
        if deps_chosen is not None and dep_of(r) not in deps_chosen:
            continue  # в списке ответственных — только из выбранных отделов
        key = r["emp"] or ("id:" + (r["responsible_id"] or ""))
        resp.setdefault(key, {"value": key, "name": r["responsible"] or "Без ответственного", "count": 0})
    cats = {}
    for r in all_rows:
        cats.setdefault(r["category_id"], {"value": r["category_id"], "name": r["category"], "count": 0})

    if (date_from or date_to) and not fact:
        col = DATE_FIELDS[date_field]
        all_rows = [r for r in all_rows if r[col] and (not date_from or r[col][:10] >= date_from)
                    and (not date_to or r[col][:10] <= date_to)]
    for r in all_rows:
        deps[dep_of(r)]["count"] += 1
        cats[r["category_id"]]["count"] += 1
        key = r["emp"] or ("id:" + (r["responsible_id"] or ""))
        if key in resp:
            resp[key]["count"] += 1

    rows = all_rows if deps_chosen is None else [r for r in all_rows if dep_of(r) in deps_chosen]
    logins = {login} if isinstance(login, str) else set(login or [])  # один ответственный или несколько
    logins.discard("")
    if logins:
        rows = [r for r in rows if (r["emp"] or ("id:" + (r["responsible_id"] or ""))) in logins]
    if category not in (None, ""):
        rows = [r for r in rows if r["category_id"] == str(category)]

    now = dt.datetime.now()
    sections, matched_any, in_funnel = [], set(), set()
    # записи каждой секции; секция с sum_of — сумма перечисленных секций (например, «Поступило» над этапами лидов)
    picked = {}
    for n, sc in enumerate(sections_cfg):
        if sc.get("sum_of"):
            continue
        if fact:
            sel = _fact_pick(sc, rows, by_rec)
            in_funnel |= {r["id"] for r in sel}
            picked[sc.get("key") or n] = sel
            continue
        ids = set(sc.get("stages") or [])
        names = {x.strip().lower() for x in sc.get("stage_names") or []}
        only_cats = {str(c) for c in sc.get("category_ids") or []}  # секция только по этим воронкам Б24
        sel = [r for r in rows if not only_cats or r["category_id"] in only_cats]
        if not sc.get("all_open"):
            sel = [r for r in sel if r["stage_id"] in ids or (r["stage"] or "").strip().lower() in names]
            matched_any |= {r["id"] for r in sel}
        in_funnel |= {r["id"] for r in sel}
        picked[sc.get("key") or n] = sel
    for n, sc in enumerate(sections_cfg):
        if sc.get("sum_of"):
            sel = list({r["id"]: r for key in sc["sum_of"] for r in picked.get(key, [])}.values())
        else:
            sel = picked[sc.get("key") or n]
        by_stage, by_resp = {}, {}
        for r in sel:
            k = (r["category"], r["stage"])
            s = by_stage.setdefault(k, {"category": r["category"], "stage": r["stage"], "sort": r["stage_sort"] or 0, "count": 0, "amount": 0})
            s["count"] += 1
            s["amount"] += r["amount"] or 0
            rk = r["emp"] or r["responsible_id"]
            p = by_resp.setdefault(rk, {"name": r["responsible"] or "Без ответственного", "login": r["emp"],
                                        "value": r["emp"] or ("id:" + (r["responsible_id"] or "")), "count": 0, "amount": 0})
            p["count"] += 1
            p["amount"] += r["amount"] or 0
        ages = [a for a in (_days(r["stage_since"], now) for r in sel) if a is not None]
        deals = sorted(sel, key=lambda r: -(r["amount"] or 0))
        sections.append({
            "key": sc.get("key"), "title": sc.get("title"), "all_open": bool(sc.get("all_open")), "sum_of": sc.get("sum_of"),
            "rule": "все открытые" if sc.get("all_open") else
                    "сумма этапов: " + ", ".join(next((x.get("title") or k for x in sections_cfg if x.get("key") == k), k)
                                                 for k in sc["sum_of"]) if sc.get("sum_of") else
                    "стадии: " + ", ".join(list(sc.get("stage_names") or []) + list(sc.get("stages") or [])),
            "count": len(sel), "amount": sum(r["amount"] or 0 for r in sel),
            "avg_days_on_stage": round(sum(ages) / len(ages)) if ages else None,
            "by_stage": sorted(by_stage.values(), key=lambda x: (x["category"], x["sort"])),
            "by_responsible": sorted(by_resp.values(), key=lambda x: (-x["amount"], -x["count"])),
            "deals": [{"title": r["title"], "client": r["client"], "responsible": r["responsible"], "category": r["category"],
                       "stage": r["stage"], "amount": r["amount"], "days": _days(r["stage_since"], now)} for r in deals[:300]],
        })
    first = sections[0]["count"] if sections else 0
    for i, s in enumerate(sections):
        s["share_of_first"] = s["count"] / first if first else None
        prev = sections[i - 1]["count"] if i else None
        s["share_of_prev"] = s["count"] / prev if prev else None
        s["share_of_total"] = s["count"] / len(in_funnel) if in_funnel else None

    other = {}
    for r in ([] if fact else rows):
        if r["id"] not in matched_any:
            other.setdefault((r["category"], r["stage"]), 0)
            other[(r["category"], r["stage"])] += 1
    return {
        "kind": kind,
        "mode": mode,
        "config_key": cfg_key,
        "snapshot": snap,
        "sections": sections,
        "total": len(in_funnel),
        "total_amount": sum(r["amount"] or 0 for r in rows if r["id"] in in_funnel),
        "other_stages": [{"category": c, "stage": s, "count": n} for (c, s), n in sorted(other.items(), key=lambda x: -x[1])],
        "filters": {
            "responsibles": sorted(resp.values(), key=lambda x: x["name"]),
            # отделы по алфавиту, «Без отдела» — последним
            "departments": sorted(deps.values(), key=lambda x: (x["value"] == "", x["name"])),
            "categories": sorted(cats.values(), key=lambda x: x["name"]),
        },
        "applied": {"login": ",".join(sorted(logins)), "logins": sorted(logins), "category": "" if category is None else str(category),
                    "departments": sorted(deps_chosen) if deps_chosen is not None else [],
                    "date_from": date_from or "", "date_to": date_to or "", "date_field": date_field},
    }
