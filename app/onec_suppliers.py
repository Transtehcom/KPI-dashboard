"""Вкладка «Поставщики»: закупки, номенклатура и цены из 1С (УТ 11, ТТК) по «ТТК_Поставщики_дашборд_ТЗ.xlsx».

Закупки = поступления (Document_ПриобретениеТоваровУслуг) минус возвраты поставщику (Document_ВозвратТоваровПоставщику).
Суммы — без НДС; цена за единицу = сумма / количество (в базовых единицах, поэтому упаковки не искажают сравнение).
Сравниваются только строки с одной номенклатурой, единицей и валютой. Нет данных — None, а не ноль.
"""
import datetime as dt

SINCE_DAYS = 730  # история цен: два года
STALE_MINUTES = 60
RUB = "руб."


def _day(v):
    return None if not v or str(v).startswith("0001") else str(v)[:10]


def _empty(key):
    return not key or key.startswith("00000000-0000")


def lines_from_docs(docs, kind, partners, items):
    """docs: документы 1С с табличной частью Товары → строки supplier_lines.
    partners: {ключ партнёра: {name, inn}}, items: {ключ номенклатуры: {name, unit}}."""
    out = []
    for d in docs:
        p = partners.get(d.get("Партнер_Key"), {})
        vat_in = bool(d.get("ЦенаВключаетНДС"))
        for r in d.get("Товары") or []:
            qty = r.get("Количество") or 0
            if not qty:
                continue
            amount = (r.get("Сумма") or 0) - ((r.get("СуммаНДС") or 0) if vat_in else 0)
            it = items.get(r.get("Номенклатура_Key"), {})
            ch = r.get("Характеристика_Key")
            out.append({"id": f"{d['Ref_Key']}:{r.get('LineNumber')}", "kind": kind, "doc": (d.get("Number") or "").strip(),
                        "doc_date": _day(d.get("Date")), "partner_key": d.get("Партнер_Key"), "partner": p.get("name") or "Без названия",
                        "inn": p.get("inn") or "", "item_key": r.get("Номенклатура_Key") + ("" if _empty(ch) else ":" + ch),
                        "item": (it.get("name") or "Номенклатура без названия") + ("" if _empty(ch) else " (характеристика)"),
                        "unit": it.get("unit") or "", "qty": float(qty), "amount": round(amount, 2), "currency": d.get("_currency") or RUB})
    return out


def quotes_from_docs(docs, partners, items, units):
    """Заявки в отдел снабжения (до 10 поставщиков в шапке, цена/сумма/срок по каждому в строке) → строки supplier_quotes:
    по строке заявки и поставщику; поставщики без цены (0) в строку заявки не попадают."""
    out = []
    for d in docs:
        sup = [(i, d.get(f"Поставщик{i}_Key")) for i in range(1, 11)]
        sup = [(i, k) for i, k in sup if k and not _empty(k)]
        for r in d.get("ТаблицаТовары") or []:
            ik = r.get("Номенклатура_Key")
            if not sup or not ik or _empty(ik):
                continue
            prices = {i: r.get(f"ЦенаПоставки{i}") or 0 for i, _ in sup}
            pos = [p for p in prices.values() if p > 0]
            best = min(pos) if pos else None
            it = items.get(ik, {})
            unit = units.get(r.get("ЕдиницаИзмерения_Key")) or it.get("unit") or ""
            for i, key in sup:
                p, pr = prices[i], partners.get(key, {})
                if p <= 0:  # без цены в заявке — не показываем
                    continue
                try:
                    term = int(r.get(f"Срок{i}") or 0) or None
                except (TypeError, ValueError):
                    term = None
                out.append({"id": f"{d['Ref_Key']}:{r.get('LineNumber')}:{i}", "req_id": d["Ref_Key"], "req_number": (d.get("Number") or "").strip(),
                            "req_date": _day(d.get("Date")), "partner_key": key, "partner": pr.get("name") or "Без названия",
                            "inn": pr.get("inn") or "", "item_key": ik, "item": it.get("name") or "Номенклатура без названия", "unit": unit,
                            "qty": float(r.get("Количество") or 0), "price": p if p > 0 else None,
                            "amount": (r.get(f"СуммаПоставки{i}") or None) if p > 0 else None, "term_days": term if p > 0 else None,
                            "best": int(p > 0 and p == best), "chosen": int(r.get("ТекущийПоставщик_Key") == key)})
    return out


def store_quotes(con, quotes):
    con.execute("DELETE FROM supplier_quotes")
    cols = ["id", "req_id", "req_number", "req_date", "partner_key", "partner", "inn", "item_key", "item", "unit", "qty", "price", "amount",
            "term_days", "best", "chosen"]
    con.executemany(f"INSERT OR REPLACE INTO supplier_quotes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [[r[c] for c in cols] for r in quotes])


def _quote_item_stats(rows):
    """Строки одной позиции (и единицы) у одного поставщика → показатели; цена — без пересчёта, как ввёл снабженец."""
    priced = sorted((r for r in rows if r["price"]), key=lambda r: (r["req_date"], r["req_number"]))
    prices = [r["price"] for r in priced]
    first, last = (prices[0], prices[-1]) if prices else (None, None)
    return {"requests": len({r["req_id"] for r in rows}), "priced": len(priced), "first": first, "last": last,
            "avg": round(sum(prices) / len(prices), 2) if prices else None, "min": min(prices) if prices else None,
            "change": round(last / first - 1, 4) if first else None, "term": priced[-1]["term_days"] if priced else None,
            "last_date": priced[-1]["req_date"] if priced else None, "qty": priced[-1]["qty"] if priced else None,
            "best": sum(r["best"] for r in rows), "chosen": sum(r["chosen"] for r in rows)}


def quote_suppliers(con, date_from, date_to, not_bought=False):
    """Список поставщиков из заявок за период: заявок, позиций, ответили ценой, лучшая цена, выбран; покупали ли у них."""
    rows = [dict(r) for r in con.execute("SELECT * FROM supplier_quotes WHERE req_date BETWEEN ? AND ?", (date_from, date_to))]
    bought = {r[0] for r in con.execute("SELECT DISTINCT partner_key FROM supplier_lines")}
    by = {}
    for r in rows:
        by.setdefault(r["partner_key"], []).append(r)
    out = []
    for key, rs in by.items():
        if not_bought and key in bought:
            continue
        out.append({"key": key, "name": rs[0]["partner"], "inn": rs[0]["inn"], "requests": len({r["req_id"] for r in rs}),
                    "items": len({r["item_key"] for r in rs}), "priced": sum(1 for r in rs if r["price"]), "best": sum(r["best"] for r in rs),
                    "chosen": sum(r["chosen"] for r in rs), "bought": key in bought, "last": max(r["req_date"] for r in rs)})
    return sorted(out, key=lambda x: (-x["requests"], x["name"]))


def quote_find_partner(con, key=None, inn=None):
    if key:
        r = con.execute("SELECT partner_key FROM supplier_quotes WHERE partner_key=? LIMIT 1", (key,)).fetchone()
    elif inn:
        r = con.execute("SELECT partner_key FROM supplier_quotes WHERE inn=? LIMIT 1", (inn.strip(),)).fetchone()
    else:
        return None
    return r[0] if r else None


def build_quotes(con, partner_key, date_from, date_to, item_key=None):
    """Страница поставщика в режиме «Заявки поставщикам»: плитки, позиции и цены из его предложений, динамика, альтернативы, история."""
    rows = [dict(r) for r in con.execute("SELECT * FROM supplier_quotes WHERE partner_key=? AND req_date BETWEEN ? AND ?", (partner_key, date_from, date_to))]
    name = rows[0]["partner"] if rows else (con.execute("SELECT partner FROM supplier_quotes WHERE partner_key=? LIMIT 1", (partner_key,)).fetchone() or [""])[0]
    bought = bool(con.execute("SELECT 1 FROM supplier_lines WHERE partner_key=? LIMIT 1", (partner_key,)).fetchone())
    groups = {}
    for r in rows:
        groups.setdefault((r["item_key"], r["unit"]), []).append(r)
    items = []
    for (ik, unit), rs in groups.items():
        alt = {a[0] for a in con.execute("SELECT DISTINCT partner_key FROM supplier_quotes WHERE item_key=? AND unit=? AND price IS NOT NULL AND partner_key<>? "
                                         "AND req_date BETWEEN ? AND ?", (ik, unit, partner_key, date_from, date_to))}
        items.append({"key": ik, "item": rs[0]["item"], "unit": unit, **_quote_item_stats(rs), "alternatives": len(alt)})
    items.sort(key=lambda x: (-x["requests"], x["item"]))
    tiles = {"requests": len({r["req_id"] for r in rows}) if rows else None, "items": sum(1 for i in items if i["priced"]) if rows else None,
             "best": sum(r["best"] for r in rows) if rows else None, "chosen": sum(r["chosen"] for r in rows) if rows else None}
    out = {"partner": {"key": partner_key, "name": name, "bought": bought}, "period": [date_from, date_to], "tiles": tiles, "items": items, "meta": meta(con)}
    hist = sorted(rows, key=lambda r: (r["req_date"], r["req_number"]), reverse=True)
    out["history"] = [{"date": r["req_date"], "doc": r["req_number"], "item": r["item"], "unit": r["unit"], "qty": r["qty"], "price": r["price"],
                       "amount": r["amount"], "term": r["term_days"], "best": r["best"], "chosen": r["chosen"]}
                      for r in hist if not item_key or r["item_key"] == item_key]
    if item_key:
        cur = next((i for i in items if i["key"] == item_key), None)
        unit = cur["unit"] if cur else ""
        by_month = {}
        for r in rows:
            if r["item_key"] == item_key and r["price"]:
                by_month.setdefault(r["req_date"][:7], []).append(r["price"])
        out["dynamics"] = [{"month": m, "price": round(sum(v) / len(v), 2)} for m, v in sorted(by_month.items())]
        others = {}
        for a in con.execute("SELECT * FROM supplier_quotes WHERE item_key=? AND unit=? AND price IS NOT NULL AND partner_key<>? AND req_date BETWEEN ? AND ?",
                             (item_key, unit, partner_key, date_from, date_to)):
            others.setdefault(a["partner_key"], []).append(dict(a))
        out["alternatives"] = sorted(({"key": k, "name": rs[0]["partner"], "inn": rs[0]["inn"], **_quote_item_stats(rs)} for k, rs in others.items()),
                                     key=lambda x: (x["last"] is None, x["last"] or 0))
        out["item"] = cur
    return out


def fetch(onec, today=None):
    """Только строки закупок (поступления минус возвраты) — см. fetch_all."""
    return fetch_all(onec, today, quotes=False)[0]


def fetch_all(onec, today=None, quotes=True):
    """Загрузка из 1С: (строки закупок, предложения поставщиков из заявок в отдел снабжения) за SINCE_DAYS;
    ошибка 1С поднимается наверх — прежние данные не затираются."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=SINCE_DAYS)).isoformat()
    q = lambda e, p=None: onec._odata(e, {"$format": "json", **(p or {})})
    currencies = {c["Ref_Key"]: c["Description"] for c in q("Catalog_Валюты")}
    inn = {}
    for k in q("Catalog_Контрагенты", {"$select": "Партнер_Key,ИНН"}):
        if k.get("ИНН") and k.get("Партнер_Key") not in inn:
            inn[k["Партнер_Key"]] = k["ИНН"].strip()
    partners = {p["Ref_Key"]: {"name": p["Description"].strip(), "inn": inn.get(p["Ref_Key"], "")}
                for p in q("Catalog_Партнеры", {"$select": "Ref_Key,Description"})}
    units = {u["Ref_Key"]: u["Description"].strip() for u in q("Catalog_УпаковкиЕдиницыИзмерения", {"$select": "Ref_Key,Description"})}
    items = {n["Ref_Key"]: {"name": n["Description"].strip(), "unit": units.get(n.get("ЕдиницаИзмерения_Key"), "")}
             for n in q("Catalog_Номенклатура", {"$filter": "IsFolder eq false", "$select": "Ref_Key,Description,ЕдиницаИзмерения_Key"})}
    lines = []
    for entity, kind, op in (("Document_ПриобретениеТоваровУслуг", "in", "ХозяйственнаяОперация eq 'ЗакупкаУПоставщика' and "),
                             ("Document_ВозвратТоваровПоставщику", "ret", "")):
        docs = q(entity, {"$filter": op + "DeletionMark eq false and Posted eq true and Date ge datetime'%sT00:00:00'" % since,
                          "$select": "Ref_Key,Number,Date,Партнер_Key,Валюта_Key,ЦенаВключаетНДС,Товары"})
        for d in docs:
            d["_currency"] = currencies.get(d.get("Валюта_Key"), RUB)
        lines += lines_from_docs(docs, kind, partners, items)
    offers = []
    if quotes:
        docs = q("Document_ТТК_ЗаявкаВОтделСнабжения", {
            "$filter": "DeletionMark eq false and Posted eq true and СтатусЗаявки eq 'Отработана' and Date ge datetime'%sT00:00:00'" % since,
            "$select": "Ref_Key,Number,Date," + ",".join(f"Поставщик{i}_Key" for i in range(1, 11)) + ",ТаблицаТовары"})
        offers = quotes_from_docs(docs, partners, items, units)
    return lines, offers


def store(con, lines, now=None):
    now = (now or dt.datetime.now()).isoformat(timespec="seconds")
    con.execute("DELETE FROM supplier_lines")
    cols = ["id", "kind", "doc", "doc_date", "partner_key", "partner", "inn", "item_key", "item", "unit", "qty", "amount", "currency"]
    con.executemany(f"INSERT OR REPLACE INTO supplier_lines ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [[r[c] for c in cols] for r in lines])
    con.execute("INSERT OR REPLACE INTO supplier_meta VALUES ('synced_at', ?)", (now,))
    con.execute("INSERT OR REPLACE INTO supplier_meta VALUES ('error', '')")


def store_error(con, message):
    con.execute("INSERT OR REPLACE INTO supplier_meta VALUES ('error', ?)", (message[:500],))


def meta(con):
    m = dict(con.execute("SELECT key, value FROM supplier_meta").fetchall())
    return {"synced_at": m.get("synced_at") or None, "error": m.get("error") or None}


def is_stale(con, now=None):
    at = meta(con)["synced_at"]
    return not at or dt.datetime.fromisoformat(at) < (now or dt.datetime.now()) - dt.timedelta(minutes=STALE_MINUTES)


def _net(rows):
    """Нетто по строкам: поступления минус возвраты → (количество, сумма, средневзвешенная цена или None)."""
    q = sum(r["qty"] * (1 if r["kind"] == "in" else -1) for r in rows)
    s = sum(r["amount"] * (1 if r["kind"] == "in" else -1) for r in rows)
    return q, round(s, 2), (round(s / q, 2) if q > 1e-9 else None)


def _month_key(d):
    return d[:7]


def _item_stats(rows):
    """Строки одной номенклатуры (и единицы) у одного поставщика → показатели позиции."""
    q, s, avg = _net(rows)
    buys = sorted((r for r in rows if r["kind"] == "in" and r["qty"] > 0), key=lambda r: r["doc_date"])
    price = lambda r: r["amount"] / r["qty"]
    first, last = (price(buys[0]), price(buys[-1])) if buys else (None, None)
    return {"qty": q, "amount": s, "avg": avg, "first": round(first, 2) if first is not None else None,
            "last": round(last, 2) if last is not None else None, "last_date": buys[-1]["doc_date"] if buys else None,
            "change": round(last / first - 1, 4) if first else None, "docs": len({r["doc"] for r in rows})}


def suppliers(con, date_from, date_to):
    """Список поставщиков за период: закупки нетто, позиций, документов."""
    rows = [dict(r) for r in con.execute("SELECT * FROM supplier_lines WHERE currency=? AND doc_date BETWEEN ? AND ?", (RUB, date_from, date_to))]
    by = {}
    for r in rows:
        by.setdefault(r["partner_key"], []).append(r)
    out = []
    for key, rs in by.items():
        out.append({"key": key, "name": rs[0]["partner"], "inn": rs[0]["inn"], "amount": _net(rs)[1],
                    "items": len({r["item_key"] for r in rs}), "docs": len({r["doc"] for r in rs if r["kind"] == "in"}),
                    "last": max(r["doc_date"] for r in rs)})
    return sorted(out, key=lambda x: -x["amount"])


def find_partner(con, key=None, inn=None):
    """Поставщик по ключу или ИНН (из ссылки в карточке Битрикса)."""
    if key:
        r = con.execute("SELECT partner_key FROM supplier_lines WHERE partner_key=? LIMIT 1", (key,)).fetchone()
    elif inn:
        r = con.execute("SELECT partner_key FROM supplier_lines WHERE inn=? LIMIT 1", (inn.strip(),)).fetchone()
    else:
        return None
    return r[0] if r else None


def build(con, partner_key, date_from, date_to, item_key=None):
    """Страница поставщика: плитки, номенклатура и цены, история закупок, динамика цены и альтернативы выбранной позиции."""
    rows = [dict(r) for r in con.execute("SELECT * FROM supplier_lines WHERE partner_key=? AND currency=? AND doc_date BETWEEN ? AND ?",
                                         (partner_key, RUB, date_from, date_to))]
    foreign = con.execute("SELECT COUNT(*) FROM supplier_lines WHERE partner_key=? AND currency<>? AND doc_date BETWEEN ? AND ?",
                          (partner_key, RUB, date_from, date_to)).fetchone()[0]
    name = rows[0]["partner"] if rows else (con.execute("SELECT partner FROM supplier_lines WHERE partner_key=? LIMIT 1", (partner_key,)).fetchone() or [""])[0]
    groups = {}
    for r in rows:
        groups.setdefault((r["item_key"], r["unit"]), []).append(r)
    items = []
    for (ik, unit), rs in groups.items():
        st = _item_stats(rs)
        # альтернативы: тот же товар и единица у других поставщиков за период
        alt = [dict(a) for a in con.execute("SELECT * FROM supplier_lines WHERE item_key=? AND unit=? AND currency=? AND partner_key<>? "
                                            "AND doc_date BETWEEN ? AND ?", (ik, unit, RUB, partner_key, date_from, date_to))]
        items.append({"key": ik, "item": rs[0]["item"], "unit": unit, **st, "alternatives": len({a["partner_key"] for a in alt})})
    items.sort(key=lambda x: -x["amount"])
    total = round(sum(i["amount"] for i in items), 2)
    tiles = {"amount": total if rows else None, "items": len(items) if rows else None,
             "growth": sum(1 for i in items if i["change"] is not None and i["change"] > 0) if rows else None,
             "no_alt": sum(1 for i in items if not i["alternatives"]) if rows else None}
    out = {"partner": {"key": partner_key, "name": name}, "period": [date_from, date_to], "tiles": tiles, "items": items,
           "foreign_lines": foreign, "meta": meta(con)}
    history = sorted(rows, key=lambda r: (r["doc_date"], r["doc"]), reverse=True)
    out["history"] = [{"date": r["doc_date"], "doc": r["doc"], "kind": r["kind"], "item": r["item"], "item_key": r["item_key"], "unit": r["unit"],
                       "qty": r["qty"], "price": round(r["amount"] / r["qty"], 2), "amount": r["amount"]}
                      for r in history if not item_key or r["item_key"] == item_key]
    if item_key:
        cur = next((i for i in items if i["key"] == item_key), None)
        unit = cur["unit"] if cur else ""
        by_month = {}
        for r in rows:
            if r["item_key"] == item_key and r["kind"] == "in":
                by_month.setdefault(_month_key(r["doc_date"]), []).append(r)
        out["dynamics"] = [{"month": m, "price": _net(rs)[2] or round(rs[0]["amount"] / rs[0]["qty"], 2)} for m, rs in sorted(by_month.items())]
        others = {}
        for a in con.execute("SELECT * FROM supplier_lines WHERE item_key=? AND unit=? AND currency=? AND partner_key<>? AND doc_date BETWEEN ? AND ?",
                             (item_key, unit, RUB, partner_key, date_from, date_to)):
            others.setdefault(a["partner_key"], []).append(dict(a))
        out["alternatives"] = sorted(({"key": k, "name": rs[0]["partner"], "inn": rs[0]["inn"], **_item_stats(rs)} for k, rs in others.items()),
                                     key=lambda x: (x["last"] is None, x["last"] or 0))
        out["item"] = cur
    return out
