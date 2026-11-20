"""Документооборот снабжения — доля заказов с файлом договора."""
import json
import re

from .onec_supply import _when, order_requests, supply_request_context
from .onec_operations import _pages


CONTRACT_NAME = re.compile(r"договор|(?<![а-яёa-z])дог(?![а-яёa-z])", re.I)
ORDER_TYPE = "StandardODATA.Document_ЗаказПоставщику"
CONTRACT_TYPE = "StandardODATA.Catalog_ДоговорыКонтрагентов"
FORWARDER_TYPE = "StandardODATA.Document_ПоручениеЭкспедитору"
FORWARDER_FILES = "Catalog_ПоручениеЭкспедиторуПрисоединенныеФайлы"


def contract_files(onec, include_contracts=True, file_entity='Catalog_Файлы'):
    """Индекс подходящих файлов по типу и ссылке владельца, без загрузки содержимого."""
    attached = _pages(onec, "Catalog_ДоговорыКонтрагентовПрисоединенныеФайлы", {
        "$format": "json", "$filter": "DeletionMark eq false and IsFolder eq false",
        "$select": "Ref_Key,Description,ВладелецФайла_Key,Расширение,DeletionMark,IsFolder"}) if include_contracts else []
    attachments = [(CONTRACT_TYPE, file.get("ВладелецФайла_Key"), file) for file in attached]
    if file_entity == FORWARDER_FILES:
        forwarder_files = _pages(onec, file_entity, {
            "$filter": "DeletionMark eq false",
            "$select": "Ref_Key,Description,ВладелецФайла_Key,Расширение,DeletionMark"})
        attachments.extend((FORWARDER_TYPE, file["ВладелецФайла_Key"], file) for file in forwarder_files)
    else:
        generic = _pages(onec, file_entity, {
            "$filter": "DeletionMark eq false",
            "$select": "Ref_Key,Description,ВладелецФайла,ВладелецФайла_Type,Расширение,DeletionMark"})
        attachments.extend((file.get("ВладелецФайла_Type"), file.get("ВладелецФайла"), file) for file in generic)
    files = {}
    for owner_type, owner_key, file in attachments:
        if file.get("DeletionMark") or file.get("IsFolder"):
            continue
        name = file.get("Description") or ""
        extension = file.get("Расширение") or ""
        name = name if not extension or name.lower().endswith("." + extension.lower()) else name + "." + extension
        if CONTRACT_NAME.search(name):
            files.setdefault((owner_type, owner_key), {})[file["Ref_Key"]] = name
    return files


def fetch(onec, ds, managers, first, last):
    context = supply_request_context(onec, ds, first)
    orders = onec._odata("Document_ЗаказПоставщику", {
        "$format": "json", "$expand": "Контрагент",
        "$filter": f"Posted eq true and DeletionMark eq false and Date ge datetime'{_when(first)}' "
                   f"and Date le datetime'{last.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Договор_Key,ТТК_ЗаказКлиента_Key,ДокументОснование,Контрагент/Description"})
    contracts = {r["Ref_Key"]: r.get("Description") for r in onec._odata("Catalog_ДоговорыКонтрагентов", {
        "$format": "json", "$select": "Ref_Key,Description"})}
    files = contract_files(onec)
    rows = {}
    for order in orders:
        requests = order_requests(order, context)
        owners = {(r.get("ОтветственныйСнабжение") or {}).get("Description") for r in requests} - {None, ""}
        owner = next(iter(owners)) if len(owners) == 1 else ""
        contract = order.get("Договор_Key")
        matched_files = dict(files.get((CONTRACT_TYPE, contract), {}))
        matched_files.update(files.get((ORDER_TYPE, order["Ref_Key"]), {}))
        matched = list(matched_files.values())
        rows[order["Ref_Key"]] = {
            "id": order["Ref_Key"], "emp": managers.get(owner.strip().lower(), "1c:" + owner),
            "doc": order["Number"].strip(), "dt": order["Date"][:10],
            "supplier": (order.get("Контрагент") or {}).get("Description"),
            "request_docs": ", ".join(sorted({r.get("Number") or r["Ref_Key"] for r in requests})),
            "contract_name": contracts.get(contract), "contract_key": contract,
            "files": json.dumps(matched, ensure_ascii=False), "has_contract": int(bool(matched))}
    return list(rows.values())


def fetch_forwarder_contracts(onec, managers, first, last, file_entity=None):
    if not file_entity:
        raise ValueError('Источник прикреплённых файлов поручений экспедитору не настроен: требуется проверка публикации OData')
    orders = _pages(onec, 'Document_ПоручениеЭкспедитору', {
        '$format': 'json', '$expand': 'ТТК_ОтветственныйЛогист,ТТК_ТранспортнаяКомпания',
        '$filter': f"DeletionMark eq false and Date ge datetime'{_when(first)}' "
                   f"and Date le datetime'{last.isoformat()}T23:59:59'",
        '$select': 'Ref_Key,Number,Date,DeletionMark,ТТК_ОтветственныйЛогист/Description,'
                   'ТТК_ТранспортнаяКомпания/Description'})
    files = contract_files(onec, include_contracts=False, file_entity=file_entity)
    rows = []
    for order in orders:
        if order.get('DeletionMark'):
            continue
        owner = (order.get('ТТК_ОтветственныйЛогист') or {}).get('Description') or ''
        matched = list(files.get((FORWARDER_TYPE, order['Ref_Key']), {}).values())
        rows.append({'id': order['Ref_Key'], 'emp': managers.get(owner.strip().lower(), '1c:' + owner),
                     'dt': order['Date'][:10], 'doc': (order.get('Number') or '').strip(),
                     'carrier': (order.get('ТТК_ТранспортнаяКомпания') or {}).get('Description'),
                     'files': json.dumps(matched, ensure_ascii=False), 'has_contract': int(bool(matched))})
    return rows


def report(con, emp, first, last, asof, table='supply_contracts'):
    if table not in ('supply_contracts', 'forwarder_contracts'):
        raise ValueError('Неизвестный источник документооборота')
    marker = con.execute('SELECT * FROM dataset_state WHERE dataset=?', (table,)).fetchone()
    required_end = min(last, asof).isoformat()
    known = bool(marker and marker["date_from"] <= first.isoformat() <= required_end <= marker["date_to"])
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE emp=? AND dt BETWEEN ? AND ? ORDER BY dt,doc,id",
                                       (emp, first.isoformat(), last.isoformat()))]
    matched = sum(r["has_contract"] for r in rows)
    return {"known": known, "total": len(rows), "matched": matched,
            "percent": matched / len(rows) * 100 if known and rows else None, "rows": rows}
