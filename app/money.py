"""Вкладка «Деньги» по макету «вкладка _Деньги_.xlsx»: платёжный календарь на 8 дней, кто должен и когда ждём деньги,
плановые выплаты.

Долги клиентов за отгрузки берутся из 1С (таблица receivables: этапы графика оплаты заказов без оплаты); срок по
договору — «Дата платежа» этапа. Обещанную клиентом дату («Ожидаем») вводит руководитель. Авансы, плановые выплаты и
остаток на начало вводятся вручную: в 1С их источника в дашборде пока нет.
Поступления в календаре — по дате «Ожидаем», выплаты — по «Плану оплаты» и только «Согласовано».
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
    return {"start": float(s["start"]) if s.get("start") not in (None, "") else 0.0, "asof": s.get("asof") or None}


def save_settings(con, b):
    asof = _clean_date(b.get("asof"), "дата среза")
    start = _num(b.get("start"), "остаток на начало") or 0.0
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('start', ?)", (str(start),))
    con.execute("INSERT OR REPLACE INTO money_settings VALUES ('asof', ?)", (asof or "",))


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
    days = [asof + dt.timedelta(days=i) for i in range(DAYS)]
    promises = {r[0]: r[1] for r in con.execute("SELECT rec_id, expected FROM money_expect")}

    owe = []  # кто должен
    for r in con.execute("SELECT id, client, doc, amount, due_date FROM receivables WHERE (paid_date IS NULL OR paid_date = '') "
                         "AND amount > 0 ORDER BY due_date, client"):
        due = _date(r["due_date"])
        exp = promises.get(r["id"]) or (r["due_date"] if due and due >= asof else None)
        owe.append({"id": r["id"], "src": "1c", "party": r["client"], "basis": r["doc"], "type": "Отгрузка", "amount": r["amount"],
                    "owed": r["amount"], "contract": r["due_date"], "plan": exp, "promised": r["id"] in promises})
    for r in con.execute("SELECT * FROM money_items WHERE kind = 'in' ORDER BY plan, id"):
        owe.append({"id": r["id"], "src": "manual", "party": r["party"], "basis": r["basis"], "type": r["type"], "amount": r["amount"],
                    "owed": r["owed"] or 0, "contract": r["contract"], "plan": r["plan"],
                    "promised": bool(r["plan"] and r["plan"] != r["contract"])})
    for o in owe:
        due = _date(o["contract"])
        o["late_days"] = max(0, (asof - due).days) if o["owed"] and due else 0
        o["status"] = "Обещание" if o["promised"] else "По договору" if o["plan"] else "Не обещано"

    pay = [dict(r, src="manual") for r in con.execute("SELECT * FROM money_items WHERE kind = 'out' ORDER BY plan, id")]

    cal, bal = [], st["start"]
    for d in days:
        iso = d.isoformat()
        inc = round(sum(o["amount"] for o in owe if o["plan"] == iso), 2)
        out = round(sum(p["amount"] for p in pay if p["plan"] == iso and p["approval"] == "Согласовано"), 2)
        cal.append({"date": iso, "begin": round(bal, 2), "in": inc, "out": out, "end": round(bal + inc - out, 2)})
        bal += inc - out
    low = min(c["end"] for c in cal)
    return {"asof": asof.isoformat(), "asof_manual": st["asof"], "start": st["start"], "calendar": cal,
            "tiles": {"start": st["start"], "in": round(sum(c["in"] for c in cal), 2), "out": round(sum(c["out"] for c in cal), 2),
                      "low": low, "shortfall": max(0.0, -low), "low_date": next(c["date"] for c in cal if c["end"] == low)},
            "owe": owe, "owe_total": round(sum(o["owed"] for o in owe), 2),
            "owe_late": round(sum(o["owed"] for o in owe if o["late_days"] > 0), 2),
            "pay": pay, "pay_total": round(sum(p["amount"] for p in pay), 2),
            "options": {"in_types": IN_TYPES, "out_types": OUT_TYPES, "approvals": APPROVALS, "priorities": PRIORITIES}}
