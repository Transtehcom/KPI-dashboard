"""Раздел «Метрики»: где сейчас находится работа отдела (по макету Dashboard_TTK.xlsx).

Каждая единица работы в каждый момент стоит ровно на одном этапе. Путь собирается из дат в 1С:

Снабжение
  заявка в отдел снабжения (ТТК_ЗаявкаВОтделСнабжения):
    «На расчёте»           — с даты заявки до закрытия (Отработана/Отменена); просрочена, если прошёл «Срок отработки до»;
    «Расчёт передан»       — отработанная заявка с даты закрытия до первого заказа клиента по её основанию;
                             если заказа нет handed_days дней — заявка выходит из очереди;
    «Закупка согласуется»  — с даты заказа клиента до первого заказа поставщику по нему; без заказа поставщику
                             approve_days дней — выходит из очереди (заказ мог быть закрыт со склада);
  заказ поставщику (ЗаказПоставщику):
    «Заказ размещён»       — с даты заказа, пока до плановой даты больше ship_soon_days дней или она не задана;
    «Ожидается отгрузка»   — плановая дата ближе ship_soon_days дней или уже прошла, поступления ещё нет;
                             просрочен, если плановая дата («Конечная дата поставки» в заказе клиента) прошла.
  Этап заканчивается поступлением (УПД/ТН по заказу поставщику).

Логистика — заявки на расчёт транспортных расходов из 1С (таблица transport_requests), этапы как в макете:
  «На расчёте»            — статус «Поступила» или «В работе»; вход и выход — по истории статусов заявки;
  «На согласовании», «Подготовка отгрузки», «В пути», «Доставлено, не закрыто» — источника данных пока нет.
  «Всего заявок за период» — заявки с датой в периоде в любом статусе, включая помеченные на удаление.
  Очередь — на сегодня, по заявкам окна синхронизации (since), без помеченных на удаление.
"""
import datetime as dt
import json

from .onec import UNMAPPED

SUPPLY_STAGES = [("calc", "На расчёте"), ("handed", "Расчёт передан"), ("approve", "Закупка согласуется"),
                 ("placed", "Заказ размещён"), ("shipping", "Ожидается отгрузка")]
TRANSPORT_STATUSES = {"Поступила": "Поступила", "ВРаботе": "В работе", "ЗапросИнформации": "Запрос информации",
                      "Отработана": "Отработана"}
LOGISTICS_STAGES = [("calc", "На расчёте"), ("approve", "На согласовании"), ("prepare", "Подготовка отгрузки"),
                    ("transit", "В пути"), ("delivered", "Доставлено, не закрыто")]
LOGISTICS_SOURCED = {"calc"}  # этапы, для которых в 1С есть данные
CALC_STATUSES = {"Поступила", "ВРаботе"}
DEFAULTS = {"handed_days": 30, "approve_days": 30, "ship_soon_days": 7}
CLOSED = {"Отработана", "Отменена"}
STATUS_TITLES = {"Поступила": "Поступила", "ВРаботе": "В работе",
                 "ЗапросПроектнойДокументации": "Запрос проектной документации"}


def _day(v):
    return dt.date.fromisoformat(v[:10]) if v else None


def _plus(d, days):
    return d + dt.timedelta(days=days) if d else None


def request_path(r, supplier_dates, p):
    """Этапы заявки: [(stage, начало, конец или None, просрочено ли сейчас)]. supplier_dates — даты заказов
    поставщику по номерам заказов клиента."""
    created, closed = _day(r["created_at"] or r["dt"]), _day(r["closed_at"])
    if r["status"] in CLOSED and not closed:
        closed = created  # закрыта без даты закрытия — на расчёте не числится
    out = [("calc", created, closed if r["status"] in CLOSED else None)]
    if r["status"] != "Отработана":
        return out
    order = _day(r["order_dt"])
    start = closed
    if not order or order > start:
        out.append(("handed", start, order or _plus(start, p["handed_days"])))
        if not order:
            return out
        start = order
    nums = [x.strip() for x in (r["customer_order"] or "").split(",") if x.strip()]
    sup = min((d for n in nums for d in supplier_dates.get(n, [])), default=None)
    if not sup or sup > start:
        out.append(("approve", start, sup or _plus(start, p["approve_days"])))
    return out


def order_path(o, p):
    placed, plan, got = _day(o["dt"]), _day(o["planned_date"]), _day(o["actual_date"])
    soon = _plus(plan, -p["ship_soon_days"])
    out = []
    if not soon or soon > placed:
        end = min([x for x in (soon, got) if x], default=None)
        out.append(("placed", placed, end))
    if soon and (not got or soon < got):
        out.append(("shipping", max(placed, soon), got))
    return out


def supply(con, names, date_from, date_to, today=None, now=None, params=None):
    """Панель снабжения: этапы на сегодня, движение за период [date_from, date_to], очередь по сотрудникам, реестры."""
    p = {**DEFAULTS, **(params or {})}

    def name(login):  # «1c:ФИО» — ответственный из 1С, которого нет в дашборде
        if login and login.startswith(UNMAPPED):
            return login[len(UNMAPPED):] or "Не назначен"
        return names.get(login) or login or "Не назначен"

    today = today or dt.date.today()
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    a, b = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    reqs = [dict(r) for r in con.execute("SELECT * FROM supply_funnel")]
    for r in reqs:  # выгрузка до появления раздела: статуса нет, есть только этап воронки
        if not r["status"]:
            r["status"] = {"done": "Отработана", "ordered": "Отработана", "cancelled": "Отменена"}.get(r["stage"], "ВРаботе")
    orders = [dict(r) for r in con.execute("SELECT * FROM supplier_orders")]
    supplier_dates = {}
    for o in orders:
        if o["customer_order"]:
            supplier_dates.setdefault(o["customer_order"].strip(), []).append(_day(o["dt"]))

    segs = []  # (stage, emp, начало, конец, просрочено)
    for r in reqs:
        late = r["status"] not in CLOSED and bool(r["due_at"]) and r["due_at"] < now
        for st, s, e in request_path(r, supplier_dates, p):
            segs.append((st, r["emp"], s, e, late and st == "calc"))
    for o in orders:
        plan = _day(o["planned_date"])
        for st, s, e in order_path(o, p):
            segs.append((st, o["emp"], s, e, st == "shipping" and not o["actual_date"] and plan < today))

    def current(s, e):
        return s <= today and (e is None or e > today)

    stages = []
    for key, title in SUPPLY_STAGES:
        mine = [x for x in segs if x[0] == key]
        now_ = [x for x in mine if current(x[2], x[3])]
        stages.append({"key": key, "title": title, "now": len(now_),
                       "came": sum(1 for x in mine if a <= x[2] <= b and x[2] <= today),
                       "left": sum(1 for x in mine if x[3] and a <= x[3] <= b and x[3] <= today),
                       "max_days": max(((today - x[2]).days for x in now_), default=None),
                       "overdue": sum(1 for x in now_ if x[4])})

    people = {}
    for st, emp, s, e, late in segs:
        if not current(s, e):
            continue
        row = people.setdefault(emp, {"login": emp, "name": name(emp),
                                      "stages": {k: 0 for k, _ in SUPPLY_STAGES}, "overdue": 0, "total": 0})
        row["stages"][st] += 1
        row["overdue"] += late
        row["total"] += 1

    open_reqs = []
    for r in reqs:
        if r["status"] in CLOSED or _day(r["created_at"] or r["dt"]) > today:
            continue
        late_days = (today - _day(r["due_at"])).days if r["due_at"] and r["due_at"] < now else 0
        open_reqs.append({"doc": r["doc"], "emp": name(r["emp"]), "created": (r["created_at"] or r["dt"])[:16],
                          "status": STATUS_TITLES.get(r["status"], r["status"] or "—"), "due": (r["due_at"] or "")[:16] or None,
                          "late_days": late_days})
    open_reqs.sort(key=lambda x: (-x["late_days"], x["created"]))

    registry = []
    for o in orders:
        got, plan = _day(o["actual_date"]), _day(o["planned_date"])
        if got and not a <= got <= b and not a <= _day(o["dt"]) <= b:
            continue  # поставлено вне периода и размещено вне периода
        if not got and _day(o["dt"]) > today:
            continue
        registry.append({"doc": o["doc"], "supplier": o["supplier"], "emp": name(o["emp"]),
                         "amount": o["amount_novat"], "dt": o["dt"], "plan": o["planned_date"], "fact": o["actual_date"],
                         "late_days": None if not plan else max(0, ((got or today) - plan).days)})
    registry.sort(key=lambda x: (x["fact"] is not None, -(x["late_days"] or 0), x["plan"] or "9999"))

    return {
        "date_from": date_from, "date_to": date_to, "today": today.isoformat(), "params": p,
        "tiles": {"in_work": sum(s["now"] for s in stages),
                  "received": sum(1 for r in reqs if a <= _day(r["created_at"] or r["dt"]) <= b),
                  "done_calc": sum(1 for r in reqs if r["status"] == "Отработана" and r["closed_at"]
                                   and a <= _day(r["closed_at"]) <= b),
                  "done_orders": sum(1 for o in orders if o["actual_date"] and a <= _day(o["actual_date"]) <= b),
                  "overdue": sum(s["overdue"] for s in stages)},
        "stages": stages,
        "people": sorted(people.values(), key=lambda x: (x["name"] == "Не назначен", x["name"])),
        "requests": open_reqs, "orders": registry,
    }


def calc_spans(r):
    """Интервалы «На расчёте» по истории статусов: [(вход, выход или None)] — даты ГГГГ-ММ-ДДTчч:мм:сс."""
    hist = json.loads(r.get("history") or "[]") or [[r["status"], r["created_at"] or r["dt"]]]
    spans, start = [], None
    for st, at in hist:
        inside = st in CALC_STATUSES
        if inside and start is None:
            start = at
        elif not inside and start is not None:
            spans.append((start, at))
            start = None
    if start is not None:
        spans.append((start, None))
    return spans


def logistics(con, names, date_from, date_to, since=None, today=None):
    """Логистика по макету: плитки, этапы (сейчас / поступило / вышло / макс. дней / просрочено), очередь по логистам,
    реестр перевозок. Очередь — на сегодня по заявкам с датой не раньше since (окно синхронизации)."""
    today = today or dt.date.today()
    a, b = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    allrows = [dict(r) for r in con.execute("SELECT * FROM transport_requests ORDER BY created_at DESC")]
    period = [r for r in allrows if date_from <= r["dt"] <= date_to]

    def name(login):
        if login and login.startswith(UNMAPPED):
            return login[len(UNMAPPED):] or "Не назначен"
        return names.get(login) or login or "Не назначен"

    live = [r for r in allrows if not r["deleted"] and (not since or r["dt"] >= since)]
    queue = [r for r in live if r["status"] in CALC_STATUSES]
    came = left = 0
    for r in live:
        for start, end in calc_spans(r):
            came += a <= _day(start) <= b and _day(start) <= today
            left += bool(end) and a <= _day(end) <= b
    calc = {"key": "calc", "title": "На расчёте", "sourced": True, "now": len(queue), "came": came, "left": left,
            "max_days": max(((today - _day(calc_spans(r)[-1][0])).days for r in queue if calc_spans(r)), default=None),
            "overdue": None}
    stages = [calc if k == "calc" else {"key": k, "title": t, "sourced": False} for k, t in LOGISTICS_STAGES]

    people = {}
    for r in queue:
        p = people.setdefault(r["emp"], {"name": name(r["emp"]), "stages": {}, "total": 0})
        p["stages"]["calc"] = p["stages"].get("calc", 0) + 1
        p["total"] += 1
    # «Всего заявок за период» по статусам: столбцы — в порядке работы с заявкой, незнакомые — в конце
    by_status = {}
    for r in period:
        st = TRANSPORT_STATUSES.get(r["status"], r["status"] or "—")
        by_status[st] = by_status.get(st, 0) + 1
    order = list(TRANSPORT_STATUSES.values())
    statuses = [[st, by_status.get(st, 0)] for st in order + sorted(x for x in by_status if x not in order)]
    registry = [{"doc": r["doc"], "dt": r["created_at"] or r["dt"], "status": TRANSPORT_STATUSES.get(r["status"], r["status"]),
                 "route": r.get("route") or r.get("address"), "carrier": r.get("carrier"), "logist": name(r["emp"]),
                 "plan": r.get("cost")} for r in period if not r["deleted"]]
    return {"available": bool(allrows), "date_from": date_from, "date_to": date_to, "today": today.isoformat(),
            "tiles": {"total": len(period), "deleted": sum(1 for r in period if r["deleted"]), "in_work": len(queue)},
            "statuses": statuses, "stages": stages, "sourced": sorted(LOGISTICS_SOURCED),
            "people": sorted(people.values(), key=lambda x: (x["name"] == "Не назначен", -x["total"], x["name"])),
            "registry": registry}
