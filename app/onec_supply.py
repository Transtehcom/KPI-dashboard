"""Заказы поставщикам из 1С (УТ 11) с ответственным специалистом снабжения.

Ответственный определяется через заявку в отдел снабжения (ТТК_ЗаявкаВОтделСнабжения):
заказ поставщику → заказ клиента (ТТК_ЗаказКлиента) → его основание (КП или тендер) →
заявка с тем же основанием → ОтветственныйСнабжение. Отменённые заявки не учитываются, если есть другие.
Если по основанию несколько заявок с разными ответственными, заказ ни на кого не относится.

Плановая дата поставки — «Конечная дата поставки» (ТТК_КонечнаяДатаПоставки) в связанном заказе клиента;
фактическая для KPI 1 — ТТК_ФактическаяДатаПоставки того же заказа (customer_actual_date).
actual_date хранит дату поступления по заказу поставщику отдельно и не подменяет дату клиента.
Месяц KPI 1 определяется конечной датой поставки заказа клиента; без неё заказ не попадает в выборку.

Отсрочка — из «Правил оплаты» заказа поставщику (ЭтапыГрафикаОплаты): строки «от даты заказа» с контролем «Оплата до
отгрузки» или «Оплата после отгрузки»; «% платежа» → доля отсрочки (сумма по таким строкам), «Срок (дн)» → дней отсрочки
(наименьший). Нет таких строк — отсрочки нет (0); нет правил оплаты — ждёт ввода руководителя.
Количество поставщиков для KPI 3 берётся из полей Поставщик 1…10 связанной заявки в отдел снабжения и хранится
в исторически названном поле kp_count. Поля без источника в 1С (брак, пересорт, лот, монополия, аварийность,
утверждённое обоснование) вводит руководитель в дашборде; его ввод важнее выгрузки и для отсрочки.
"""
import datetime as dt

EMPTY = "00000000-0000-0000-0000-000000000000"
SUPPLIERS = [f"Поставщик{i}_Key" for i in range(1, 11)]
# «Контроль» в правилах оплаты: «Оплата до отгрузки» — ПредоплатаДоПоступления, «Оплата после отгрузки» — КредитСдвиг;
# «Оплата независимо от отгрузки» (КредитПослеПоступления) и «Оплата до подтверждения» (АвансДоПодтверждения) — не отсрочка
DEFERRAL_KINDS = {"ПредоплатаДоПоступления", "КредитСдвиг"}


def deferral(stages):
    """(доля отсрочки 0..1, дней) по этапам графика оплаты заказа поставщику; (None, None) — правил оплаты нет."""
    if not stages:
        return None, None
    fit = [x for x in stages if x.get("ВариантОтсчета") == "ОтДатыЗаказа" and x.get("ВариантОплаты") in DEFERRAL_KINDS]
    if not fit:
        return 0.0, 0
    return round(sum(x.get("ПроцентПлатежа") or 0 for x in fit) / 100, 4), min(int(x.get("Сдвиг") or 0) for x in fit)


def _when(d):
    return d.isoformat() + "T00:00:00"


def _date(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def _moment(v):
    return None if not v or str(v).startswith("0001") else str(v)[:19]


def supply_request_context(onec, ds, date_from):
    """Заказы клиентов и заявки снабжения для определения ответственного по основанию."""
    lookback = date_from - dt.timedelta(days=ds.get("lookback_days", 365))
    customer_orders = onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": f"Date ge datetime'{_when(lookback)}'",
        "$select": "Ref_Key,Number,Date,ДокументОснование,ТТК_КонечнаяДатаПоставки,ТТК_ФактическаяДатаПоставки"})
    requests = onec._odata("Document_ТТК_ЗаявкаВОтделСнабжения", {
        "$format": "json", "$expand": "ОтветственныйСнабжение",
        "$filter": f"DeletionMark eq false and Date ge datetime'{_when(lookback)}'",
        "$select": "Ref_Key,Number,ДокументОснования,СтатусЗаявки,ОтветственныйСнабжение/Description," + ",".join(SUPPLIERS)})
    by_base = {}
    for request in requests:
        by_base.setdefault(request.get("ДокументОснования"), []).append(request)
    return {"customers": {c["Ref_Key"]: c for c in customer_orders},
            "by_base": by_base, "by_id": {r["Ref_Key"]: r for r in requests}}


def order_requests(order, context):
    customer = context["customers"].get(order.get("ТТК_ЗаказКлиента_Key"))
    direct = context["by_id"].get(order.get("ДокументОснование"))
    requests = context["by_base"].get(customer.get("ДокументОснование"), []) if customer else []
    if direct:
        requests = [direct]
    return [r for r in requests if r.get("СтатусЗаявки") != "Отменена"] or requests


def fetch_supplier_orders(onec, ds, date_from, date_to):
    """onec: OneC; ds: настройки набора. Возвращает строки для таблицы supplier_orders с полем manager (ФИО)."""
    posted = "Posted eq true and DeletionMark eq false"
    orders = onec._odata("Document_ЗаказПоставщику", {
        "$format": "json", "$expand": "Контрагент",
        "$filter": f"{posted} and Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Автор_Key,ТТК_ЗаказКлиента_Key,ДокументОснование,Контрагент/Description,"
                   "Товары/СуммаСНДС,Товары/СуммаНДС,Товары/Отменено,ЭтапыГрафикаОплаты"})
    context = supply_request_context(onec, ds, date_from)
    receipts = onec._odata("Document_ПриобретениеТоваровУслуг", {
        "$format": "json", "$filter": f"{posted} and Date ge datetime'{_when(date_from)}'",
        "$select": "Ref_Key,Date,Товары/ЗаказПоставщику_Key"})
    balance = onec._odata("AccumulationRegister_ЗаказыПоставщикам/Balance()", {
        "$format": "json", "$select": "ЗаказПоставщику_Key,КПоступлениюBalance"})

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
        c = context["customers"].get(o.get("ТТК_ЗаказКлиента_Key"))
        active = order_requests(o, context)
        names = {(r.get("ОтветственныйСнабжение") or {}).get("Description") for r in active} - {None, ""}
        manager = names.pop() if len(names) == 1 else None
        # Явное правило по автору имеет приоритет перед ответственным заявки.
        manager = ds.get("author_owners", {}).get(o.get("Автор_Key")) or manager
        supplier_count = max((sum(1 for s in SUPPLIERS if r.get(s) and r[s] != EMPTY) for r in active), default=None)
        lines = [x for x in o.get("Товары") or [] if not x.get("Отменено")]
        out.append({
            "id": o["Ref_Key"], "manager": manager, "doc": o["Number"].strip(), "dt": o["Date"][:10],
            "author_key": o.get("Автор_Key"),
            "supplier": (o.get("Контрагент") or {}).get("Description"),
            "amount_novat": round(sum((x.get("СуммаСНДС") or 0) - (x.get("СуммаНДС") or 0) for x in lines), 2),
            "planned_date": _date(c.get("ТТК_КонечнаяДатаПоставки")) if c else None,
            "actual_date": last_receipt.get(o["Ref_Key"]),
            "complete": to_receive.get(o["Ref_Key"], 0) <= 0,
            "customer_order": c["Number"].strip() if c else None,
            "customer_order_key": c["Ref_Key"] if c else None,
            "customer_order_date": _date(c.get("Date")) if c else None,
            "customer_actual_date": _date(c.get("ТТК_ФактическаяДатаПоставки")) if c else None,
            "kp_count": supplier_count,  # историческое имя столбца; фактически поставщики в заявке снабжения
            **dict(zip(("deferred_share", "deferral_days"), deferral(o.get("ЭтапыГрафикаОплаты") or []))),
        })
    return out


# этапы заявки в отдел снабжения для KPI «Конверсия воронки» (как в отчёте 1С «Отчёт ОП», раздел ОС)
REQUEST_STAGES = {"Отработана": "done", "Отменена": "cancelled"}  # остальные статусы — заявка ещё в работе


def fetch_supply_funnel(onec, ds, date_from, date_to):
    """Заявки в отдел снабжения за период (по дате заявки) с ответственным снабжения.
    Заявка «перешла в заказ», если есть заказ клиента с тем же основанием (КП или тендер), что и у заявки;
    как в «Отчёте ОП», считаются и заказы с пометкой удаления.
    stage: ordered — отработана и есть заказ клиента; done — отработана без заказа; open — в работе; cancelled — отменена.
    Для раздела «Метрики» сохраняются статус из 1С, время заявки, закрытия, срок отработки и дата первого заказа клиента."""
    requests = onec._odata("Document_ТТК_ЗаявкаВОтделСнабжения", {
        "$format": "json", "$expand": "ОтветственныйСнабжение",
        "$filter": f"DeletionMark eq false and Date ge datetime'{_when(date_from)}' "
                   f"and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,ДокументОснования,СтатусЗаявки,ДатаЗакрытияЗаявки,СрокОтработкиДо,"
                   "ОтветственныйСнабжение/Description," + ",".join(SUPPLIERS)})
    orders = {}
    for c in onec._odata("Document_ЗаказКлиента", {
            "$format": "json", "$filter": f"Date ge datetime'{_when(date_from)}'",  # с пометкой удаления — тоже, как в «Отчёте ОП»
            "$select": "Number,Date,ДокументОснование"}):
        if c.get("ДокументОснование") and c["ДокументОснование"] != EMPTY:
            orders.setdefault(c["ДокументОснование"], []).append((c["Date"][:10], c["Number"].strip()))
    out = []
    for r in requests:
        stage = REQUEST_STAGES.get(r.get("СтатусЗаявки"), "open")
        found = sorted(orders.get(r.get("ДокументОснования")) or [])
        nums = [n for _, n in found]
        if stage == "done" and nums:
            stage = "ordered"
        out.append({"id": r["Ref_Key"], "manager": (r.get("ОтветственныйСнабжение") or {}).get("Description"),
                    "dt": r["Date"][:10], "doc": r["Number"].strip(), "customer_order": ", ".join(nums) or None,
                    "stage": stage, "status": r.get("СтатусЗаявки"), "created_at": r["Date"][:19],
                    "closed_at": _moment(r.get("ДатаЗакрытияЗаявки")), "due_at": _moment(r.get("СрокОтработкиДо")),
                    "order_dt": found[0][0] if found else None,
                    "suppliers": sum(1 for s in SUPPLIERS if r.get(s) and r[s] != EMPTY)})
    return out
