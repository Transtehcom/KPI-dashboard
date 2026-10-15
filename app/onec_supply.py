"""Заказы поставщикам из 1С (УТ 11) с ответственным специалистом снабжения.

Ответственный определяется через заявку в отдел снабжения (ТТК_ЗаявкаВОтделСнабжения):
заказ поставщику → заказ клиента (ТТК_ЗаказКлиента) → его основание (КП или тендер) →
заявка с тем же основанием → ОтветственныйСнабжение. Отменённые заявки не учитываются, если есть другие.
Если по основанию несколько заявок с разными ответственными, заказ ни на кого не относится.

Поля без источника в 1С (брак, пересорт, лот, отсрочка, признаки конкурентности) вводит руководитель в дашборде.
"""
import datetime as dt

EMPTY = "00000000-0000-0000-0000-000000000000"
SUPPLIERS = [f"Поставщик{i}_Key" for i in range(1, 11)]


def _when(d):
    return d.isoformat() + "T00:00:00"


def _date(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def fetch_supplier_orders(onec, ds, date_from, date_to):
    """onec: OneC; ds: настройки набора. Возвращает строки для таблицы supplier_orders с полем manager (ФИО)."""
    lookback = date_from - dt.timedelta(days=ds.get("lookback_days", 365))
    posted = "Posted eq true and DeletionMark eq false"
    orders = onec._odata("Document_ЗаказПоставщику", {
        "$format": "json", "$expand": "Контрагент",
        "$filter": f"{posted} and Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,ДатаПоступления,ТТК_ЗаказКлиента_Key,Контрагент/Description,"
                   "Товары/СуммаСНДС,Товары/СуммаНДС,Товары/Отменено"})
    customer_orders = onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": f"Date ge datetime'{_when(lookback)}'",
        "$select": "Ref_Key,Number,ДокументОснование"})
    requests = onec._odata("Document_ТТК_ЗаявкаВОтделСнабжения", {
        "$format": "json", "$expand": "ОтветственныйСнабжение",
        "$filter": f"DeletionMark eq false and Date ge datetime'{_when(lookback)}'",
        "$select": "Ref_Key,ДокументОснования,СтатусЗаявки,ОтветственныйСнабжение/Description," + ",".join(SUPPLIERS)})
    receipts = onec._odata("Document_ПриобретениеТоваровУслуг", {
        "$format": "json", "$filter": f"{posted} and Date ge datetime'{_when(date_from)}'",
        "$select": "Ref_Key,Date,Товары/ЗаказПоставщику_Key"})
    balance = onec._odata("AccumulationRegister_ЗаказыПоставщикам/Balance()", {
        "$format": "json", "$select": "ЗаказПоставщику_Key,КПоступлениюBalance"})

    co = {c["Ref_Key"]: c for c in customer_orders}
    by_base = {}
    for r in requests:
        by_base.setdefault(r.get("ДокументОснования"), []).append(r)
    last_receipt = {}
    for rc in receipts:
        for line in rc.get("Товары") or []:
            k = line.get("ЗаказПоставщику_Key")
            if k and k != EMPTY:
                last_receipt[k] = max(last_receipt.get(k, ""), rc["Date"][:10])
    to_receive = {}
    for b in balance:
        k = b.get("ЗаказПоставщику_Key")
        to_receive[k] = to_receive.get(k, 0) + (b.get("КПоступлениюBalance") or 0)

    out = []
    for o in orders:
        c = co.get(o.get("ТТК_ЗаказКлиента_Key"))
        reqs = by_base.get(c["ДокументОснование"], []) if c else []
        active = [r for r in reqs if r.get("СтатусЗаявки") != "Отменена"] or reqs
        names = {(r.get("ОтветственныйСнабжение") or {}).get("Description") for r in active} - {None, ""}
        manager = names.pop() if len(names) == 1 else None
        kp_count = max((sum(1 for s in SUPPLIERS if r.get(s) and r[s] != EMPTY) for r in active), default=None)
        lines = [x for x in o.get("Товары") or [] if not x.get("Отменено")]
        out.append({
            "id": o["Ref_Key"], "manager": manager, "doc": o["Number"].strip(), "dt": o["Date"][:10],
            "supplier": (o.get("Контрагент") or {}).get("Description"),
            "amount_novat": round(sum((x.get("СуммаСНДС") or 0) - (x.get("СуммаНДС") or 0) for x in lines), 2),
            "planned_date": _date(o.get("ДатаПоступления")),
            "actual_date": last_receipt.get(o["Ref_Key"]),
            "complete": to_receive.get(o["Ref_Key"], 0) <= 0,
            "customer_order": c["Number"].strip() if c else None,
            "kp_count": kp_count,
        })
    return out
