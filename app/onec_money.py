"""Вкладка «Деньги»: остатки и планы платежей из 1С (УТ 11, ТТК) по «Краткому ТЗ».

Остаток — движения регистров денежных средств (безналичные, наличные, ККМ): приход минус расход по счетам и кассам,
отдельно по валютам; дата последнего движения показывается рядом (OData в 1С — копия базы на прошлый день).
Плановые выплаты:
  заявки на расходование денежных средств с датой платежа не раньше среза — «Согласована» и «К оплате» считаются
  согласованными, «Не согласована» — нет; платёжные документы на заявки не ссылаются, поэтому оплаченные раньше
  заявки с прошлой датой не берутся;
  план оплаты поставщикам (регистр РасчетыСПоставщикамиПланОплат) — открытые этапы (приход минус расход) с датой
  не раньше среза. Заявки на них в 1С не связаны, поэтому они «не согласованы» и в календарь не входят.
Авансы клиентов — открытые этапы плана оплаты клиентов «до отгрузки» / «до обеспечения» (регистр РасчетыСКлиентамиПланОплат);
этапы после отгрузки — долг за отгрузку, он берётся из дебиторки.
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
APPROVED = {"Согласована", "КОплате"}
ADVANCE_VARIANTS = {"ПредоплатаДоОтгрузки", "АвансДоОбеспечения"}
OPEN_EPS = 0.5


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
    if r["ХозяйственнаяОперация"] in INTERNAL_OPERATIONS:
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
    reqs = q("Document_ЗаявкаНаРасходованиеДенежныхСредств", {
        "$filter": "DeletionMark eq false and Posted eq true and ДатаПлатежа ge datetime'%sT00:00:00'" % today, "$expand": "Контрагент",
        "$select": "Ref_Key,Number,Date,Статус,ХозяйственнаяОперация,СуммаДокумента,Валюта_Key,НазначениеПлатежа,ЖелательнаяДатаПлатежа,"
                   "ДатаПлатежа,ПриоритетОплаты_Key,Контрагент/Description"})
    plan = []
    for r in reqs:
        row = request_row(r, (r.get("Контрагент") or {}).get("Description"), priorities, currencies, today)
        if row:
            plan.append(row)
    sel = "Period,RecordType,ОбъектРасчетов_Key,Валюта_Key,ДокументПлан,ДатаПлановогоПогашения,ВариантОплаты,КОплате"
    sup_orders = {o["Ref_Key"]: o for o in q("Document_ЗаказПоставщику", {"$expand": "Контрагент",
                                                                         "$select": "Ref_Key,Number,Date,Контрагент/Description"})}
    for s in open_plan(q("AccumulationRegister_РасчетыСПоставщикамиПланОплат_RecordType", {"$filter": "Active eq true", "$select": sel})):
        if s["date"] < today:
            continue
        o = sup_orders.get(s["doc_key"], {})
        num = (o.get("Number") or "").strip()
        plan.append({"id": f"sp:{s['doc_key']}:{s['date']}:{s['variant']}", "kind": "out", "source": "supplier_plan", "doc": num, "doc_date": _day(o.get("Date")),
                     "party": (o.get("Контрагент") or {}).get("Description") or "Поставщик", "basis": num or "—",
                     "purpose": "Аванс" if "Предоплата" in s["variant"] or "Аванс" in s["variant"] else "Оплата по графику",
                     "detail": "План оплаты заказа поставщику (" + s["variant"] + ")", "amount": s["amount"],
                     "currency": currencies.get(s["currency_key"], "руб."), "contract": s["date"], "plan": s["date"],
                     "approval": "Не согласовано", "priority": None})
    cust_orders = {o["Ref_Key"]: o for o in q("Document_ЗаказКлиента", {"$filter": "DeletionMark eq false",
                                                                       "$expand": "Контрагент", "$select": "Ref_Key,Number,Date,Контрагент/Description"})}
    for s in open_plan(q("AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType", {"$filter": "Active eq true", "$select": sel})):
        if s["variant"] not in ADVANCE_VARIANTS or s["date"] < today:  # давние открытые этапы — не оплаты, а недозакрытый план
            continue
        o = cust_orders.get(s["doc_key"], {})
        num = (o.get("Number") or "").strip()
        plan.append({"id": f"cp:{s['doc_key']}:{s['date']}:{s['variant']}", "kind": "in", "source": "customer_plan", "doc": num, "doc_date": _day(o.get("Date")),
                     "party": (o.get("Контрагент") or {}).get("Description") or "Клиент", "basis": num or "—", "purpose": "Аванс",
                     "detail": "План оплаты заказа клиента (" + s["variant"] + ")", "amount": s["amount"],
                     "currency": currencies.get(s["currency_key"], "руб."), "contract": s["date"], "plan": s["date"],
                     "approval": None, "priority": None})
    return {"cash": cash, "plan": plan}


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
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('synced_at', ?)", (now,))
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('error', '')")


def store_error(con, message):
    """Ошибка загрузки: прежние суммы остаются, на странице видна причина и время последней удачной загрузки."""
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('error', ?)", (message[:500],))
