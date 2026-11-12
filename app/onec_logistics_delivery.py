"""Customer delivery dates, expected cost and explicit logistics links, read-only."""
from .onec_logistics import EMPTY, _date, _when

TYPES = {
    "StandardODATA.Document_ЗаказКлиента": "order",
    "StandardODATA.Document_КоммерческоеПредложениеКлиенту": "kp",
    "StandardODATA.Document_ТТК_Тендер": "tender",
    "StandardODATA.Document_ТТК_ЗаявкаВОтделСнабжения": "supply",
}


def fetch_customer_deliveries(onec, ds, date_from, date_to):
    def read(entity, fields, **params):
        return onec._odata(entity, {"$format": "json", "$select": fields, **params})

    customers = read("Document_ЗаказКлиента",
        "Ref_Key,Number,Date,DeletionMark,Статус,ДокументОснование,ДокументОснование_Type,"
        "ТТК_КонечнаяДатаПоставки,ТТК_ФактическаяДатаПоставки,ТТК_СуммаДоставкиБезНДС,Партнер/Description", **{"$expand": "Партнер"})
    proposals = read("Document_КоммерческоеПредложениеКлиенту",
        "Ref_Key,ДокументОснование,ДокументОснование_Type")
    requests = read("Document_ТТК_ЗаявкаВОтделСнабжения",
        "Ref_Key,ДокументОснования,ДокументОснования_Type")
    suppliers = read("Document_ЗаказПоставщику",
        "Ref_Key,DeletionMark,ТТК_ЗаказКлиента_Key,ДокументОснование")
    customers = {row["Ref_Key"]: row for row in customers if not row.get("DeletionMark")}
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

    window = f"Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'"
    sources = [
        ("forwarder", "Document_ПоручениеЭкспедитору", "ТТК_ОтветственныйЛогист",
         "ТТК_ЗаказКлиента_Key,ТТК_ЗаказПоставщику_Key,Основания"),
        ("transport", "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов", "ОтветственныйЛогист",
         "ДокументОснование,ДокументОснование_Type,ДокументОснование2,ДокументОснование2_Type"),
    ]
    records = []
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
            record = {"kind": kind, "key": row["Ref_Key"], "doc": (row.get("Number") or "").strip(),
                      "date": row["Date"][:19], "manager": (row.get(owner) or {}).get("Description"),
                      "linked": sorted(linked)}
            records.append(record)

    result = []
    for record in records:
        day = record["date"][:10]
        if not date_from.isoformat() <= day <= date_to.isoformat():
            continue
        for ref in record["linked"] or [None]:
            customer = customers.get(ref, {})
            result.append({"id": f"{record['kind']}:{record['key']}:{ref or 'unlinked'}",
                "manager": record["manager"], "dt": day,
                "source_kind": record["kind"], "source_key": record["key"], "source_doc": record["doc"],
                "order_key": ref, "order_doc": (customer.get("Number") or "").strip(),
                "order_date": _date(customer.get("Date")), "client": (customer.get("Партнер") or {}).get("Description"),
                "planned_date": _date(customer.get("ТТК_КонечнаяДатаПоставки")),
                "actual_date": _date(customer.get("ТТК_ФактическаяДатаПоставки")),
                "order_status": customer.get("Статус"),
                "expected_delivery_novat": customer.get("ТТК_СуммаДоставкиБезНДС"),
                # The actual MP amount is computed by the 1C form and is absent from OData.
                "actual_delivery_novat": None})
    return result
