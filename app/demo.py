"""Генератор демо-данных: правдоподобные записи в нормализованных таблицах, чтобы дашборд работал без подключения к Б24 и 1С."""
import datetime as dt
import random

from .common import SYNC_TABLES, current_month, db_lock, month_add, month_bounds, replace_rows, today, weeks_of_month

CLIENTS = ["ООО «СтройИнвест»", "АО «УралТехМонтаж»", "ГБУ «Дирекция капстроя»", "ООО «ЭнергоСервис»", "МУП «Водоканал»",
           "ПАО «Металлург»", "ООО «ТрансЛогистик»", "АО «Нефтехимпроект»", "ООО «ГородСтрой»", "ФГУП «Связьмонтаж»",
           "ООО «Агрохолдинг Восток»", "АО «Медтехника»", "ООО «Альфа-Инжиниринг»", "ГКУ «Управление дорог»",
           "ООО «ТеплоРесурс»", "АО «Машзавод»", "ООО «Северный терминал»", "ООО «ПромВентиляция»"]
SUPPLIERS = ["ООО «Электрокомплект»", "АО «КабельПром»", "ООО «ТД Металлснаб»", "ООО «Насосные системы»", "ООО «Арматура-Урал»",
             "АО «ПромОборудование»", "ООО «Сварка-Центр»", "ООО «ИнжТехСнаб»", "ООО «Профиль-М»", "ООО «Гидравлика»"]
SUBJECTS = ["Уточнение потребности", "Обсуждение КП", "Выход на главного инженера", "Согласование ТЗ", "Сроки закупки",
            "Повторный контакт", "Презентация компании", "Условия оплаты"]

# профиль сотрудника: насколько активно он работает (1.0 — ровно на норме)
PROFILES = {"sokolova": 1.12, "petrov": 0.92, "melnikova": 1.08, "kuznetsov": 1.1, "orlova": 0.95, "zakharov": 1.03,
            "lebedev": 1.05, "fedorova": 0.9}


def _days(first, last, until):
    d, res = first, []
    while d <= min(last, until):
        if d.weekday() < 5:
            res.append(d)
        d += dt.timedelta(days=1)
    return res


def _ts(d, rnd):
    return dt.datetime(d.year, d.month, d.day, rnd.randint(9, 17), rnd.randint(0, 59)).isoformat()


def generate(con, config):
    rnd = random.Random(42)
    t = today()
    months = [month_add(current_month(), -k) for k in range(6, -1, -1)]
    data = {k: [] for k in SYNC_TABLES}
    scores, plans, baselines, snapshots = [], [], [], []
    uid = [0]

    def nid(prefix):
        uid[0] += 1
        return f"{prefix}{uid[0]}"

    for e in config["employees"]:
        pos = e.get("position")
        if not pos:
            continue
        emp = e["login"]
        k = PROFILES.get(emp, 1.0)
        for mi, month in enumerate(months):
            first, last = month_bounds(month)
            if first > t:
                continue
            kk = k * rnd.uniform(0.9, 1.1)
            days = _days(first, last, t)

            # --- звонки и встречи (Б24)
            if pos in ("tender_lead", "tender", "pm"):
                per_day = {"tender_lead": 6, "tender": 7, "pm": 30}[pos]
                p_res = {"tender_lead": 0.62, "tender": 0.55, "pm": 0.6}[pos]
                for d in days:
                    for _ in range(max(0, int(rnd.gauss(per_day * kk, 2)))):
                        good = rnd.random() < p_res * min(kk, 1.15)
                        data["activities"].append({
                            "id": nid("c"), "emp": emp, "dt": _ts(d, rnd), "kind": "call", "direction": "out",
                            "duration": rnd.randint(130, 900) if good else rnd.choice([5, 20, 45, 90, rnd.randint(120, 400)]),
                            "is_lpr": 1 if good or rnd.random() < 0.3 else 0,
                            "secretary": 0 if good else int(rnd.random() < 0.35),
                            "next_step": 1 if good else int(rnd.random() < 0.2), "status": "done",
                            "subject": rnd.choice(SUBJECTS), "client": rnd.choice(CLIENTS)})
                n_meet = {"tender_lead": 5, "tender": 4.5, "pm": 6.5}[pos]
                for _ in range(max(0, round(rnd.gauss(n_meet * kk * len(days) / 21, 1)))):
                    d = rnd.choice(days)
                    closed = d + dt.timedelta(days=0 if rnd.random() < 0.9 else rnd.randint(1, 3))
                    data["activities"].append({
                        "id": nid("m"), "emp": emp, "dt": _ts(d, rnd), "kind": "meeting", "direction": "out", "duration": 3600,
                        "is_lpr": int(rnd.random() < 0.85), "secretary": 0, "next_step": 1, "status": "done",
                        "has_protocol": int(rnd.random() < 0.9), "has_summary": int(rnd.random() < 0.92),
                        "closed_dt": _ts(closed, rnd) if closed <= t else None,
                        "subject": "Встреча: " + rnd.choice(SUBJECTS).lower(), "client": rnd.choice(CLIENTS)})

            # --- торги (Б24)
            if pos in ("tender_lead", "tender"):
                n = int(rnd.gauss(25 * kk, 3) * len(days) / 21)
                for _ in range(max(0, n)):
                    d = rnd.choice(days)
                    in_work = rnd.random() < 0.7
                    pr = in_work and rnd.random() < 0.75
                    sub = d + dt.timedelta(days=rnd.randint(1, 10)) if in_work else None
                    res_d = d + dt.timedelta(days=rnd.randint(10, 35)) if pr else None
                    if res_d and res_d > t:
                        result, res_d = "open", None
                    elif pr:
                        result = "won" if rnd.random() < 0.11 * kk else "lost"
                    else:
                        result = "open"
                    price = round(rnd.uniform(0.4, 9) * 1e6, -3)
                    planned = round(price * rnd.uniform(0.78, 0.93), -3)
                    if result == "won" and rnd.random() < 0.04:
                        planned = round(price * 1.03, -3)  # редкий случай: выиграно ниже себестоимости
                    data["tenders"].append({
                        "id": nid("t"), "emp": emp, "title": f"Закупка № {rnd.randint(10000, 99999)}/{month[2:4]}",
                        "client": rnd.choice(CLIENTS), "created": d.isoformat(), "in_work": int(in_work), "price_review": int(pr),
                        "submitted_date": sub.isoformat() if sub and sub <= t else None, "result": result,
                        "result_date": res_d.isoformat() if res_d else None, "price": price, "planned_cost": planned,
                        "actual_cost": round(planned * rnd.uniform(0.97, 1.03), -3) if result == "won" else None})
                    # тот же тендер в 1С: статус для конверсии, как в «Отчёте ОП»
                    status = {"won": "Выиграно", "lost": "Проиграно"}.get(result) or ("Действует" if in_work else "Отменено")
                    data["tenders_1c"].append({"id": nid("t1"), "emp": emp, "dt": d.isoformat(), "doc": f"ТН-{rnd.randint(100, 999)}",
                                               "client": rnd.choice(CLIENTS), "title": "Поставка оборудования",
                                               "status": status, "amount": price})
                # КП вне тендера (1С)
                if pos == "tender":
                    for _ in range(int(rnd.gauss(108 * kk, 8) * len(days) / 21)):
                        data["kp"].append({"id": nid("k"), "emp": emp, "dt": rnd.choice(days).isoformat(), "client": rnd.choice(CLIENTS),
                                           "amount": round(rnd.uniform(50, 900) * 1e3, -3), "out_of_tender": 1, "confirmed": 1,
                                           "source": "1С"})
                # обеспечения и штрафы (1С)
                for _ in range(rnd.randint(1, 3)):
                    acts = rnd.choice(days)
                    ret = acts + dt.timedelta(days=int(rnd.gauss(24 / kk, 8)))
                    if ret <= t:
                        data["guarantees"].append({"id": nid("g"), "emp": emp, "kind": "contract", "ref": f"Контракт {rnd.randint(100, 999)}",
                                                   "amount": round(rnd.uniform(1, 8) * 1e5, -3), "acts_date": acts.isoformat(),
                                                   "return_date": ret.isoformat()})
                for _ in range(rnd.randint(3, 8)):
                    ub = rnd.choice(days)
                    delay = 1 if rnd.random() < 0.9 * min(kk, 1.08) else rnd.randint(2, 5)
                    ret = ub + dt.timedelta(days=delay + (2 if ub.weekday() == 4 else 0))
                    data["guarantees"].append({"id": nid("g"), "emp": emp, "kind": "application", "ref": f"Заявка {rnd.randint(1000, 9999)}",
                                               "amount": round(rnd.uniform(0.2, 3) * 1e5, -3), "unblock_date": ub.isoformat(),
                                               "return_date": ret.isoformat() if ret <= t else None})
                if rnd.random() < 0.08:
                    data["penalties"].append({"id": nid("p"), "emp": emp, "dt": rnd.choice(days).isoformat(),
                                              "amount": round(rnd.uniform(5, 60) * 1e3, -3), "reason": "Несвоевременное предоставление БГ"})
            if pos == "tender_lead":
                # снимок клиентов на конец каждого месяца; текущее состояние — последний месяц
                n_cl = int(4 + 3 * k + mi * 0.5)
                for i in range(n_cl):
                    row = {"id": f"cl-{emp}-{i}", "emp": emp, "name": CLIENTS[i % len(CLIENTS)],
                           "need": 1, "strategy": int(rnd.random() < 0.85)}
                    snapshots.append((month, row["id"], emp, row["name"], row["need"], row["strategy"]))
                    if month == current_month():
                        data["clients"].append({**row, "updated": t.isoformat()})

            # --- проектный менеджер: сделки, лиды, КП, реализации, дебиторка
            if pos == "pm":
                for _ in range(int(rnd.gauss(42, 5) * len(days) / 21)):
                    d = rnd.choice(days)
                    won = rnd.random() < 0.075 * kk
                    data["deals"].append({"id": nid("d"), "emp": emp, "title": f"Сделка: {rnd.choice(['насосы', 'кабель', 'арматура', 'щиты', 'вентиляция'])}",
                                          "client": rnd.choice(CLIENTS), "created": (d - dt.timedelta(days=rnd.randint(10, 90))).isoformat(),
                                          "closed_date": d.isoformat(), "result": "won" if won else "lost",
                                          "amount": round(rnd.uniform(0.3, 6) * 1e6, -3)})
                for _ in range(int(rnd.gauss(12 * kk, 2) * len(days) / 21)):
                    d = rnd.choice(days)
                    flags = {f: int(rnd.random() < 0.93) for f in ("need", "budget", "lpr", "solvency", "next_step")}
                    data["leads"].append({"id": nid("l"), "emp": emp, "title": rnd.choice(CLIENTS), "first_contact": d.isoformat(),
                                          "qualified_date": _ts(d, rnd), "status": "IN_PROCESS", **flags})
                for d in days:
                    for _ in range(max(0, int(rnd.gauss(16.5 * kk, 2)))):
                        confirmed = int(rnd.random() < 0.93)
                        client = rnd.choice(CLIENTS)
                        data["kp"].append({"id": nid("k"), "emp": emp, "dt": d.isoformat(), "client": client,
                                           "amount": round(rnd.uniform(30, 2000) * 1e3, -3), "out_of_tender": 0,
                                           "confirmed": confirmed, "source": "1С", "ordered": int(rnd.random() < 0.08 * kk)})
                        if confirmed:
                            data["kp_letters"].append({"id": nid("e"), "emp": emp, "dt": _ts(d, rnd), "deal": nid("deal"),
                                                       "subject": f"КП для {client}"})
                for _ in range(rnd.randint(8, 14)):
                    d = rnd.choice(days)
                    rev = round(rnd.uniform(0.3, 3.2) * 1e6, -3)
                    cost = round(rev * rnd.uniform(0.72, 0.9), -3)
                    # подписание обеими сторонами — через 0–20 дней после документа, иногда уже в следующем месяце
                    signed_d = d + dt.timedelta(days=rnd.choice([0, 1, 2, 3, 5, 7, 10, 14, 20]))
                    signed = signed_d <= t and rnd.random() < 0.95
                    docno = f"УПД-{rnd.randint(1000, 9999)}"
                    client = rnd.choice(CLIENTS)
                    data["sales"].append({"id": nid("s"), "emp": emp, "dt": d.isoformat(), "doc": docno, "client": client,
                                          "revenue": rev, "cost": cost, "signed": int(signed),
                                          "signed_date": signed_d.isoformat() if signed else None})
                    due = d + dt.timedelta(days=rnd.choice([10, 14, 21, 30]))
                    late = rnd.random() < 0.1 / kk
                    paid = due + dt.timedelta(days=rnd.randint(1, 20) if late else -rnd.randint(0, 5))
                    data["receivables"].append({"id": nid("r"), "emp": emp, "doc": docno, "client": client, "ship_date": d.isoformat(),
                                                "due_date": due.isoformat(), "paid_date": paid.isoformat() if paid <= t else None,
                                                "amount": round(rev * 1.2, -3)})
                plans.append((emp, month, "gross_profit", round(2.4e6 * rnd.uniform(0.9, 1.1), -4)))

            # --- снабжение: заказы поставщикам, воронка
            if pos == "supply":
                for i in range(int(rnd.gauss(26, 3) * len(days) / 21)):
                    d = rnd.choice(days)
                    big = rnd.random() < 0.45
                    amt = round(rnd.uniform(5, 25) * 1e5 if big else rnd.uniform(0.5, 4.8) * 1e5, -3)
                    plan_d = d + dt.timedelta(days=rnd.randint(5, 25))
                    late = rnd.random() < 0.09 / kk
                    act = plan_d + dt.timedelta(days=rnd.randint(1, 7) if late else -rnd.randint(0, 3))
                    kp_cnt = rnd.choice([3, 3, 3, 4, 5]) if rnd.random() < 0.86 * kk else rnd.choice([1, 2])
                    data["supplier_orders"].append({
                        "id": nid("o"), "emp": emp, "doc": f"ЗП-{rnd.randint(1000, 9999)}", "dt": d.isoformat(),
                        "supplier": rnd.choice(SUPPLIERS), "lot": None, "amount_novat": amt, "planned_date": plan_d.isoformat(),
                        "actual_date": act.isoformat() if act <= t else None, "complete": int(rnd.random() < 0.96),
                        "defect": int(rnd.random() < 0.03), "mis_sort": int(rnd.random() < 0.02),
                        "deferred_share": rnd.choice([0.3, 0.5, 1.0, 1.0]) if rnd.random() < 0.84 * kk else rnd.choice([0, 0.1, 0.2]),
                        "deferral_days": rnd.choice([14, 21, 30, 45]) if rnd.random() < 0.92 else 7,
                        "kp_count": kp_cnt, "relevant": int(rnd.random() < 0.93), "evidence": int(rnd.random() < 0.94),
                        "affiliated": int(rnd.random() < 0.01), "single_source": int(rnd.random() < 0.05),
                        "emergency": int(rnd.random() < 0.04), "justified": int(kp_cnt < 3 and rnd.random() < 0.4),
                        "customer_order": f"ЗК-{rnd.randint(100, 999)}"})
                # иногда закупка единым лотом разбита на несколько заказов (в том числе ниже порога 500 000 ₽)
                if rnd.random() < 0.6:
                    d = rnd.choice(days)
                    lot, sup, order = f"Лот-{nid('')}", rnd.choice(SUPPLIERS), f"ЗК-{rnd.randint(100, 999)}"
                    for _ in range(rnd.randint(2, 3)):
                        plan_d = d + dt.timedelta(days=rnd.randint(5, 20))
                        data["supplier_orders"].append({
                            "id": nid("o"), "emp": emp, "doc": f"ЗП-{rnd.randint(1000, 9999)}", "dt": d.isoformat(),
                            "supplier": sup, "lot": lot, "amount_novat": round(rnd.uniform(2.6, 4.6) * 1e5, -3),
                            "planned_date": plan_d.isoformat(), "actual_date": plan_d.isoformat() if plan_d <= t else None,
                            "complete": 1, "defect": 0, "mis_sort": 0, "deferred_share": 0, "deferral_days": 0,
                            "kp_count": rnd.choice([1, 3]), "relevant": 1, "evidence": 1, "affiliated": 0,
                            "single_source": 0, "emergency": 0, "justified": 0, "customer_order": order})
                for i in range(int(rnd.gauss(20, 3) * len(days) / 21)):
                    d = rnd.choice(days)
                    x = rnd.random() * kk
                    stage = "ordered" if x > 0.85 else "done" if x > 0.2 else "cancelled" if x > 0.08 else "open"
                    data["supply_funnel"].append({"id": nid("f"), "emp": emp, "dt": d.isoformat(), "doc": f"ЗС-{rnd.randint(1000, 9999)}",
                                                  "customer_order": f"ЗК-{rnd.randint(1000, 9999)}" if stage == "ordered" else None,
                                                  "client": None, "stage": stage})

            # --- задачи Б24 (все)
            for _ in range(rnd.randint(12, 22)):
                d = rnd.choice(days)
                dl = dt.datetime(d.year, d.month, d.day, 18, 0)
                late = rnd.random() < 0.08 / kk
                closed = dl + (dt.timedelta(days=rnd.randint(1, 4)) if late else -dt.timedelta(hours=rnd.randint(1, 30)))
                data["tasks"].append({"id": nid("x"), "emp": emp, "title": rnd.choice(["Подготовить договор", "Внести спецификацию в 1С",
                                      "Загрузить сканы первички", "Отчёт по воронке", "Сверка с клиентом", "Обновить карточку клиента"]),
                                      "created": (d - dt.timedelta(days=3)).isoformat(), "deadline": dl.isoformat(),
                                      "closed": closed.isoformat() if closed.date() <= t else None, "status": "done"})

            # --- оценки руководителя (кроме текущей недели)
            crit = ["docs", "tasks"] + (["quality"] if pos in ("tender_lead", "tender") else [])
            for w in weeks_of_month(month):
                wd = dt.date.fromisoformat(w)
                if wd + dt.timedelta(days=4) > t:
                    continue
                for c in crit:
                    sc = 100 if rnd.random() < 0.9 * min(kk, 1.08) else rnd.choice([75, 50])
                    scores.append((emp, w, c, sc, "" if sc == 100 else rnd.choice(["Нет сканов по 2 договорам", "Файлы названы не по шаблону",
                                                                                  "Две задачи закрыты без результата", "Спецификация не подписана"]), "boss"))
        if pos == "pm" and emp != "zakharov":
            baselines.append((emp, "conversion_last_year", 0.065 if emp == "kuznetsov" else 0.07))

    # срез открытых сделок и лидов CRM для вкладки «Воронки»
    pipelines = {
        "0": ("Отдел продаж", "Сделка", "pm", [
            ("NEW", "Выявление потребности", 8), ("UC_SUPPLY", "Обработка отделами снабжения и логистики", 14),
            ("PREPARATION", "Подготовка КП", 12), ("PREPAYMENT_INVOICE", "КП отправлено клиенту", 22),
            ("UC_PRE", "Преддоговорные сделки", 6), ("EXECUTING", "Подписание договора", 5),
            ("UC_DOCS", "Закрывающие документы", 4), ("FINAL_INVOICE", "Ожидает оплаты", 6)]),
        "4": ("Поиск тендеров", "Тендер", "tender", [
            ("C4:NEW", "Черновик", 3), ("C4:PREPARATION", "В работе", 14), ("C4:UC_ASK", "Дозапрос заказчику", 3),
            ("C4:PREPAYMENT_INVOICE", "Экономическое обоснование", 5), ("C4:EXECUTING", "Подготовка документации", 6),
            ("C4:FINAL_INVOICE", "Обеспечение заявки", 3), ("C4:UC_SUBMIT", "Подача на площадке", 4),
            ("C4:UC_WAIT", "Ожидает результатов", 20), ("C4:UC_EXPL", "Разъяснение от заказчика", 2),
            ("C4:UC_PRE", "Преддоговорные сделки", 3)]),
        "2": ("Договорная работа (тендеры)", "Тендер", "tender", [
            ("C2:NEW", "Подписание договора", 3), ("C2:UC_PROD", "В производстве", 5),
            ("C2:UC_SHIP", "В отгрузке и доставке", 4), ("C2:UC_CLAIM", "Рекламации", 1),
            ("C2:UC_DOCS", "Закрывающие документы", 3), ("C2:UC_PAY", "Ожидает оплаты", 6),
            ("C2:UC_OVERDUE", "Просроченная оплата", 2)]),
    }
    lead_statuses = [("NEW", "Не обработан", 4), ("UC_DIST", "Распределённые", 60), ("IN_PROCESS", "В работе", 25),
                     ("UC_CONTACTS", "Поиск контактов", 6), ("UC_CALL", "Ожидают звонка", 30),
                     ("UC_CONTR", "Поиск подрядчиков", 3), ("UC_TEND", "Поиск тендеров", 4),
                     ("UC_MEET", "Переговоры о встрече", 5), ("UC_TKP", "Запросили ТКП", 12)]
    by_group = {"pm": [e for e in config["employees"] if e.get("position") == "pm"],
                "tender": [e for e in config["employees"] if e.get("position") in ("tender", "tender_lead")]}
    extra_owner = {"login": None, "name": "Сергей Волков", "b24_user_id": 25}
    snap = dt.datetime.combine(t, dt.time(9, 0)).isoformat()
    goods = ["насосное оборудование", "кабельная продукция", "запорная арматура", "щитовое оборудование", "вентиляция",
             "металлопрокат"]

    def crm_row(entity, cid, cname, sid, sname, si, owners, title, amount):
        o = rnd.choice(owners + [extra_owner])
        created = t - dt.timedelta(days=rnd.randint(5 + si * 6, 40 + si * 15))
        since = min(t, created + dt.timedelta(days=rnd.randint(1, 4 + si * 5)))
        return {"id": nid("cd"), "entity": entity, "emp": o.get("login"), "responsible_id": str(o.get("b24_user_id", "")),
                "responsible": o["name"], "category_id": cid, "category": cname, "stage_id": sid, "stage": sname,
                "stage_sort": (si + 1) * 10, "semantic": "P", "title": title, "client": rnd.choice(CLIENTS), "amount": amount,
                "created": created.isoformat(), "stage_since": since.isoformat(), "snapshot": snap}

    for cid, (cname, word, group, stages) in pipelines.items():
        for si, (sid, sname, w) in enumerate(stages):
            for _ in range(w):
                data["crm_deals"].append(crm_row("deal", cid, cname, sid, sname, si, by_group[group],
                                                 f"{word}: {rnd.choice(goods)}", round(rnd.lognormvariate(14.2, 0.7), -3)))
    for si, (sid, sname, w) in enumerate(lead_statuses):
        for _ in range(w):
            data["crm_deals"].append(crm_row("lead", "lead", "Лиды", sid, sname, min(si, 3), by_group["pm"] + by_group["tender"],
                                             f"Запрос: {rnd.choice(goods)}", 0))
    # показательный случай для демо: в прошлом месяце у Петрова торги выиграны ниже плановой себестоимости
    prev_first, prev_last = month_bounds(month_add(current_month(), -1))
    for r in data["tenders"]:
        if r["emp"] == "petrov" and r["price_review"] and r["created"] >= (prev_first - dt.timedelta(days=20)).isoformat():
            r.update(result="won", result_date=(prev_first + dt.timedelta(days=17)).isoformat(),
                     planned_cost=round(r["price"] * 1.04, -3), actual_cost=round(r["price"] * 1.05, -3))
            break

    with db_lock():
        for table in SYNC_TABLES:
            replace_rows(con, table, data[table], commit=False)
        con.execute("DELETE FROM clients_monthly")
        con.executemany("INSERT INTO clients_monthly (month, id, emp, name, need, strategy) VALUES (?,?,?,?,?,?)", snapshots)
        con.execute("DELETE FROM manual_scores")
        con.executemany("INSERT OR REPLACE INTO manual_scores (emp, week, criterion, score, comment, author, updated) VALUES (?,?,?,?,?,?,datetime('now'))", scores)
        con.execute("DELETE FROM plans")
        con.executemany("INSERT OR REPLACE INTO plans VALUES (?,?,?,?)", plans)
        con.execute("DELETE FROM baselines")
        con.executemany("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", baselines)
        con.commit()
    return {k: len(v) for k, v in data.items()}
