"""Данные для конверсий как в отчёте 1С УТ «Продажи → Отчёт ОП» (ТТК).

- КП (КоммерческоеПредложениеКлиенту) по менеджеру КП с признаком ordered: есть заказ клиента,
  созданный на основании этого КП («КП → Заказ» в разделе «Отдел продаж»);
- тендеры (ТТК_Тендер) по ведущему менеджеру со статусом («Валидные заявки», «Действует», «Выигран»,
  «Проигран» в разделе «Тендерный отдел»). Валидные — статусы Действует, Выиграно, Проиграно.
"""
EMPTY = "00000000-0000-0000-0000-000000000000"


def _period(date_from, date_to, field="Date"):
    return (f"{field} ge datetime'{date_from.isoformat()}T00:00:00' and "
            f"{field} le datetime'{date_to.isoformat()}T23:59:59'")


def fetch_kp(onec, ds, date_from, date_to):
    """КП за период (проведённые, без пометки удаления) с полем ordered."""
    kps = onec._odata("Document_КоммерческоеПредложениеКлиенту", {
        "$format": "json", "$expand": "Менеджер,Клиент",
        "$filter": "Posted eq true and DeletionMark eq false and " + _period(date_from, date_to),
        "$select": "Ref_Key,Date,Number,СуммаДокумента,Менеджер/Description,Клиент/Description"})
    based = {c["ДокументОснование"] for c in onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": f"DeletionMark eq false and Date ge datetime'{date_from.isoformat()}T00:00:00'",
        "$select": "ДокументОснование"}) if c.get("ДокументОснование") not in (None, "", EMPTY)}
    return [{"id": k["Ref_Key"], "manager": (k.get("Менеджер") or {}).get("Description"), "dt": k["Date"][:10],
             "doc": (k.get("Number") or "").strip(), "client": (k.get("Клиент") or {}).get("Description"),
             "amount": k.get("СуммаДокумента"), "ordered": int(k["Ref_Key"] in based)} for k in kps]


def fetch_tenders(onec, ds, date_from, date_to):
    """Тендеры за период (по дате документа) с ведущим менеджером и статусом."""
    rows = onec._odata("Document_ТТК_Тендер", {
        "$format": "json", "$expand": "ВедущийМенеджер,Клиент",
        "$filter": "DeletionMark eq false and " + _period(date_from, date_to),
        "$select": "Ref_Key,Date,Number,Статус,СуммаДокумента,НаименованиеЛота,ВедущийМенеджер/Description,Клиент/Description"})
    return [{"id": t["Ref_Key"], "manager": (t.get("ВедущийМенеджер") or {}).get("Description"), "dt": t["Date"][:10],
             "doc": (t.get("Number") or "").strip(), "client": (t.get("Клиент") or {}).get("Description"),
             "title": t.get("НаименованиеЛота"), "status": t.get("Статус"), "amount": t.get("СуммаДокумента")}
            for t in rows]
