"""Логистика из 1С (УТ 11, ТТК) для раздела «Метрики → Логистика»:
- заявки на расчёт транспортных расходов (Продажи → Заявка на расчёт транспортных расходов,
  документ ТТК_ЗаявкаНаРасчетТранспортныхРасходов);
- поручения экспедитору (Склад и доставка → Поручения экспедиторам, документ ПоручениеЭкспедитору): статус ТТК_Статус,
  плановая дата отгрузки ТТК_ДатаОтгрузки, ответственный логист ТТК_ОтветственныйЛогист. План, ₽ — «Ожидаемая МП →
  Сумма доставки (с НДС)» связанного заказа клиента (ТТК_СуммаДоставки). Факт, ₽ — сумма с НДС строк доставки
  (номенклатура DELIVERY_PREFIXES) в заказах поставщику по этому заказу клиента; «Фактическая МП → Сумма доставки» из формы
  заказа не берётся: в базе её нет, а форма относит к доставке и товары. Истории статусов в поручении нет.

Берутся все заявки периода в любом статусе, в том числе непроведённые и с пометкой удаления (признаки сохраняются).
Ответственный — «Ответственный логист»; логисты, которых нет в дашборде, сохраняются как «1c:ФИО» (onec.KEEP_UNMAPPED).
history — история статусов [[статус, дата установки]] по времени. Маршрут, перевозчик и стоимость — по вариантам
расчёта, отмеченным флажком (несколько отмеченных — плечи одной перевозки, стоимость суммируется); без флажков — первый.
"""
import datetime as dt
import json


def _when(d):
    return d.isoformat() + "T00:00:00"


def _text(v):
    return " ".join(str(v or "").split())


def _route(results):
    """(маршрут, перевозчик, стоимость с НДС) по выбранным вариантам расчёта."""
    chosen = [x for x in results if x.get("Флаг")] or results[:1]
    if not chosen:
        return None, None, None
    legs, carriers = [], []
    for x in chosen:
        src, dst = _text(x.get("АдресОтправки")), _text(x.get("АдресДоставки"))
        leg = " → ".join(p for p in (src, dst) if p)
        if leg and leg not in legs:
            legs.append(leg)
        c = _text(x.get("ТранспортнаяКомпания"))
        if c and c not in carriers:
            carriers.append(c)
    return "; ".join(legs) or None, ", ".join(carriers) or None, round(sum(x.get("СтоимостьСНДС") or 0 for x in chosen), 2)


KP_TYPE = "StandardODATA.Document_КоммерческоеПредложениеКлиенту"


def kp_deadlines(onec, since):
    """{КП клиенту: «Срок действия»} — в заявке на расчёт ТР «Отработать до» показывает срок действия КП-основания."""
    return {k["Ref_Key"]: _date(k.get("СрокДействия")) and str(k["СрокДействия"])[:19] for k in onec._odata(
        "Document_КоммерческоеПредложениеКлиенту", {
            "$format": "json", "$filter": f"Date ge datetime'{_when(since)}'", "$select": "Ref_Key,СрокДействия"})}


def fetch_transport_requests(onec, ds, date_from, date_to):
    rows = onec._odata("Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов", {
        "$format": "json", "$expand": "ОтветственныйЛогист,Менеджер",
        "$filter": f"Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,Статус,АдресДоставки,ИсторияСтатусов,РезультатыРасчета,"
                   "ДокументОснование,ДокументОснование_Type,ОтветственныйЛогист/Description,Менеджер/Description"})
    deadlines = kp_deadlines(onec, date_from - dt.timedelta(days=ds.get("lookback_days", 365)))
    out = []
    for r in rows:
        hist = sorted(([h["Статус"], h["ДатаУстановки"][:19]] for h in r.get("ИсторияСтатусов") or []
                       if h.get("Статус") and h.get("ДатаУстановки") and not h["ДатаУстановки"].startswith("0001")),
                      key=lambda x: x[1])
        route, carrier, cost = _route(r.get("РезультатыРасчета") or [])
        out.append({"id": r["Ref_Key"], "manager": (r.get("ОтветственныйЛогист") or {}).get("Description"),
                    "dt": r["Date"][:10], "created_at": r["Date"][:19], "doc": (r.get("Number") or "").strip(),
                    "status": r.get("Статус"), "posted": r.get("Posted"), "deleted": r.get("DeletionMark"),
                    "sales_manager": (r.get("Менеджер") or {}).get("Description"), "address": r.get("АдресДоставки"),
                    "due_at": deadlines.get(r.get("ДокументОснование")) if r.get("ДокументОснование_Type") == KP_TYPE else None,
                    "history": json.dumps(hist, ensure_ascii=False), "route": route, "carrier": carrier, "cost": cost})
    return out


# номенклатура доставки — по началу названия («Грунт-эмаль … транспортно-желтый» и «Металлопрокат … без доставки» — не она)
DELIVERY_PREFIXES = ("транспортн", "доставк", "автодоставк", "перевозк", "услуги по перевозке", "возмещение транспортных",
                     "организация доставки")
EMPTY = "00000000-0000-0000-0000-000000000000"


def delivery_facts(onec, since):
    """{заказ клиента: сумма с НДС строк доставки в его заказах поставщику} (заказы без пометки удаления, строки не отменены)."""
    noms = {n["Ref_Key"] for n in onec._odata("Catalog_Номенклатура", {"$format": "json", "$select": "Ref_Key,Description"})
            if _text(n.get("Description")).lower().startswith(DELIVERY_PREFIXES)}
    out = {}
    for s in onec._odata("Document_ЗаказПоставщику", {
            "$format": "json", "$filter": f"DeletionMark eq false and Date ge datetime'{_when(since)}'",
            "$select": "ТТК_ЗаказКлиента_Key,Товары/Номенклатура_Key,Товары/СуммаСНДС,Товары/Отменено"}):
        co = s.get("ТТК_ЗаказКлиента_Key")
        if not co or co == EMPTY:
            continue
        for x in s.get("Товары") or []:
            if x.get("Номенклатура_Key") in noms and not x.get("Отменено"):
                out[co] = round(out.get(co, 0) + (x.get("СуммаСНДС") or 0), 2)
    return out


def _date(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def fetch_forwarder_orders(onec, ds, date_from, date_to):
    since = date_from - dt.timedelta(days=ds.get("lookback_days", 365))
    delivery = {c["Ref_Key"]: c.get("ТТК_СуммаДоставки") for c in onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": f"Date ge datetime'{_when(since)}'", "$select": "Ref_Key,ТТК_СуммаДоставки"})}
    facts = delivery_facts(onec, since)
    rows = onec._odata("Document_ПоручениеЭкспедитору", {
        "$format": "json", "$expand": "ТТК_ОтветственныйЛогист,ТТК_ТранспортнаяКомпания",
        "$filter": f"Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,ТТК_Статус,ТТК_ДатаОтгрузки,ТТК_ДатаПоставки,ТТК_ДОП,ТТК_ДФО,ТТК_ДПП,ТТК_ДФП,ТТК_ЗаказКлиента_Key,"
                   "ТТК_АдресОтгрузки,ТТК_АдресДоставки,АдресДоставки,ТТК_ОтветственныйЛогист/Description,"
                   "ТТК_ТранспортнаяКомпания/Description"})
    out = []
    for r in rows:
        src, dst = _text(r.get("ТТК_АдресОтгрузки")), _text(r.get("ТТК_АдресДоставки") or r.get("АдресДоставки"))
        out.append({"id": r["Ref_Key"], "manager": (r.get("ТТК_ОтветственныйЛогист") or {}).get("Description"),
                    "dt": r["Date"][:10], "created_at": r["Date"][:19], "doc": (r.get("Number") or "").strip(),
                    "status": r.get("ТТК_Статус") or None, "posted": r.get("Posted"), "deleted": r.get("DeletionMark"),
                    "ship_date": _date(r.get("ТТК_ДатаОтгрузки")), "delivery_date": _date(r.get("ТТК_ДатаПоставки")),
                    "dop": _date(r.get("ТТК_ДОП")), "dof": _date(r.get("ТТК_ДФО")),  # ДОП/ДОФ — отгрузка план/факт
                    "dpp": _date(r.get("ТТК_ДПП")), "dfp": _date(r.get("ТТК_ДФП")),  # ДПП/ДФП — поставка план/факт
                    "plan_sum": delivery.get(r.get("ТТК_ЗаказКлиента_Key")) or None,
                    "fact_sum": facts.get(r.get("ТТК_ЗаказКлиента_Key")) or None,
                    "carrier": _text((r.get("ТТК_ТранспортнаяКомпания") or {}).get("Description")) or None,
                    "route": " → ".join(p for p in (src, dst) if p) or None})
    return out
