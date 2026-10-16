"""Реализации из 1С (УТ 11) с сотрудником, который ведёт сделку.

Реализации часто оформлены на одного менеджера (поле «Менеджер»), а ведёт сделку другой. Если реализация выросла
из тендера (реализация → заказ клиента → его основание ТТК_Тендер), она относится к «Ведущему менеджеру» тендера.
Иначе — к менеджеру реализации. Выручка без НДС — сумма регистра ВыручкаИСебестоимостьПродаж по документу.
"""


def _when(d):
    return d.isoformat() + "T00:00:00"


def fetch_sales(onec, ds, date_from, date_to):
    """Строки для таблицы sales с полем manager (ФИО того, кто ведёт сделку)."""
    period = f"ge datetime'{_when(date_from)}' and %s le datetime'{date_to.isoformat()}T23:59:59'"
    sales = onec._odata("Document_РеализацияТоваровУслуг", {
        "$format": "json", "$expand": "Менеджер,Контрагент",
        "$filter": "Posted eq true and DeletionMark eq false and Date " + period % "Date",
        "$select": "Ref_Key,Date,Number,ТТК_ОригиналУПД,ЗаказКлиента,Менеджер/Description,Контрагент/Description"})
    revenue = {}
    for r in onec._odata("AccumulationRegister_ВыручкаИСебестоимостьПродаж_RecordType", {
            "$format": "json", "$select": "Recorder,СуммаВыручкиБезНДС", "$filter": "Period " + period % "Period"}):
        revenue[r["Recorder"]] = revenue.get(r["Recorder"], 0.0) + (r.get("СуммаВыручкиБезНДС") or 0)
    orders = {c["Ref_Key"]: c.get("ДокументОснование") for c in onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$select": "Ref_Key,ДокументОснование,ДокументОснование_Type",
        "$filter": "ДокументОснование_Type eq 'StandardODATA.Document_ТТК_Тендер'"})}
    tenders = {t["Ref_Key"]: (t.get("ВедущийМенеджер") or {}).get("Description") for t in onec._odata(
        "Document_ТТК_Тендер", {"$format": "json", "$expand": "ВедущийМенеджер",
                                "$select": "Ref_Key,ВедущийМенеджер/Description"})}
    out = []
    for s in sales:
        lead = tenders.get(orders.get(s.get("ЗаказКлиента")))
        out.append({
            "id": s["Ref_Key"], "manager": lead or (s.get("Менеджер") or {}).get("Description"),
            "dt": s["Date"][:10], "doc": s["Number"].strip(), "client": (s.get("Контрагент") or {}).get("Description"),
            "signed": s.get("ТТК_ОригиналУПД"), "revenue": revenue.get(s["Ref_Key"]),
        })
    return out
