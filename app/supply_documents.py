"""Документооборот снабжения — доля заказов с файлом в карточке договора."""
import json
import re

from .onec_supply import _when, order_requests, supply_request_context


CONTRACT_NAME = re.compile(r"договор|(?<![а-яё])дог\.", re.I)


def fetch(onec, ds, managers, first, last):
    context = supply_request_context(onec, ds, first)
    orders = onec._odata("Document_ЗаказПоставщику", {
        "$format": "json", "$expand": "Контрагент",
        "$filter": f"Posted eq true and DeletionMark eq false and Date ge datetime'{_when(first)}' "
                   f"and Date le datetime'{last.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Договор_Key,ТТК_ЗаказКлиента_Key,ДокументОснование,Контрагент/Description"})
    contracts = {r["Ref_Key"]: r.get("Description") for r in onec._odata("Catalog_ДоговорыКонтрагентов", {
        "$format": "json", "$select": "Ref_Key,Description"})}
    attached = onec._odata("Catalog_ДоговорыКонтрагентовПрисоединенныеФайлы", {
        "$format": "json", "$filter": "DeletionMark eq false and IsFolder eq false",
        "$select": "Ref_Key,Description,ВладелецФайла_Key,Расширение,DeletionMark,IsFolder"})
    files = {}
    for file in attached:
        if file.get("DeletionMark") or file.get("IsFolder"):
            continue
        name = file.get("Description") or ""
        extension = file.get("Расширение") or ""
        name = name if not extension or name.lower().endswith("." + extension.lower()) else name + "." + extension
        if CONTRACT_NAME.search(name):
            files.setdefault(file.get("ВладелецФайла_Key"), {})[file["Ref_Key"]] = name
    rows = {}
    for order in orders:
        requests = order_requests(order, context)
        owners = {(r.get("ОтветственныйСнабжение") or {}).get("Description") for r in requests} - {None, ""}
        owner = next(iter(owners)) if len(owners) == 1 else ""
        contract = order.get("Договор_Key")
        matched = list(files.get(contract, {}).values())
        rows[order["Ref_Key"]] = {
            "id": order["Ref_Key"], "emp": managers.get(owner.strip().lower(), "1c:" + owner),
            "doc": order["Number"].strip(), "dt": order["Date"][:10],
            "supplier": (order.get("Контрагент") or {}).get("Description"),
            "request_docs": ", ".join(sorted({r.get("Number") or r["Ref_Key"] for r in requests})),
            "contract_name": contracts.get(contract), "contract_key": contract,
            "files": json.dumps(matched, ensure_ascii=False), "has_contract": int(bool(matched))}
    return list(rows.values())


def report(con, emp, first, last, asof):
    marker = con.execute("SELECT * FROM dataset_state WHERE dataset='supply_contracts'").fetchone()
    required_end = min(last, asof).isoformat()
    known = bool(marker and marker["date_from"] <= first.isoformat() <= required_end <= marker["date_to"])
    rows = [dict(r) for r in con.execute("SELECT * FROM supply_contracts WHERE emp=? AND dt BETWEEN ? AND ? ORDER BY dt,doc,id",
                                       (emp, first.isoformat(), last.isoformat()))]
    matched = sum(r["has_contract"] for r in rows)
    return {"known": known, "total": len(rows), "matched": matched,
            "percent": matched / len(rows) * 100 if known and rows else None, "rows": rows}
