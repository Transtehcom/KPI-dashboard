"""Дебиторская задолженность из 1С (УТ 11) по заказам клиентов.

Срок оплаты — «Дата платежа» в правилах оплаты заказа (табличная часть ЭтапыГрафикаОплаты; этапов может быть
несколько: предоплата, оплата после отгрузки…). Долг и оплаты — регистр РасчетыСКлиентами по объекту расчётов
заказа, как в отчёте «Карточка расчётов с клиентами»: приход — долг клиента (реализация), расход — оплата
(поступление денег, возврат). Строка таблицы receivables — этап графика оплаты:
  все этапы сохраняются независимо от даты заказа и срока оплаты; до отгрузки остаток долга равен нулю;
  срок оплаты — «Дата платежа» этапа как есть: крайний день оплаты, после него долг просрочен;
  оплачено — день, когда оплаты по заказу покрыли этот этап нарастающим итогом;
  история движений хранит остаток и оплату на дату выбранного периода, неоплаченные этапы продолжают контролироваться.
Ответственный — тот, кто ведёт заказ (как у выручки, onec_sales.order_lead), иначе менеджер заказа.
"""
import json

from .onec_sales import order_lead

EMPTY = "00000000-0000-0000-0000-000000000000"
TOLERANCE = 1.0  # ₽: копеечные расхождения не считаются долгом


def _day(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def _first_reach(events, amount):
    """events: [(день, сумма)] по возрастанию дня; первый день, когда нарастающий итог дошёл до amount."""
    total = 0.0
    for day, v in events:
        total += v
        if total >= amount - TOLERANCE:
            return day
    return None


def stage_rows(stages, moves):
    """stages: [(дата платежа, сумма этапа)]; moves: [(день, «Receipt» | «Expense», сумма)] из РасчетыСКлиентами.
    Возвращает [{n, amount, ship_date, due_date, paid_date}] — этапы, по которым есть отгруженный долг.
    У неоплаченного этапа amount — остаток: оплаты по заказу идут в зачёт этапов по порядку, и частично оплаченный
    этап уменьшается на уже полученное (как «Долг клиента» в «Карточке расчётов с клиентами»)."""
    shipped = sorted((d, v) for d, kind, v in moves if kind == "Receipt" and v)
    paid = sorted((d, v) for d, kind, v in moves if kind == "Expense" and v)
    total_shipped = sum(v for _, v in shipped)
    total_paid = sum(v for _, v in paid)
    out, cum, prev = [], 0.0, 0.0
    for n, (due, amount) in enumerate(sorted(stages), 1):
        cum += amount
        target = min(cum, total_shipped)
        if target - prev < TOLERANCE:
            continue
        ship = _first_reach(shipped, target)
        paid_date = _first_reach(paid, target)
        left = target - prev - min(max(total_paid - prev, 0.0), target - prev)  # неоплаченный остаток этапа
        out.append({"n": n, "amount": round(target - prev if paid_date else left, 2), "ship_date": ship, "due_date": due,
                    "paid_date": paid_date})
        prev = target
    return out


def control_rows(stages, moves):
    """Все этапы графика и их состояние по датам движений, включая ещё не отгруженные.

    История нужна для KPI прошлых месяцев: поздняя оплата не уменьшает долг на прежнюю дату.
    """
    stages = sorted(stages)
    history = {n: [] for n in range(1, len(stages) + 1)}
    for day in sorted({m[0] for m in moves}):
        at = [m for m in moves if m[0] <= day]
        states = {s["n"]: s for s in stage_rows(stages, at)}
        paid = sorted((d, v) for d, kind, v in at if kind == "Expense" and v)
        cumulative = 0.0
        for n, (due, planned) in enumerate(stages, 1):
            cumulative += planned
            state = states.get(n, {"amount": 0.0, "ship_date": None, "paid_date": _first_reach(paid, cumulative)})
            history[n].append({"at": day, **{k: state[k] for k in ("amount", "ship_date", "paid_date")}})
    return [{"n": n, "due_date": due, "planned_amount": planned,
             **({k: history[n][-1][k] for k in ("amount", "ship_date", "paid_date")} if history[n]
                else {"amount": 0.0, "ship_date": None, "paid_date": None}),
             "history": json.dumps(history[n], ensure_ascii=False)}
            for n, (due, planned) in enumerate(stages, 1)]


def state_at(row, asof):
    """Состояние этапа на выбранную дату; старые выгрузки без истории поддерживаются."""
    if row.get("history"):
        states = [s for s in json.loads(row["history"]) if s["at"] <= asof.isoformat()]
        return {**row, **(states[-1] if states else {"amount": 0.0, "ship_date": None, "paid_date": None})}
    return row


def fetch_receivables(onec, ds, date_from, date_to):
    """Строки для таблицы receivables с полем manager (ФИО того, кто ведёт заказ)."""
    orders = onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$expand": "Менеджер,Контрагент",
        "$filter": "Posted eq true and DeletionMark eq false",
        "$select": "Ref_Key,Number,Date,ДокументОснование,ОбъектРасчетов_Key,Менеджер/Description,Контрагент/Description,"
                   "ЭтапыГрафикаОплаты/ДатаПлатежа,ЭтапыГрафикаОплаты/СуммаПлатежа"})
    moves = {}
    for r in onec._odata("AccumulationRegister_РасчетыСКлиентами_RecordType", {
            "$format": "json", "$filter": "Active eq true",
            "$select": "Period,RecordType,Сумма,ОбъектРасчетов_Key"}):
        if r.get("Сумма"):
            moves.setdefault(r["ОбъектРасчетов_Key"], []).append((r["Period"][:10], r["RecordType"], r["Сумма"]))
    lead_of = order_lead(onec, ds)
    out = []
    for o in orders:
        obj = o.get("ОбъектРасчетов_Key")
        stages = [(_day(x.get("ДатаПлатежа")), x.get("СуммаПлатежа") or 0) for x in o.get("ЭтапыГрафикаОплаты") or []]
        stages = [(d, v) for d, v in stages if d and v > 0]
        if not obj or obj == EMPTY or not stages:
            continue
        who = lead_of(o) or (o.get("Менеджер") or {}).get("Description")
        for st in control_rows(stages, moves.get(obj, [])):
            out.append({"id": f"{o['Ref_Key']}-{st['n']}", "manager": who, "doc": o["Number"].strip(),
                        "client": (o.get("Контрагент") or {}).get("Description"), "ship_date": st["ship_date"],
                        "due_date": st["due_date"], "paid_date": st["paid_date"], "amount": st["amount"],
                        "order_date": _day(o.get("Date")), "planned_amount": st["planned_amount"], "history": st["history"]})
    return out
