"""Реализации из 1С (УТ 11) с сотрудником, который ведёт сделку.

Реализации часто оформлены на одного менеджера (поле «Менеджер»), а ведёт сделку другой. Кому засчитать реализацию:
1) ответственному за сделку Битрикс24, в которой указан номер заказа клиента этой реализации (ds["order_managers"]);
2) если такой сделки нет и заказ клиента вырос из тендера (основание ТТК_Тендер) — «Ведущему менеджеру» тендера;
3) иначе — менеджеру реализации. Выручка без НДС — сумма регистра ВыручкаИСебестоимостьПродаж по документу.
"""
import datetime as dt

from .bitrix24 import order_key


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
    lookback = date_from - dt.timedelta(days=ds.get("lookback_days", 365))
    orders = {c["Ref_Key"]: c for c in onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$select": "Ref_Key,Number,Date,ДокументОснование",
        "$filter": f"Date ge datetime'{_when(lookback)}'"})}
    lead_of = order_lead(onec, ds)
    out = []
    for s in sales:
        order = orders.get(s.get("ЗаказКлиента")) or {}
        lead = lead_of(order)
        out.append({
            "id": s["Ref_Key"], "manager": lead or (s.get("Менеджер") or {}).get("Description"),
            "dt": s["Date"][:10], "doc": s["Number"].strip(), "client": (s.get("Контрагент") or {}).get("Description"),
            "signed": s.get("ТТК_ОригиналУПД"), "revenue": revenue.get(s["Ref_Key"]),
        })
    return out


def order_lead(onec, ds):
    """Кто ведёт заказ клиента: ответственный за сделку Б24 с номером заказа (ds["order_managers"]), иначе ведущий
    менеджер тендера-основания. Возвращает функцию заказ (Number, Date, ДокументОснование) → ФИО или None."""
    by_deal = ds.get("order_managers") or {}  # номер заказа (order_key) → [(ФИО ответственного за сделку Б24, дата сделки)]
    tenders = {t["Ref_Key"]: (t.get("ВедущийМенеджер") or {}).get("Description") for t in onec._odata(
        "Document_ТТК_Тендер", {"$format": "json", "$expand": "ВедущийМенеджер",
                                "$select": "Ref_Key,ВедущийМенеджер/Description"})}

    def deal_owner(order):
        # номера заказов каждый год начинаются заново: заказ должен быть создан не раньше чем за 60 дней до сделки
        if not order.get("Number") or not order.get("Date"):
            return None
        od = dt.date.fromisoformat(order["Date"][:10])
        fit = [(d, who) for who, d in by_deal.get(order_key(order["Number"]), [])
               if od >= dt.date.fromisoformat(d) - dt.timedelta(days=60)]
        return max(fit)[1] if fit else None

    return lambda order: deal_owner(order) or tenders.get(order.get("ДокументОснование"))
