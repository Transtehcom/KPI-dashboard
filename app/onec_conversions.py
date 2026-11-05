"""Customer orders with independent sales and supply ownership for KPI conversions."""
from .onec_report import EMPTY, _period


def fetch_conversion_orders(onec, ds, date_from, date_to):
    first, last = date_from.isoformat(), date_to.isoformat()
    orders = onec._odata("Document_ЗаказКлиента", {
        "$format": "json", "$filter": "DeletionMark eq false and " + _period(date_from, date_to),
        "$expand": "Менеджер,Контрагент", "$select": "Ref_Key,Number,Date,Статус,СуммаДокумента,DeletionMark,ДокументОснование,"
        "Менеджер/Description,Контрагент/Description"})
    orders = {r["Ref_Key"]: r for r in orders if r.get("Ref_Key") and not r.get("DeletionMark")
              and first <= str(r.get("Date") or "")[:10] <= last}
    bases = {r.get("ДокументОснование") for r in orders.values()} - {None, "", EMPTY}
    by_basis = {}
    if bases:
        # A valid October order may be linked to an August request. Do not restrict by request month.
        requests = onec._odata("Document_ТТК_ЗаявкаВОтделСнабжения", {
            "$format": "json", "$filter": f"DeletionMark eq false and Date le datetime'{last}T23:59:59'",
            "$expand": "ОтветственныйСнабжение", "$select": "Ref_Key,Number,Date,DeletionMark,ДокументОснования,"
            "ОтветственныйСнабжение/Description"})
        for r in requests:
            basis = r.get("ДокументОснования")
            name = str((r.get("ОтветственныйСнабжение") or {}).get("Description") or "").strip()
            if basis not in bases or not name or r.get("DeletionMark") or str(r.get("Date") or "")[:10] > last:
                continue
            owners = by_basis.setdefault(basis, {})
            owner = owners.setdefault(name.lower(), {"name": name, "docs": set()})
            doc = str(r.get("Number") or r.get("Ref_Key") or "").strip()
            if doc:
                owner["docs"].add(doc)
    out = []
    for key, r in orders.items():
        row = {"order_key": key, "dt": r["Date"][:10], "doc": str(r.get("Number") or "").strip(),
               "client": (r.get("Контрагент") or {}).get("Description"), "status": r.get("Статус"),
               "amount": r.get("СуммаДокумента")}
        manager = str((r.get("Менеджер") or {}).get("Description") or "").strip()
        if manager:
            out.append({**row, "id": key + "-sales", "manager": manager, "role": "sales"})
        for owner in by_basis.get(r.get("ДокументОснование"), {}).values():
            out.append({**row, "id": key + "-supply-" + owner["name"].lower(), "manager": owner["name"],
                        "role": "supply", "request_docs": ", ".join(sorted(owner["docs"]))})
    return out
