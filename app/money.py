"""Вкладка «Деньги» по «вкладка _Деньги_.xlsx» и «Краткому ТЗ»: платёжный календарь на 8 дней, кто должен и когда ждём
деньги, плановые выплаты.

Все суммы приходят из 1С (app/onec_money.py при синхронизации и по кнопке): остатки по счетам и кассам, долги за
отгрузки (таблица receivables), авансы клиентов, заявки на расходование и план оплаты поставщикам. Вручную вводятся
только то, чего в 1С нет: обещанная клиентом дата, перенос плановой даты, согласование, недоступные средства (вычитаются
из остатка) и дополнительные строки. Договорная дата при переносе не меняется.
Календарь — рубли: остаток на начало + поступления по дате «Ожидаем» − согласованные выплаты по «Плану оплаты».
Платежи без даты и несогласованные в календарь не входят и выводятся отдельно с суммами; другие валюты — отдельно.
"""
import datetime as dt

DAYS = 8
IN_TYPES = ["Отгрузка", "Аванс"]
OUT_TYPES = ["Аванс", "Изготовление", "Фин. платёж", "Доставка", "Монтаж", "Сводно", "Прочее"]
APPROVALS = ["Согласовано", "Не согласовано"]
PRIORITIES = ["Обязательный", "Отложить"]


def _date(v):
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _clean_date(v, what):
    v = (v or "").strip() if isinstance(v, str) else v
    if not v:
        return None
    if _date(v) is None:
        raise ValueError(f"«{v}» — не дата ({what})")
    return str(v)[:10]


def _num(v, what, required=False):
    if v in (None, ""):
        if required:
            raise ValueError(f"укажите сумму ({what})")
        return None
    try:
        return round(float(str(v).replace(",", ".").replace(" ", "")), 2)
    except ValueError:
        raise ValueError(f"«{v}» — не число ({what})")


def settings(con):
    s = {r[0]: r[1] for r in con.execute("SELECT key, value FROM money_settings")}
    return {"adjust": float(s["adjust"]) if s.get("adjust") not in (None, "") else 0.0, "asof": s.get("asof") or None,
            "synced_at": s.get("synced_at") or None, "error": s.get("error") or None,
            "client_debt": float(s["client_debt"]) if s.get("client_debt") else None,
            "client_debt_orders": int(s["client_debt_orders"]) if s.get("client_debt_orders") else 0}


def save_settings(con, b):
    asof = _clean_date(b.get("asof"), "дата среза")
    adjust = _num(b.get("adjust"), "недоступные средства") or 0.0
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('adjust', ?)", (str(adjust),))
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('asof', ?)", (asof or "",))


def save_override(con, line_id, plan, approval):
    """Перенос плановой даты и согласование строки из 1С; пустые значения — вернуть данные 1С."""
    plan = _clean_date(plan, "план оплаты")
    if approval and approval not in APPROVALS:
        raise ValueError("недопустимое согласование")
    if plan or approval:
        con.execute("INSERT OR REPLACE INTO money_override VALUES (?, ?, ?)", (str(line_id), plan, approval or None))
    else:
        con.execute("DELETE FROM money_override WHERE id = ?", (str(line_id),))


def save_item(con, b, login):
    kind = b.get("kind")
    if kind not in ("in", "out"):
        raise ValueError("неизвестный вид строки")
    if not (b.get("party") or "").strip():
        raise ValueError("укажите клиента" if kind == "in" else "укажите получателя")
    typ = (b.get("type") or "").strip() or None
    if typ and typ not in (IN_TYPES if kind == "in" else OUT_TYPES):
        raise ValueError(f"«{typ}» — недопустимый вид")
    if kind == "out":
        if b.get("approval") and b["approval"] not in APPROVALS:
            raise ValueError("недопустимое согласование")
        if b.get("priority") and b["priority"] not in PRIORITIES:
            raise ValueError("недопустимый приоритет")
    row = (kind, b["party"].strip(), (b.get("basis") or "").strip() or None, typ, _num(b.get("amount"), "к получению / к оплате", True),
           _num(b.get("owed"), "долг за отгрузку") if kind == "in" else None, _clean_date(b.get("contract"), "по договору"),
           _clean_date(b.get("plan"), "ожидаем" if kind == "in" else "план оплаты"),
           (b.get("approval") or None) if kind == "out" else None, (b.get("priority") or None) if kind == "out" else None,
           login, dt.datetime.now().isoformat(timespec="seconds"))
    cols = "kind, party, basis, type, amount, owed, contract, plan, approval, priority, updated_by, updated_at"
    if b.get("id"):
        con.execute(f"UPDATE money_items SET ({cols}) = ({','.join('?' * 12)}) WHERE id = ?", (*row, int(b["id"])))
    else:
        con.execute(f"INSERT INTO money_items ({cols}) VALUES ({','.join('?' * 12)})", row)


def delete_item(con, item_id):
    con.execute("DELETE FROM money_items WHERE id = ?", (int(item_id),))


def save_expect(con, rec_id, expected):
    expected = _clean_date(expected, "ожидаем")
    if expected:
        con.execute("INSERT OR REPLACE INTO money_expect VALUES (?, ?)", (str(rec_id), expected))
    else:
        con.execute("DELETE FROM money_expect WHERE rec_id = ?", (str(rec_id),))


def build(con, today=None):
    st = settings(con)
    asof = _date(st["asof"]) or today or dt.date.today()
    iso0 = asof.isoformat()
    days = [asof + dt.timedelta(days=i) for i in range(DAYS)]
    last_day = days[-1].isoformat()
    promises = {r[0]: r[1] for r in con.execute("SELECT rec_id, expected FROM money_expect")}
    over = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, plan, approval FROM money_override")}
    cash = [dict(r) for r in con.execute("SELECT account, currency, amount, last FROM money_cash ORDER BY ABS(amount) DESC")]
    rub = lambda c: (c or "руб.") == "руб."

    owe = []  # кто должен: долги за отгрузки из 1С, авансы клиентов, ручные строки
    for r in con.execute("SELECT id, client, doc, amount, due_date FROM receivables WHERE (paid_date IS NULL OR paid_date = '') "
                         "AND amount > 0 ORDER BY due_date, client"):
        due = _date(r["due_date"])
        exp = promises.get(r["id"]) or (r["due_date"] if due else None)  # «Ожидаем» — обещание клиента, иначе «Дата платежа» из правил оплаты заказа
        owe.append({"id": r["id"], "src": "1c", "party": r["client"], "basis": r["doc"], "type": "Отгрузка", "amount": r["amount"],
                    "owed": r["amount"], "contract": r["due_date"], "plan": exp, "promised": r["id"] in promises, "currency": "руб.",
                    "detail": "Дебиторская задолженность: неоплаченный этап графика оплаты заказа " + (r["doc"] or "")})
    for r in con.execute("SELECT * FROM money_plan WHERE kind = 'in' ORDER BY plan, party"):
        plan = promises.get(r["id"]) or r["plan"]
        owe.append({"id": r["id"], "src": "1c", "party": r["party"], "basis": r["basis"], "type": "Аванс", "amount": r["amount"],
                    "owed": 0, "contract": r["contract"], "plan": plan, "promised": r["id"] in promises,
                    "currency": r["currency"] or "руб.", "detail": r["detail"]})
    for r in con.execute("SELECT * FROM money_items WHERE kind = 'in' ORDER BY plan, id"):
        owe.append({"id": r["id"], "src": "manual", "party": r["party"], "basis": r["basis"], "type": r["type"], "amount": r["amount"],
                    "owed": r["owed"] or 0, "contract": r["contract"], "plan": r["plan"], "currency": "руб.",
                    "promised": bool(r["plan"] and r["plan"] != r["contract"]), "detail": "Введено вручную"})
    for o in owe:
        due = _date(o["contract"])
        o["late_days"] = max(0, (asof - due).days) if o["owed"] and due else 0
        o["status"] = "Обещание" if o["promised"] else "По договору" if o["plan"] else "Не обещано"

    pay = []  # плановые выплаты из 1С (заявки, план оплаты поставщикам) и ручные
    for r in con.execute("SELECT * FROM money_plan WHERE kind = 'out' ORDER BY plan, party"):
        plan, approval = over.get(r["id"], (None, None))
        pay.append({"id": r["id"], "src": "1c", "party": r["party"], "basis": r["basis"], "type": r["purpose"], "amount": r["amount"],
                    "contract": r["contract"], "plan": plan or r["plan"], "approval": approval or r["approval"],
                    "priority": r["priority"], "currency": r["currency"] or "руб.", "moved": bool(plan or approval),
                    "detail": f"{r['detail'] or ''} · документ {r['doc'] or '—'}" + (f" от {r['doc_date']}" if r["doc_date"] else "")})
    for r in con.execute("SELECT * FROM money_items WHERE kind = 'out' ORDER BY plan, id"):
        pay.append({"id": r["id"], "src": "manual", "party": r["party"], "basis": r["basis"], "type": r["type"], "amount": r["amount"],
                    "contract": r["contract"], "plan": r["plan"], "approval": r["approval"], "priority": r["priority"],
                    "currency": "руб.", "moved": False, "detail": "Введено вручную"})
    for p in pay:
        p["in_cal"] = bool(p["approval"] == "Согласовано" and p["plan"] and rub(p["currency"]) and iso0 <= p["plan"] <= last_day)
    pay_main = [p for p in pay if p["approval"] == "Согласовано" and p["plan"]]
    pay_other = [p for p in pay if not (p["approval"] == "Согласовано" and p["plan"])]

    cash_rub = round(sum(c["amount"] for c in cash if rub(c["currency"])), 2)
    start = round(cash_rub - st["adjust"], 2)
    cal, bal = [], start
    for d in days:
        iso = d.isoformat()
        inc = round(sum(o["amount"] for o in owe if o["plan"] == iso and rub(o["currency"])), 2)
        out = round(sum(p["amount"] for p in pay_main if p["plan"] == iso and rub(p["currency"])), 2)
        cal.append({"date": iso, "begin": round(bal, 2), "in": inc, "out": out, "end": round(bal + inc - out, 2)})
        bal += inc - out
    low = min(c["end"] for c in cal)
    foreign = {}
    for rows, key, field in ((cash, "cash", "amount"), (owe, "in", "amount"), (pay, "out", "amount")):
        for x in rows:
            if not rub(x["currency"]):
                f = foreign.setdefault(x["currency"], {"currency": x["currency"], "cash": 0.0, "in": 0.0, "out": 0.0})
                f[key] += x[field]
    return {"asof": iso0, "asof_manual": st["asof"], "adjust": st["adjust"], "cash": cash, "cash_rub": cash_rub, "calendar": cal,
            "sync": {"at": st["synced_at"], "error": st["error"], "last_move": max((c["last"] for c in cash), default=None)},
            "tiles": {"start": start, "in": round(sum(c["in"] for c in cal), 2), "out": round(sum(c["out"] for c in cal), 2),
                      "low": low, "shortfall": max(0.0, -low), "low_date": next(c["date"] for c in cal if c["end"] == low)},
            "client_debt": st["client_debt"], "client_debt_orders": st["client_debt_orders"],
            "owe": owe, "owe_total": round(sum(o["owed"] for o in owe), 2),
            "owe_late": round(sum(o["owed"] for o in owe if o["late_days"] > 0), 2),
            "owe_advance": round(sum(o["amount"] for o in owe if o["type"] == "Аванс"), 2),
            "pay": pay_main, "pay_total": round(sum(p["amount"] for p in pay_main if rub(p["currency"])), 2),
            "pay_beyond": round(sum(p["amount"] for p in pay_main if rub(p["currency"]) and p["plan"] > last_day), 2),
            "pay_other": pay_other, "pay_other_total": round(sum(p["amount"] for p in pay_other if rub(p["currency"])), 2),
            "foreign": [{**f, "cash": round(f["cash"], 2), "in": round(f["in"], 2), "out": round(f["out"], 2)} for f in foreign.values()],
            "options": {"in_types": IN_TYPES, "out_types": OUT_TYPES, "approvals": APPROVALS, "priorities": PRIORITIES}}
