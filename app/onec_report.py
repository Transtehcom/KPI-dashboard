"""Данные для конверсий как в отчёте 1С УТ «Продажи → Отчёт ОП» (ТТК).

- КП (КоммерческоеПредложениеКлиенту) по менеджеру КП с признаком ordered: есть заказ клиента,
  созданный на основании этого КП («КП → Заказ» в разделе «Отдел продаж»);
- тендеры (ТТК_Тендер) по ведущему менеджеру со статусом («Валидные заявки», «Действует», «Выигран»,
  «Проигран» в разделе «Тендерный отдел»). Валидные — статусы Действует, Выиграно, Проиграно.
"""
from .onec_operations import _metadata

EMPTY = "00000000-0000-0000-0000-000000000000"


def _period(date_from, date_to, field="Date"):
    return (f"{field} ge datetime'{date_from.isoformat()}T00:00:00' and "
            f"{field} le datetime'{date_to.isoformat()}T23:59:59'")


def _based_on(onec, date_from):
    """Документы-основания заказов клиентов (КП, тендеры). Как в «Отчёте ОП», считаются и заказы с пометкой удаления."""
    return {c["ДокументОснование"] for c in onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": f"Date ge datetime'{date_from.isoformat()}T00:00:00'",
        "$select": "ДокументОснование"}) if c.get("ДокументОснование") not in (None, "", EMPTY)}


def fetch_kp(onec, ds, date_from, date_to):
    """КП за период (проведённые, без пометки удаления) с полем ordered."""
    kps = onec._odata("Document_КоммерческоеПредложениеКлиенту", {
        "$format": "json", "$expand": "Менеджер,Клиент",
        "$filter": "Posted eq true and DeletionMark eq false and " + _period(date_from, date_to),
        "$select": "Ref_Key,Date,Number,СуммаДокумента,Менеджер/Description,Клиент/Description"})
    based = _based_on(onec, date_from)
    return [{"id": k["Ref_Key"], "manager": (k.get("Менеджер") or {}).get("Description"), "dt": k["Date"][:10],
             "doc": (k.get("Number") or "").strip(), "client": (k.get("Клиент") or {}).get("Description"),
             "amount": k.get("СуммаДокумента"), "ordered": int(k["Ref_Key"] in based)} for k in kps]


def tender_title_field(onec):
    """Published tender title field for the connected 1C configuration."""
    warnings = []
    metadata = _metadata(onec, warnings)
    for warning in warnings:
        getattr(onec, "log", lambda _: None)(warning)
    available = metadata.get("Document_ТТК_Тендер", set()) if metadata is not None else None
    return next((name for name in ("НаименованиеЛота", "Наименование")
                 if available is None or name in available), None)


def fetch_tenders(onec, ds, date_from, date_to):
    """Тендеры за период (по дате документа) с ведущим менеджером, статусом и признаком ordered — есть заказ клиента."""
    title_field = tender_title_field(onec)
    fields = ["Ref_Key", "Date", "Number", "Статус", "СуммаДокумента",
              "ВедущийМенеджер/Description", "Клиент/Description"]
    if title_field:
        fields.append(title_field)
    rows = onec._odata("Document_ТТК_Тендер", {
        "$format": "json", "$expand": "ВедущийМенеджер,Клиент",
        "$filter": "DeletionMark eq false and " + _period(date_from, date_to),
        "$select": ",".join(fields)})
    based = _based_on(onec, date_from)
    return [{"id": t["Ref_Key"], "manager": (t.get("ВедущийМенеджер") or {}).get("Description"), "dt": t["Date"][:10],
             "doc": (t.get("Number") or "").strip(), "client": (t.get("Клиент") or {}).get("Description"),
             "title": t.get(title_field) if title_field else None, "status": t.get("Статус"), "amount": t.get("СуммаДокумента"),
             "ordered": int(t["Ref_Key"] in based)} for t in rows]
