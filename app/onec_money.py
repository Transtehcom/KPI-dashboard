"""Вкладка «Деньги»: остатки и планы платежей из 1С (УТ 11, ТТК) по «Краткому ТЗ».

Остаток — движения регистров денежных средств (безналичные, наличные, ККМ): приход минус расход по счетам и кассам,
отдельно по валютам; дата последнего движения показывается рядом (OData в 1С — копия базы на прошлый день).
Поступления периода — безналичные поступления ООО «ТрансТехКом» с операцией «Поступление оплаты от клиента»,
проведённые банком; период определяется датой проведения банком.
Плановые выплаты — заявки на расходование денежных средств со статусами «Согласована», «Не согласована» и «К оплате».
Фактические выплаты — сумма заявок, отмеченных «Закрыта», либо полностью покрытых проведёнными документами
«Списание безналичных денежных средств» (связь хранится в строках расшифровки платежа).
Графики оплаты сохраняются за все даты: период календаря выбирается после загрузки.
"""
import datetime as dt

# регистр движений → поле счёта или кассы
CASH_REGISTERS = {"ДенежныеСредстваБезналичные": "БанковскийСчет_Key", "ДенежныеСредстваНаличные": "Касса_Key",
                  "ДенежныеСредстваВКассахККМ": "КассаККМ_Key"}
OPERATIONS = {"ОплатаПоставщику": "Оплата поставщику", "ОплатаПоКредитам": "Платёж по кредиту", "ВыплатаЗарплаты": "Зарплата",
              "ВыдачаДенежныхСредствПодотчетнику": "Выдача подотчётному лицу", "ПеречислениеВБюджет": "Налоги и взносы",
              "ВыдачаЗаймов": "Выдача займа", "ВозвратОплатыКлиенту": "Возврат оплаты клиенту",
              "ПрочаяВыдачаДенежныхСредств": "Прочая выплата"}
# внутренние переводы между своими счетами в поток не входят
INTERNAL_OPERATIONS = {"ПеречислениеНаДругойСчет", "ПеремещениеДенежныхСредств", "ПереводМеждуСвоимиСчетами"}
PLAN_STATUSES = {"Согласована", "НеСогласована", "КОплате"}
APPROVED = {"Согласована", "КОплате"}
ADVANCE_VARIANTS = {"ПредоплатаДоОтгрузки", "АвансДоОбеспечения"}
OPEN_EPS = 0.5
RECEIPT_ORGANIZATION = "ООО «ТрансТехКом»"
CUSTOMER_RECEIPT_OPERATION = "ПоступлениеОплатыОтКлиента"


def _day(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def cash_balances(records, accounts, currencies):
    """records: движения [{RecordType, Сумма, <ключ>_Key, Period}]; accounts: {ключ: {name, currency_key}}.
    Возвращает [{account, currency, amount, last}] по счетам и кассам."""
    out = {}
    for r in records:
        key = r.get("БанковскийСчет_Key") or r.get("Касса_Key") or r.get("КассаККМ_Key") or ""
        row = out.setdefault(key, {"amount": 0.0, "last": ""})
        row["amount"] += (r.get("Сумма") or 0) * (1 if r["RecordType"] == "Receipt" else -1)
        row["last"] = max(row["last"], r.get("Period") or "")
    res = []
    for key, v in out.items():
        a = accounts.get(key, {})
        if abs(v["amount"]) < 0.005:
            continue
        res.append({"account": a.get("name") or "Счёт без названия", "currency": currencies.get(a.get("currency_key"), "руб."),
                    "amount": round(v["amount"], 2), "last": v["last"][:16]})
    return sorted(res, key=lambda x: -abs(x["amount"]))


def open_plan(rows):
    """Открытые этапы плана оплат: приход минус расход по (объект расчётов, документ плана, дата, вариант) больше нуля."""
    net, cur = {}, {}
    for r in rows:
        key = (r["ОбъектРасчетов_Key"], r["ДокументПлан"], _day(r["ДатаПлановогоПогашения"]), r["ВариантОплаты"])
        net[key] = net.get(key, 0.0) + (r.get("КОплате") or 0) * (1 if r["RecordType"] == "Receipt" else -1)
        cur[key] = r.get("Валюта_Key")
    return [{"object": k[0], "doc_key": k[1], "date": k[2], "variant": k[3], "amount": round(v, 2), "currency_key": cur[k]}
            for k, v in net.items() if v > OPEN_EPS and k[2]]


def request_row(r, counterparty, priorities, currencies, today):
    """Заявка на расходование ДС → строка плановых выплат или None (внутренний перевод, дата раньше среза)."""
    if r["ХозяйственнаяОперация"] in INTERNAL_OPERATIONS or r.get("Статус") not in PLAN_STATUSES:
        return None
    plan = _day(r.get("ДатаПлатежа")) or _day(r.get("ЖелательнаяДатаПлатежа"))
    if plan and plan < today:
        return None
    op = r["ХозяйственнаяОперация"]
    return {"id": "rq:" + r["Ref_Key"], "kind": "out", "source": "request", "doc": r["Number"].strip(), "doc_date": _day(r["Date"]),
            "party": counterparty or ("Сотрудники" if op == "ВыплатаЗарплаты" else OPERATIONS.get(op, op)),
            "basis": r["Number"].strip(), "purpose": OPERATIONS.get(op, op), "detail": (r.get("НазначениеПлатежа") or "").strip(),
            "amount": round(r["СуммаДокумента"] or 0, 2), "currency": currencies.get(r.get("Валюта_Key"), "руб."),
            "contract": _day(r.get("ЖелательнаяДатаПлатежа")), "plan": plan,
            "approval": "Согласовано" if r["Статус"] in APPROVED else "Не согласовано",
            "priority": priorities.get(r.get("ПриоритетОплаты_Key"))}


def actual_rows(requests, payments, currencies):
    """Фактические движения для плиток. Заявка считается выплаченной только целиком: явный флаг «Закрыта» либо
    сумма проведённых списаний по ссылкам из РасшифровкаПлатежа покрывает сумму заявки."""
    paid, paid_date = {}, {}
    for p in payments:
        if not p.get("Posted") or p.get("DeletionMark"):
            continue
        for line in p.get("РасшифровкаПлатежа") or []:
            ref = line.get("ЗаявкаНаРасходованиеДенежныхСредств")
            if not ref or ref == "00000000-0000-0000-0000-000000000000":
                continue
            paid[ref] = paid.get(ref, 0.0) + (line.get("Сумма") or 0)
            paid_date[ref] = max(paid_date.get(ref, ""), _day(p.get("Date")) or "")
    out = []
    for r in requests:
        amount = round(r.get("СуммаДокумента") or 0, 2)
        if r.get("ХозяйственнаяОперация") in INTERNAL_OPERATIONS or amount <= 0:
            continue
        full = bool(r.get("Закрыта")) or paid.get(r["Ref_Key"], 0) >= amount - OPEN_EPS
        if not full:
            continue
        day = paid_date.get(r["Ref_Key"]) or _day(r.get("ДатаПлатежа")) or _day(r.get("Date"))
        out.append({"id": "out:" + r["Ref_Key"], "kind": "out", "doc": (r.get("Number") or "").strip(),
                    "dt": day, "amount": amount, "currency": currencies.get(r.get("Валюта_Key"), "руб.")})
    return out


def customer_payment_rows(orders, currencies):
    """Все датированные этапы «Безналичные платежи: к поступлению» из графиков оплаты заказов клиентов.

    Прошедшие даты сохраняются: пользователь может выбрать в платёжном календаре прошлый период.
    """
    out = []
    for o in orders:
        num = (o.get("Number") or "").strip()
        party = (o.get("Контрагент") or {}).get("Description") or "Клиент"
        for i, stage in enumerate(o.get("ЭтапыГрафикаОплаты") or []):
            day = _day(stage.get("ДатаПлатежа"))
            amount = round(stage.get("СуммаПлатежа") or 0, 2)
            if not day or amount <= 0:
                continue
            out.append({"id": f"cp:{o['Ref_Key']}:{i}:{day}", "kind": "in", "source": "customer_plan",
                        "doc": num, "doc_date": _day(o.get("Date")), "party": party, "basis": num or "—",
                        "purpose": "Безналичные платежи: к поступлению", "detail": "Этап графика оплаты заказа клиента",
                        "amount": amount, "currency": currencies.get(o.get("Валюта_Key"), "руб."),
                        "contract": day, "plan": day, "approval": None, "priority": None})
    return out


def receipt_organization_key(organizations):
    """Точное имя организации без различий в кавычках, регистре и пробелах."""
    normalize = lambda v: "".join(str(v or "").translate(str.maketrans("", "", '\"«»„“”')).split()).casefold()
    keys = [r["Ref_Key"] for r in organizations if not r.get("DeletionMark")
            and normalize(r.get("Description")) == normalize(RECEIPT_ORGANIZATION)]
    if len(keys) != 1:
        raise ValueError(f"Не найдена однозначно организация {RECEIPT_ORGANIZATION} для поступлений периода")
    return keys[0]


def customer_receipt_rows(receipts, organization_key, currencies):
    """Фактические оплаты клиентов нужной организации по дате «Проведено банком»."""
    out = []
    for r in receipts:
        if (not r.get("Posted") or not r.get("ПроведеноБанком") or r.get("DeletionMark") or r.get("Организация_Key") != organization_key
                or r.get("ХозяйственнаяОперация") != CUSTOMER_RECEIPT_OPERATION):
            continue
        amount = round(r.get("СуммаДокумента") or 0, 2)
        day = _day(r.get("ДатаПроведенияБанком"))
        if amount > 0 and day:
            out.append({"id": "in:" + r["Ref_Key"], "kind": "in", "doc": (r.get("Number") or "").strip(),
                        "dt": day, "amount": amount, "currency": currencies.get(r.get("Валюта_Key"), "руб.")})
    return out


def fetch(onec, today=None):
    """Загрузка из 1С: {"cash": [...], "plan": [...]}; ошибка 1С поднимается наверх — вызывающий не затирает прежние данные."""
    today = (today or dt.date.today()).isoformat()
    q = lambda e, p=None: onec._odata(e, {"$format": "json", **(p or {})})
    currencies = {c["Ref_Key"]: c["Description"] for c in q("Catalog_Валюты")}
    accounts = {a["Ref_Key"]: {"name": a["Description"].strip(), "currency_key": a.get("ВалютаДенежныхСредств_Key")}
                for a in q("Catalog_БанковскиеСчетаОрганизаций")}
    for k in q("Catalog_Кассы"):
        accounts[k["Ref_Key"]] = {"name": "Касса " + k["Description"].strip(), "currency_key": k.get("ВалютаДенежныхСредств_Key")}
    records = []
    for reg, key in CASH_REGISTERS.items():
        try:
            records += q(f"AccumulationRegister_{reg}_RecordType", {"$filter": "Active eq true",
                                                                   "$select": f"Period,RecordType,Сумма,{key}"})
        except Exception:  # noqa — безналичные обязательны, остальные регистры могут отличаться по полям
            if key == "БанковскийСчет_Key":
                raise
    cash = cash_balances(records, accounts, currencies)

    priorities = {p["Ref_Key"]: p["Description"] for p in q("Catalog_ПриоритетыОплаты")}
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=62)).isoformat()
    reqs = q("Document_ЗаявкаНаРасходованиеДенежныхСредств", {
        "$filter": "DeletionMark eq false and Posted eq true and ДатаПлатежа ge datetime'%sT00:00:00'" % since, "$expand": "Контрагент",
        "$select": "Ref_Key,Number,Date,Статус,ХозяйственнаяОперация,СуммаДокумента,Валюта_Key,НазначениеПлатежа,ЖелательнаяДатаПлатежа,"
                   "ДатаПлатежа,Закрыта,ПриоритетОплаты_Key,Контрагент/Description"})
    plan = []
    for r in reqs:
        row = request_row(r, (r.get("Контрагент") or {}).get("Description"), priorities, currencies, today)
        if row:
            plan.append(row)
    payments = q("Document_СписаниеБезналичныхДенежныхСредств", {
        "$filter": "DeletionMark eq false and Posted eq true and Date ge datetime'%sT00:00:00'" % since,
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,РасшифровкаПлатежа"})
    actual = actual_rows(reqs, payments, currencies)
    organization_key = receipt_organization_key(q("Catalog_Организации"))
    receipts = q("Document_ПоступлениеБезналичныхДенежныхСредств", {
        "$filter": "DeletionMark eq false and Posted eq true and ПроведеноБанком eq true and "
                   f"Организация_Key eq guid'{organization_key}' and ХозяйственнаяОперация eq '{CUSTOMER_RECEIPT_OPERATION}'",
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,ПроведеноБанком,ДатаПроведенияБанком,"
                   "Организация_Key,ХозяйственнаяОперация,СуммаДокумента,Валюта_Key"})
    actual.extend(customer_receipt_rows(receipts, organization_key, currencies))
    cust_orders = {o["Ref_Key"]: o for o in q("Document_ЗаказКлиента", {"$filter": "DeletionMark eq false",
        "$expand": "Контрагент", "$select": "Ref_Key,Number,Date,Статус,ОбъектРасчетов_Key,Валюта_Key,ЭтапыГрафикаОплаты,Контрагент/Description"})}
    debt = client_debt(cust_orders.values(), q("AccumulationRegister_РасчетыСКлиентами/Balance()"))
    plan.extend(customer_payment_rows(cust_orders.values(), currencies))
    return {"cash": cash, "plan": plan, "actual": actual, "debt": debt}


# в сумму «Долга клиента» идут только заказы в этих статусах (решение от 29.10.2026)
COUNT_ORDER_STATUSES = {"ТТК_ВЗакуп"}


def client_debt(orders, balance):
    """Дебиторская задолженность — сумма столбца «Долг клиента» списка «Заказы клиентов» в 1С: остаток «Сумма» регистра
    РасчетыСКлиентами по объекту расчётов заказа; в сумму идут только долги (переплата и аванс в минус не вычитаются),
    учитываются только заказы в статусе «ТТК_ВЗакуп». Возвращает {"amount": ₽, "orders": число заказов с долгом}."""
    by_obj = {}
    for b in balance:
        k = b.get("ОбъектРасчетов_Key")
        by_obj[k] = by_obj.get(k, 0.0) + (b.get("СуммаBalance") or 0)
    debts = [by_obj[o["ОбъектРасчетов_Key"]] for o in orders
             if o.get("Статус") in COUNT_ORDER_STATUSES and by_obj.get(o.get("ОбъектРасчетов_Key"), 0) > 0.005]
    return {"amount": round(sum(debts), 2), "orders": len(debts)}


def store(con, data, now=None):
    """Заменить таблицы остатков и планов свежей выгрузкой и отметить время последней успешной загрузки."""
    now = (now or dt.datetime.now()).isoformat(timespec="seconds")
    con.execute("DELETE FROM money_cash")
    con.executemany("INSERT INTO money_cash (account, currency, amount, last) VALUES (?,?,?,?)",
                    [(c["account"], c["currency"], c["amount"], c["last"]) for c in data["cash"]])
    con.execute("DELETE FROM money_plan")
    cols = ["id", "kind", "source", "doc", "doc_date", "party", "basis", "purpose", "detail", "amount", "currency", "contract", "plan",
            "approval", "priority"]
    con.executemany(f"INSERT OR REPLACE INTO money_plan ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [[p[c] for c in cols] for p in data["plan"]])
    con.execute("DELETE FROM money_actual")
    con.executemany("INSERT OR REPLACE INTO money_actual (id,kind,doc,dt,amount,currency) VALUES (?,?,?,?,?,?)",
                    [(r["id"], r["kind"], r["doc"], r["dt"], r["amount"], r["currency"]) for r in data.get("actual", [])])
    if data.get("debt"):
        con.execute("INSERT OR REPLACE INTO money_settings VALUES ('client_debt', ?)", (str(data["debt"]["amount"]),))
        con.execute("INSERT OR REPLACE INTO money_settings VALUES ('client_debt_orders', ?)", (str(data["debt"]["orders"]),))
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('synced_at', ?)", (now,))
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('error', '')")


def store_error(con, message):
    """Ошибка загрузки: прежние суммы остаются, на странице видна причина и время последней удачной загрузки."""
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('error', ?)", (message[:500],))
