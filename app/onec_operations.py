"""Read-only operational documents, explicit references and source-authored events.

Source timestamps stay literal. Deadline interpretation belongs to operations.py.
Missing publication fields remain unknown and are described by coverage warnings.
"""
import concurrent.futures
import hashlib
import math
import urllib.parse
import uuid
import xml.etree.ElementTree as ET


EMPTY = "00000000-0000-0000-0000-000000000000"
DOCUMENTS = {
    "tender": "Document_ТТК_Тендер",
    "kp": "Document_КоммерческоеПредложениеКлиенту",
    "order": "Document_ЗаказКлиента",
    "supply": "Document_ТТК_ЗаявкаВОтделСнабжения",
    "transport": "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов",
}
TYPES = {"StandardODATA." + entity: kind for kind, entity in DOCUMENTS.items()}
COMMON = ["Ref_Key", "Number", "Date", "Posted", "DeletionMark"]
FIELDS = {
    "tender": ["Клиент_Key", "ВедущийМенеджер_Key", "Автор_Key", "Статус", "СрокДействия",
               "НаименованиеЛота", "СуммаДокумента", "ТекущаяПереторжка", "ЦенаВключаетНДС",
               "Валюта_Key", "Товары", "Переторжка", "ДатаОбновленияЦен", "КрайняяДатаПоставки"],
    "kp": ["Клиент_Key", "Менеджер_Key", "Автор_Key", "Статус", "ТТК_СрокПодачи", "СрокДействия",
           "ТТК_СрокДействияКП", "СуммаДокумента", "ЦенаВключаетНДС", "Валюта_Key", "Товары",
           "ДокументОснование", "ДокументОснование_Type", "ТТК_СрокОтгрузки", "УсловияДоставкиТекст"],
    "order": ["Партнер_Key", "Менеджер_Key", "Автор", "Автор_Type", "Статус", "СуммаДокумента",
              "ЦенаВключаетНДС", "Валюта_Key", "Товары", "ДокументОснование", "ДокументОснование_Type",
              "ТТК_КонечнаяДатаПоставки", "ТТК_ФактическаяДатаПоставки"],
    "supply": ["Менеджер_Key", "ОтветственныйСнабжение_Key", "ДокументОснования", "ДокументОснования_Type",
               "СтатусЗаявки", "ДатаЗакрытияЗаявки", "СрокОтработкиДо", "ДатаЗаписи", "ПунктДоставки",
               "ТаблицаТовары"] + [f"Поставщик{i}_Key" for i in range(1, 11)],
    "transport": ["Менеджер_Key", "ОтветственныйЛогист_Key", "Автор_Key", "Клиент_Key", "Статус",
                  "ДокументОснование", "ДокументОснование_Type", "ДокументОснование2", "ДокументОснование2_Type",
                  "АдресДоставки", "РезультатыРасчета", "ИсторияСтатусов"],
}
CLOSED = {"Отработана", "Закрыта", "Закрыто", "Завершена"}


def _ref(value):
    try:
        result = str(uuid.UUID(str(value)))
        return result if result != EMPTY else None
    except (ValueError, TypeError, AttributeError):
        return None


def identity(onec, kind, ref):
    """Stable source identity, independent of credentials and document numbers."""
    key = _ref(ref)
    if not key:
        raise ValueError("Некорректная ссылка 1С")
    url = urllib.parse.urlsplit(onec.base)
    host = (url.hostname or "").lower()
    if url.port:
        host += ":" + str(url.port)
    base = urllib.parse.urlunsplit((url.scheme.lower(), host, url.path.rstrip("/"), "", ""))
    database = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return f"onec:{database}:{kind}:{key}"


def _moment(value):
    if not value or str(value).startswith("0001-"):
        return None
    return str(value)


def _number(value):
    try:
        number = float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _error(exc):
    """Exception URLs may contain private source information."""
    code = getattr(exc, "code", None)
    return type(exc).__name__ + (f" HTTP {code}" if code else "")


def _pages(onec, entity, params, page_size=200, register=False):
    rows, seen = [], set()
    while True:
        query = dict(params, **{"$format": "json", "$top": page_size, "$skip": len(rows)})
        query["$orderby"] = "Период,Документ" if register else "Ref_Key"
        batch = onec._odata(entity, query)
        if not batch:
            return rows
        for row in batch:
            key = tuple(sorted((k, str(v)) for k, v in row.items())) if register else row.get("Ref_Key")
            if key in seen:
                raise ValueError("Повтор строки при постраничной загрузке 1С")
            seen.add(key)
        rows.extend(batch)
        if len(rows) > 100000:
            raise ValueError("Превышен предел выборки документов 1С")


def _metadata(onec, warnings):
    if not callable(getattr(onec, "metadata", None)):
        return None
    try:
        root = ET.fromstring(onec.metadata())
        return {element.get("Name"): {child.get("Name") for child in element
                                     if child.tag.endswith("Property")}
                for element in root.iter() if element.tag.endswith("EntityType")}
    except Exception as exc:
        warnings.append("Метаданные 1С недоступны. " + _error(exc))
        return None


def _catalog(onec, entity, keys, errors):
    keys = sorted(set(keys) - {None, "", EMPTY})
    parts = [keys[i:i + 24] for i in range(0, len(keys), 24)]

    def read(part):
        condition = " or ".join(f"Ref_Key eq guid'{key}'" for key in part)
        return _pages(onec, entity, {"$select": "Ref_Key,Description", "$filter": condition})

    result = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for batch in pool.map(read, parts):
                result.update({row["Ref_Key"]: row.get("Description") for row in batch})
    except Exception as exc:
        errors[entity] = _error(exc)
    return result


def _linked(onec, row):
    links = []
    unresolved = []
    for field in ("ДокументОснование", "ДокументОснование2", "ДокументОснования"):
        ref = _ref(row.get(field))
        if not ref:
            continue
        kind = TYPES.get(row.get(field + "_Type"))
        if kind:
            target = identity(onec, kind, ref)
            if target not in links:
                links.append(target)
        else:
            unresolved.append({"ref": ref, "type": row.get(field + "_Type"), "field": field})
    return links, unresolved


def _amount(kind, row):
    if kind in ("kp", "order"):
        return _number(row.get("СуммаДокумента")), True, "СуммаДокумента"
    if kind != "tender":
        return None, None, None
    bid = _number(row.get("ТекущаяПереторжка"))
    lines = row.get("Товары") or []
    if bid is not None and bid > 0:
        if row.get("ЦенаВключаетНДС") is False:
            return bid, False, "ТекущаяПереторжка"
        gross = [_number(line.get("СуммаСНДС")) for line in lines]
        vat = [_number(line.get("СуммаНДС")) for line in lines]
        if lines and None not in gross and None not in vat and abs(sum(gross) - bid) < 0.02:
            return round(bid - sum(vat), 2), False, "ТекущаяПереторжка−СуммаНДС"
        return None, False, "Не подтверждена сумма ставки без НДС"
    amounts = []
    for line in lines:
        gross, vat = _number(line.get("СуммаСНДС")), _number(line.get("СуммаНДС"))
        amount = round(gross - vat, 2) if gross is not None and vat is not None else None
        if amount is None and row.get("ЦенаВключаетНДС") is False:
            amount = _number(line.get("Сумма"))
        amounts.append(amount)
    if amounts and None not in amounts:
        return round(sum(amounts), 2), False, "Товары.СуммаСНДС−СуммаНДС"
    value = _number(row.get("СуммаДокумента"))
    if row.get("ЦенаВключаетНДС") is False and value is not None:
        return value, False, "СуммаДокумента"
    return None, False, None


def _items(entity, row, names, units, suppliers):
    result = []
    for index, line in enumerate(row.get("ТаблицаТовары") or row.get("Товары") or [], 1):
        number = str(line.get("LineNumber") or index)
        item_ref = _ref(line.get("Номенклатура_Key"))
        unit_ref = _ref(line.get("ЕдиницаИзмерения_Key") or line.get("Упаковка_Key"))
        supplier = _ref(line.get("ТекущийПоставщик_Key"))
        item = {"id": entity["id"] + ":row:" + number, "row_number": number,
                "item_id": item_ref, "nomenclature": item_ref, "name": names.get(item_ref),
                "quantity": _number(line.get("Количество")), "unit_id": unit_ref,
                "unit": units.get(unit_ref) or unit_ref,
                "characteristic": _ref(line.get("Характеристика_Key")), "lot": None,
                "currency": entity["currency"], "vat_included": None,
                "delivery": row.get("ПунктДоставки") or line.get("ТТК_АдресДоставки") or None,
                "included": True, "source_flag": line.get("Флаг"),
                "base_row_number": line.get("НомерСтрокиДокументаОснования"),
                "selected_supplier_id": supplier, "selected_supplier_name": suppliers.get(supplier),
                "selected_price": _number(line.get("ЦенаТекущегоПоставщика")),
                "selected_delivery_days": _number(line.get("СрокТекущегоПоставщика")),
                "source_date_raw": line.get("ДатаОбновленияЦен") or None, "offers": []}
        if entity["kind"] == "supply":
            for slot in range(1, 11):
                supplier_id = _ref(row.get(f"Поставщик{slot}_Key"))
                price = _number(line.get(f"ЦенаПоставки{slot}"))
                if supplier_id is None and price in (None, 0):
                    continue
                item["offers"].append({
                    "id": item["id"] + ":supplier:" + str(slot), "supplier_id": supplier_id,
                    "supplier_name": suppliers.get(supplier_id), "price": price,
                    "amount": _number(line.get(f"СуммаПоставки{slot}")),
                    "quantity": item["quantity"], "unit": item["unit"], "unit_id": unit_ref,
                    "currency": None, "vat_included": None, "delivery": item["delivery"],
                    "delivery_days": _number(line.get(f"Срок{slot}")), "lot": None,
                    "offered_at": None, "valid_until": None, "source_date_raw": item["source_date_raw"],
                    "selected": bool(supplier_id and supplier_id == supplier), "source": "onec"})
        result.append(item)
    return result


def fetch(onec, config, since=None):
    """Return a complete paginated snapshot, keeping failed reads distinct from gaps.

    All nondeleted sales documents are retained, including older open commitments.
    ``since`` is accepted for the sync interface, and never truncates the snapshot.
    """
    errors, warnings, datasets = {}, [], {}
    metadata = _metadata(onec, warnings)
    settings = config.get("operations", {})
    page_size = max(1, min(int(settings.get("page_size", 200)), 500))
    for kind, table in DOCUMENTS.items():
        fields = COMMON + FIELDS[kind]
        available = metadata.get(table, set()) if metadata is not None else None
        if kind == "transport" and available:
            fields += [name for name in ("ОтработатьДо", "СрокОтработкиДо", "ТТК_ОтработатьДо") if name in available]
        if available is not None:
            fields = [name for name in fields if name in available]
        try:
            datasets[kind] = _pages(onec, table, {"$select": ",".join(fields),
                                                    "$filter": "DeletionMark eq false"}, page_size)
        except Exception as exc:
            errors[kind] = _error(exc)
            datasets[kind] = []

    state_table = "InformationRegister_ТТКСостоянияДокументов"
    state_tables = [state_table, "InformationRegister_ТТК_СостоянияДокументов"]
    if metadata is not None:
        state_table = next((table for table in state_tables if table in metadata), None)
    states = []
    if state_table:
        try:
            states = _pages(onec, state_table, {}, page_size, register=True)
        except Exception as exc:
            warnings.append("Журнал состояний документов 1С недоступен. " + _error(exc))
    else:
        warnings.append("Журнал ТТКСостоянияДокументов не опубликован в OData. Время и автор подачи КП и тендеров неизвестны.")

    user_refs, partner_refs, item_refs, unit_refs, currency_refs = set(), set(), set(), set(), set()
    for rows in datasets.values():
        for row in rows:
            user_refs.update(_ref(row.get(field)) for field in ("Менеджер_Key", "ВедущийМенеджер_Key", "ОтветственныйСнабжение_Key", "ОтветственныйЛогист_Key", "Автор_Key", "Автор"))
            partner_refs.update(_ref(row.get(field)) for field in ("Клиент_Key", "Партнер_Key"))
            partner_refs.update(_ref(row.get(f"Поставщик{i}_Key")) for i in range(1, 11))
            currency_refs.add(_ref(row.get("Валюта_Key")))
            for line in row.get("ТаблицаТовары") or []:
                item_refs.add(_ref(line.get("Номенклатура_Key")))
                unit_refs.add(_ref(line.get("ЕдиницаИзмерения_Key")))
                partner_refs.add(_ref(line.get("ТекущийПоставщик_Key")))
            for history in row.get("ИсторияСтатусов") or []:
                user_refs.add(_ref(history.get("Пользователь_Key")))
    for state in states:
        user_refs.add(_ref(state.get("Автор_Key") or state.get("Автор")))
    users = _catalog(onec, "Catalog_Пользователи", user_refs, errors)
    partners = _catalog(onec, "Catalog_Партнеры", partner_refs, errors)
    names = _catalog(onec, "Catalog_Номенклатура", item_refs, errors)
    units = _catalog(onec, "Catalog_УпаковкиЕдиницыИзмерения", unit_refs, errors)
    currencies = _catalog(onec, "Catalog_Валюты", currency_refs, errors)
    managers = {str(emp.get("onec_manager") or "").strip().lower(): emp["login"]
                for emp in config.get("employees", []) if emp.get("login") and emp.get("onec_manager")}

    def actor(ref):
        key = _ref(ref)
        name = users.get(key) if key else str(ref or "").strip() or None
        login = managers.get(str(ref or "").lower()) or managers.get(str(name or "").strip().lower())
        return login or ("1c:" + (name or ref) if ref else None), name

    entities, events, by_id = [], [], {}
    for kind, rows in datasets.items():
        for row in rows:
            ref = _ref(row.get("Ref_Key"))
            if not ref:
                errors[kind] = "Некорректная ссылка документа 1С"
                continue
            owner_field = {"tender": "ВедущийМенеджер_Key", "supply": "ОтветственныйСнабжение_Key",
                           "transport": "ОтветственныйЛогист_Key"}.get(kind, "Менеджер_Key")
            owner, owner_name = actor(_ref(row.get(owner_field)))
            sales_owner, sales_name = actor(_ref(row.get("ВедущийМенеджер_Key") if kind == "tender" else row.get("Менеджер_Key")))
            links, unresolved = _linked(onec, row)
            amount, vat, amount_source = _amount(kind, row)
            due_field = {"kp": "ТТК_СрокПодачи", "tender": "СрокДействия", "supply": "СрокОтработкиДо",
                         "order": "ТТК_КонечнаяДатаПоставки"}.get(kind)
            if kind == "transport":
                due_field = next((field for field in ("ОтработатьДо", "СрокОтработкиДо", "ТТК_ОтработатьДо") if _moment(row.get(field))), None)
            due = _moment(row.get(due_field)) if due_field else None
            entity = {"id": identity(onec, kind, ref), "ref": ref, "kind": kind,
                      "number": str(row.get("Number") or "").strip(), "date": _moment(row.get("Date")),
                      "title": row.get("НаименованиеЛота") or None,
                      "client": partners.get(_ref(row.get("Клиент_Key") or row.get("Партнер_Key"))),
                      "owner": owner, "owner_name": owner_name, "sales_owner": sales_owner,
                      "sales_owner_name": sales_name, "status": row.get("СтатусЗаявки") if kind == "supply" else row.get("Статус"),
                      "due": due, "due_source": due_field, "due_verified": bool(due),
                      "closed_at": _moment(row.get("ДатаЗакрытияЗаявки")), "closed_by": None,
                      "amount": amount, "vat_included": vat, "amount_source": amount_source,
                      "source_amount": _number(row.get("СуммаДокумента")),
                      "source_price_includes_vat": row.get("ЦенаВключаетНДС"),
                      "currency": currencies.get(_ref(row.get("Валюта_Key"))), "links": links,
                      "unresolved_links": unresolved,
                      "url": "e1cib/data/Документ." + DOCUMENTS[kind][9:] + "?ref=" + ref.replace("-", ""),
                      "price_valid_until": _moment(row.get("СрокДействия")) if kind == "kp" else None,
                      "price_validity_text": row.get("ТТК_СрокДействияКП") or None,
                      "delivery_due": _moment(row.get("КрайняяДатаПоставки") or row.get("ТТК_КонечнаяДатаПоставки")),
                      "delivery_actual": _moment(row.get("ТТК_ФактическаяДатаПоставки")),
                      "delivery_terms": row.get("ТТК_СрокОтгрузки") or None,
                      "delivery": row.get("ПунктДоставки") or row.get("АдресДоставки") or row.get("УсловияДоставкиТекст") or None,
                      "posted": row.get("Posted"), "events": [], "items": []}
            entity["items"] = _items(entity, row, names, units, partners)
            if kind == "transport":
                entity["delivery_options"] = [{"id": str(line.get("LineNumber") or index),
                    "selected": bool(line.get("Флаг")), "carrier": line.get("ТранспортнаяКомпания") or None,
                    "from": line.get("АдресОтправки") or None, "to": line.get("АдресДоставки") or None,
                    "amount": _number(line.get("СтоимостьСНДС")), "vat_included": True,
                    "calculated_at": _moment(line.get("ДатаРасчета")), "transit": line.get("ВремяВПути") or None}
                    for index, line in enumerate(row.get("РезультатыРасчета") or [], 1)]
            entities.append(entity)
            by_id[entity["id"]] = entity

    def add_event(entity, at, status, author_ref, source, discriminator):
        if not at:
            return
        author, author_name = actor(author_ref)
        kind = "submitted" if entity["kind"] in ("kp", "tender") and status == "Действует" else "status"
        digest = hashlib.sha256((entity["id"] + "|" + source + "|" + str(discriminator) + "|" + at + "|" + str(status) + "|" + str(author_ref or "")).encode()).hexdigest()[:24]
        event = {"id": "onec:event:" + digest, "entity_id": entity["id"], "at": at,
                 "author": author, "author_name": author_name, "kind": kind, "status": status,
                 "description": status, "url": entity["url"], "source": "onec", "source_register": source,
                 "time_verified": True}
        entity["events"].append(event)
        events.append(event)

    for kind, rows in datasets.items():
        if kind != "transport":
            continue
        for row in rows:
            entity = by_id.get(identity(onec, kind, row["Ref_Key"]))
            for index, entry in enumerate(row.get("ИсторияСтатусов") or [], 1):
                add_event(entity, _moment(entry.get("ДатаУстановки")), entry.get("Статус"),
                          entry.get("Пользователь_Key"), "ИсторияСтатусов", entry.get("LineNumber") or index)
            if entity["status"] in CLOSED:
                closings = [event for event in entity["events"] if event["status"] in CLOSED]
                if closings:
                    last = max(closings, key=lambda event: event["at"])
                    entity["closed_at"], entity["closed_by"] = last["at"], last["author"]
            if entity["due"] is None:
                base = next((by_id[link] for link in entity["links"] if link in by_id and by_id[link]["kind"] in ("tender", "kp")), None)
                if base and base.get("due"):
                    entity["due"] = base["due"]
                    entity["due_source"] = "Срок подачи документа-основания " + base["number"]
                    entity["due_verified"] = False
                    entity["due_derived_from"] = base["id"]
    for state in states:
        kind = TYPES.get(state.get("Документ_Type"))
        ref = _ref(state.get("Документ") or state.get("Документ_Key"))
        if not kind or not ref:
            continue
        entity = by_id.get(identity(onec, kind, ref))
        if entity:
            add_event(entity, _moment(state.get("Период") or state.get("Period")), state.get("Состояние"),
                      state.get("Автор_Key") or state.get("Автор"), state_table, "")
    for entity in entities:
        if entity["kind"] == "supply" and entity["status"] in CLOSED and entity["closed_at"]:
            matching = [event for event in entity["events"] if event["status"] in CLOSED
                        and event["at"] == entity["closed_at"]]
            if matching:
                entity["closed_by"] = matching[-1]["author"]
    if datasets.get("supply"):
        warnings.append("Предложения ОС не содержат подтверждённых дат предложения, валюты и базы НДС. Первоначальные цены доступны справочно.")
    if any(entity["kind"] == "transport" and not entity["due_verified"] for entity in entities):
        warnings.append("В OData нет собственного срока «Отработать до» ОЛ. Производный срок подачи основания показан отдельно, своевременность ОЛ не подтверждена.")
    return {"entities": entities, "events": events, "errors": errors, "warnings": warnings}
