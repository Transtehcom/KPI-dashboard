"""Customer dates, transport amounts and explicit logistics document links, read-only."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .common import month_bounds
from .onec_logistics import EMPTY, _date, _when

TYPES = {
    "StandardODATA.Document_ЗаказКлиента": "order",
    "StandardODATA.Document_КоммерческоеПредложениеКлиенту": "kp",
    "StandardODATA.Document_ТТК_Тендер": "tender",
    "StandardODATA.Document_ТТК_ЗаявкаВОтделСнабжения": "supply",
}


def transport_amount(lines):
    selected = [line for line in lines if line.get("Флаг")] or lines
    if not selected:
        return None, "В транспортной заявке нет строк расчёта"
    try:
        values = [Decimal(str(line["СтоимостьСНДС"])) for line in selected]
        if not all(value.is_finite() for value in values):
            return None, "В транспортной заявке не заполнена стоимость"
        return float(sum(values).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), None
    except (KeyError, InvalidOperation, TypeError):
        return None, "В транспортной заявке не заполнена стоимость"


def fetch_customer_deliveries(onec, ds, date_from, date_to):
    def read(entity, fields, **params):
        return onec._odata(entity, {"$format": "json", "$select": fields, **params})

    customers = read("Document_ЗаказКлиента",
        "Ref_Key,Number,Date,DeletionMark,Статус,ДокументОснование,ДокументОснование_Type,"
        "ТТК_КонечнаяДатаПоставки,ТТК_ФактическаяДатаПоставки,Партнер/Description", **{"$expand": "Партнер"})
    proposals = read("Document_КоммерческоеПредложениеКлиенту",
        "Ref_Key,Number,Date,ТТК_СтоимостьДоставки,ДокументОснование,ДокументОснование_Type")
    requests = read("Document_ТТК_ЗаявкаВОтделСнабжения",
        "Ref_Key,ДокументОснования,ДокументОснования_Type")
    suppliers = read("Document_ЗаказПоставщику",
        "Ref_Key,DeletionMark,ТТК_ЗаказКлиента_Key,ДокументОснование")
    customers = {row["Ref_Key"]: row for row in customers if not row.get("DeletionMark")}
    proposals_by_key = {row["Ref_Key"]: row for row in proposals}
    edges = {}
    for kind, rows in (("order", customers.values()), ("kp", proposals), ("supply", requests)):
        for row in rows:
            links = []
            for field in ("ДокументОснование", "ДокументОснования"):
                target = TYPES.get(row.get(field + "_Type"))
                if target and row.get(field) and row[field] != EMPTY:
                    links.append((target, row[field]))
            edges[(kind, row["Ref_Key"])] = links

    def ancestors(keys):
        seen, pending = set(), list(keys)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            pending.extend(edges.get(key, []))
        return seen

    by_basis, supplier_customers = {}, {}
    for ref in customers:
        for basis in ancestors([("order", ref)]):
            by_basis.setdefault(basis, set()).add(ref)
    for supplier in suppliers:
        customer = supplier.get("ТТК_ЗаказКлиента_Key")
        if supplier.get("DeletionMark") or customer not in customers:
            continue
        supplier_customers[supplier["Ref_Key"]] = customer
        basis = ("supply", supplier.get("ДокументОснование"))
        if basis in edges:
            for anchor in ancestors([basis]):
                by_basis.setdefault(anchor, set()).add(customer)

    def related_orders(bases):
        # A specific supply request takes precedence over a shared commercial basis.
        for kind in ("order", "supply", "kp", "tender"):
            pending = [base for base in bases if base[0] == kind]
            explicit_order = kind == "order" and bool(pending)
            seen, matches = set(), set()
            while pending:
                base = pending.pop()
                if base in seen:
                    continue
                seen.add(base)
                if base[0] == "order":
                    if base[1] in customers:
                        matches.add(base[1])
                    continue
                resolved = by_basis.get(base)
                if resolved:
                    matches.update(resolved)
                else:
                    pending.extend(edges.get(base, []))
            if matches or explicit_order:
                return matches
        return set()

    # Older transport requests can remain the budget source for a current shipment.
    window = f"Date le datetime'{date_to.isoformat()}T23:59:59'"
    sources = [
        ("forwarder", "Document_ПоручениеЭкспедитору", "ТТК_ОтветственныйЛогист",
         "ТТК_ЗаказКлиента_Key,ТТК_ЗаказПоставщику_Key,Основания,ТТК_СуммаОстаток"),
        ("transport", "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов", "ОтветственныйЛогист",
         "ДокументОснование,ДокументОснование_Type,ДокументОснование2,ДокументОснование2_Type,РезультатыРасчета"),
    ]
    records, by_order = [], {}
    for kind, entity, owner, links in sources:
        rows = read(entity, f"Ref_Key,Number,Date,DeletionMark,{owner}/Description,{links}",
                    **{"$expand": owner, "$filter": window})
        for row in rows:
            if row.get("DeletionMark"):
                continue
            linked = set()
            if kind == "forwarder":
                direct = row.get("ТТК_ЗаказКлиента_Key")
                if direct in customers:
                    linked.add(direct)
                elif not direct or direct == EMPTY:
                    supplier_refs = [row.get("ТТК_ЗаказПоставщику_Key")]
                    supplier_refs += [base.get("Основание") for base in row.get("Основания") or []
                                      if base.get("Основание_Type") == "StandardODATA.Document_ЗаказПоставщику"]
                    linked.update(supplier_customers[ref] for ref in supplier_refs if ref in supplier_customers)
            else:
                bases = [(TYPES[row[field + "_Type"]], row[field]) for field in ("ДокументОснование", "ДокументОснование2")
                         if row.get(field + "_Type") in TYPES and row.get(field)]
                linked.update(related_orders(bases))
            amount, issue = transport_amount(row.get("РезультатыРасчета") or []) if kind == "transport" else (row.get("ТТК_СуммаОстаток"), None)
            record = {"kind": kind, "key": row["Ref_Key"], "doc": (row.get("Number") or "").strip(),
                      "date": row["Date"][:19], "manager": (row.get(owner) or {}).get("Description"),
                      "linked": sorted(linked), "amount": amount, "issue": issue}
            records.append(record)
            for ref in linked:
                by_order.setdefault(ref, []).append(record)

    comparisons = {}
    result = []
    for record in records:
        day = record["date"][:10]
        if not date_from.isoformat() <= day <= date_to.isoformat():
            continue
        for ref in record["linked"] or [None]:
            customer = customers.get(ref, {})
            quote_key = customer.get("ДокументОснование") if customer.get("ДокументОснование_Type") == "StandardODATA.Document_КоммерческоеПредложениеКлиенту" else None
            quote = proposals_by_key.get(quote_key, {})
            cache_key = (ref, day[:7])
            if cache_key not in comparisons:
                cutoff = min(month_bounds(day[:7])[1], date_to).isoformat()
                candidates = [item for item in by_order.get(ref, []) if item["date"][:10] <= cutoff]
                transport = [item for item in candidates if item["kind"] == "transport"]
                comparisons[cache_key] = max(transport or candidates, key=lambda item: (item["date"], item["key"]), default={})
            comparison = comparisons[cache_key]
            result.append({"id": f"{record['kind']}:{record['key']}:{ref or 'unlinked'}",
                "manager": record["manager"], "dt": day,
                "source_kind": record["kind"], "source_key": record["key"], "source_doc": record["doc"],
                "order_key": ref, "order_doc": (customer.get("Number") or "").strip(),
                "order_date": _date(customer.get("Date")), "client": (customer.get("Партнер") or {}).get("Description"),
                "planned_date": _date(customer.get("ТТК_КонечнаяДатаПоставки")),
                "actual_date": _date(customer.get("ТТК_ФактическаяДатаПоставки")),
                "order_status": customer.get("Статус"), "quote_key": quote_key,
                "quote_doc": (quote.get("Number") or "").strip(), "quote_date": _date(quote.get("Date")),
                "delivery_novat": quote.get("ТТК_СтоимостьДоставки"),
                "comparison_kind": comparison.get("kind"), "comparison_key": comparison.get("key"),
                "comparison_doc": comparison.get("doc"), "comparison_date": _date(comparison.get("date")),
                "comparison_amount": comparison.get("amount"), "comparison_issue": comparison.get("issue")})
    return result
