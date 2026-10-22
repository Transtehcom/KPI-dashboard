"""Дебиторская задолженность из 1С (УТ 11) по заказам клиентов.

Срок оплаты — «Дата платежа» в правилах оплаты заказа (табличная часть ЭтапыГрафикаОплаты; этапов может быть
несколько: предоплата, оплата после отгрузки…). Долг и оплаты — регистр РасчетыСКлиентами по объекту расчётов
заказа, как в отчёте «Карточка расчётов с клиентами»: приход — долг клиента (реализация), расход — оплата
(поступление денег, возврат). Строка таблицы receivables — этап графика оплаты:
  сумма этапа ограничена отгруженным: пока не отгружено, долга нет и просрочки тоже;
  срок оплаты — «Дата платежа» этапа как есть: крайний день оплаты, после него долг просрочен;
  оплачено — день, когда оплаты по заказу покрыли этот этап нарастающим итогом.
Ответственный — тот, кто ведёт заказ (как у выручки, onec_sales.order_lead), иначе менеджер заказа.
"""
import datetime as dt

from .onec_sales import order_lead

EMPTY = "00000000-0000-0000-0000-000000000000"
TOLERANCE = 1.0  # ₽: копеечные расхождения не считаются долгом


def _when(d):
    return d.isoformat() + "T00:00:00"


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
    Возвращает [{n, amount, ship_date, due_date, paid_date}] — этапы, по которым есть отгруженный долг."""
    shipped = sorted((d, v) for d, kind, v in moves if kind == "Receipt" and v)
    paid = sorted((d, v) for d, kind, v in moves if kind == "Expense" and v)
    total_shipped = sum(v for _, v in shipped)
    out, cum, prev = [], 0.0, 0.0
    for n, (due, amount) in enumerate(sorted(stages), 1):
        cum += amount
        target = min(cum, total_shipped)
        if target - prev < TOLERANCE:
            continue
        ship = _first_reach(shipped, target)
        out.append({"n": n, "amount": round(target - prev, 2), "ship_date": ship, "due_date": due,
                    "paid_date": _first_reach(paid, target)})
        prev = target
    return out


def fetch_receivables(onec, ds, date_from, date_to):
    """Строки для таблицы receivables с полем manager (ФИО того, кто ведёт заказ)."""
    since = date_from - dt.timedelta(days=ds.get("lookback_days", 365))
    orders = onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$expand": "Менеджер,Контрагент",
        "$filter": f"Posted eq true and DeletionMark eq false and Date ge datetime'{_when(since)}'",
        "$select": "Ref_Key,Number,Date,ДокументОснование,ОбъектРасчетов_Key,Менеджер/Description,Контрагент/Description,"
                   "ЭтапыГрафикаОплаты/ДатаПлатежа,ЭтапыГрафикаОплаты/СуммаПлатежа"})
    moves = {}
    for r in onec._odata("AccumulationRegister_РасчетыСКлиентами_RecordType", {
            "$format": "json", "$filter": f"Period ge datetime'{_when(since)}' and Active eq true",
            "$select": "Period,RecordType,Сумма,ОбъектРасчетов_Key"}):
        if r.get("Сумма"):
            moves.setdefault(r["ОбъектРасчетов_Key"], []).append((r["Period"][:10], r["RecordType"], r["Сумма"]))
    lead_of = order_lead(onec, ds)
    out = []
    for o in orders:
        obj = o.get("ОбъектРасчетов_Key")
        stages = [(_day(x.get("ДатаПлатежа")), x.get("СуммаПлатежа") or 0) for x in o.get("ЭтапыГрафикаОплаты") or []]
        stages = [(d, v) for d, v in stages if d and v > 0]
        if not obj or obj == EMPTY or not stages or obj not in moves:
            continue
        who = lead_of(o) or (o.get("Менеджер") or {}).get("Description")
        for st in stage_rows(stages, moves[obj]):
            out.append({"id": f"{o['Ref_Key']}-{st['n']}", "manager": who, "doc": o["Number"].strip(),
                        "client": (o.get("Контрагент") or {}).get("Description"), "ship_date": st["ship_date"],
                        "due_date": st["due_date"], "paid_date": st["paid_date"], "amount": st["amount"]})
    return out
