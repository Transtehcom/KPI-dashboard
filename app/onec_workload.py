"""Read-only, metadata-driven 1C document snapshot for the workload table."""
import datetime as dt
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .onec_operations import _error, _ref, identity
from .onec_supply import REQUEST_STAGES


AUTHOR_ONLY = {"shashmurina", "petrovaaa"}
RESPONSIBLE_FIELDS = ("ОтветственныйСнабжение", "ОтветственныйЛогист",
                      "ТТК_ОтветственныйЛогист", "ТТК_ОтветственныйСнабженец")
DEADLINE_FIELDS = {
    "Document_ЗаказКлиента": ("ТТК_КонечнаяДатаПоставки",),
    "Document_ПоручениеЭкспедитору": ("ТТК_ДПП",),
    "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов": ("ОтработатьДо", "СрокОтработкиДо", "ТТК_ОтработатьДо"),
    "Document_ТТК_ЗаявкаВОтделСнабжения": ("СрокОтработкиДо",),
    "Document_КоммерческоеПредложениеКлиенту": ("ТТК_СрокПодачи", "СрокПодачи"),
    "Document_ТТК_Тендер": ("СрокДействия",),
}
GENERIC_DEADLINES = ("СрокВыполнения", "ДатаВыполненияПлан", "ПлановаяДатаВыполнения",
                     "ДатаОкончанияПлан", "ПлановаяДатаОкончания", "ОтработатьДо",
                     "СрокОтработкиДо", "ТТК_ОтработатьДо")
COMPLETION_FIELDS = {
    "Document_ТТК_ЗаявкаВОтделСнабжения": ("ДатаЗакрытияЗаявки",),
    "Document_ЗаказКлиента": ("ТТК_ФактическаяДатаПоставки",),
    "Document_ПоручениеЭкспедитору": ("ТТК_ДФП",),
}
GENERIC_COMPLETIONS = ("ДатаФактическогоВыполнения", "ФактическаяДатаВыполнения",
                       "ДатаФактическогоЗавершения", "ФактическаяДатаЗавершения")
COMMON = ("Ref_Key", "Number", "Date", "Posted", "DeletionMark")
EKB = dt.timezone(dt.timedelta(hours=5))


def _metadata(onec):
    root = ET.fromstring(onec.metadata())
    types, entities = {}, {}
    for schema in root.iter():
        if schema.tag.rsplit("}", 1)[-1] != "Schema":
            continue
        namespace = schema.get("Namespace", "")
        for item in schema:
            if item.tag.rsplit("}", 1)[-1] == "EntityType":
                fields = {field.get("Name"): field.get("Type", "") for field in item
                          if field.tag.rsplit("}", 1)[-1] == "Property"}
                types[namespace + "." + item.get("Name", "")] = fields
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] == "EntitySet":
            name = item.get("Name", "")
            if re.fullmatch(r"\w+", name):
                entities[name] = types.get(item.get("EntityType"), {})
    if not entities:
        raise ValueError("Нет опубликованных наборов сущностей 1С")
    return entities


def _endpoint(onec, entity):
    return onec.base.rstrip("/") + "/odata/standard.odata/" + urllib.parse.quote(entity)


def _pages(onec, entity, params, page_size):
    """Follow server continuations and probe short pages with deterministic skip."""
    endpoint = _endpoint(onec, entity)
    rows, seen, seen_urls = [], set(), set()
    arguments = {**params, "$format": "json", "$top": page_size, "$orderby": "Ref_Key", "$skip": 0}
    url = endpoint + "?" + urllib.parse.urlencode(arguments, quote_via=urllib.parse.quote)
    while True:
        if url in seen_urls:
            raise ValueError("Повтор страницы 1С")
        seen_urls.add(url)
        payload = onec._get(url)
        if not isinstance(payload, dict) or "error" in payload or "odata.error" in payload:
            raise ValueError("Ошибка или некорректный ответ 1С")
        body = payload.get("d", payload)
        if not isinstance(body, dict) or "error" in body or "odata.error" in body:
            raise ValueError("Ошибка или некорректный ответ 1С")
        batch = body.get("results", body.get("value"))
        if not isinstance(batch, list):
            raise ValueError("Некорректная страница 1С")
        for row in batch:
            key = _ref(row.get("Ref_Key"))
            if not key or key in seen:
                raise ValueError("Повтор или неверная ссылка документа 1С")
            seen.add(key)
        rows.extend(batch)
        if len(rows) > 200000:
            raise ValueError("Превышен предел выборки 1С")
        next_link = payload.get("@odata.nextLink") or payload.get("odata.nextLink") or body.get("__next")
        if next_link:
            candidate = urllib.parse.urljoin(url, str(next_link))
            current, target = urllib.parse.urlsplit(endpoint), urllib.parse.urlsplit(candidate)
            if (current.scheme, current.netloc, urllib.parse.unquote(current.path)) != (
                    target.scheme, target.netloc, urllib.parse.unquote(target.path)):
                raise ValueError("Некорректный адрес продолжения 1С")
            url = candidate
        elif batch:
            arguments["$skip"] = len(rows)
            url = endpoint + "?" + urllib.parse.urlencode(arguments, quote_via=urllib.parse.quote)
        else:
            return rows


def _name(value):
    return " ".join(str(value or "").split()).casefold()


def _filters(conditions, selected, prefix=""):
    """Keep the encoded query below the standard IIS query-string limit."""
    batch = []
    for condition in conditions:
        candidate = [*batch, condition]
        query = {"$format": "json", "$top": 1000, "$skip": 200000, "$orderby": "Ref_Key",
                 "$select": selected, "$filter": prefix + "(" + " or ".join(candidate) + ")"}
        if batch and len(urllib.parse.urlencode(query, quote_via=urllib.parse.quote)) >= 1800:
            yield prefix + "(" + " or ".join(batch) + ")"
            candidate = [condition]
        batch = candidate
    if batch:
        yield prefix + "(" + " or ".join(batch) + ")"


def _users(onec, config, metadata, errors, page_size):
    configured = []
    for employee in config.get("employees", []):
        values = employee.get("onec_manager") or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            if employee.get("login") and _name(value):
                configured.append((employee["login"], str(value).strip()))
    names = sorted({value for _, value in configured if not _ref(value)})
    catalog = "Catalog_Пользователи"
    records = []
    if names:
        if not {"Ref_Key", "Description"}.issubset(metadata.get(catalog, {})):
            errors[catalog] = "Не опубликован справочник пользователей 1С"
        else:
            try:
                conditions = ["Description eq '" + name.replace("'", "''") + "'" for name in names]
                for condition in _filters(conditions, "Ref_Key,Description"):
                    records.extend(_pages(onec, catalog, {"$select": "Ref_Key,Description", "$filter": condition}, page_size))
            except Exception as exc:
                errors[catalog] = _error(exc)
    by_name = {}
    for row in records:
        ref = _ref(row.get("Ref_Key"))
        if ref:
            by_name.setdefault(_name(row.get("Description")), set()).add(ref)
    users = {}
    unresolved = False
    for login, value in configured:
        direct = _ref(value)
        refs = {direct} if direct else by_name.get(_name(value), set())
        if len(refs) != 1:
            unresolved = True
            continue
        users.setdefault(next(iter(refs)), set()).add(login)
    if any(len(logins) > 1 for logins in users.values()):
        unresolved = True
    if unresolved and catalog not in errors:
        errors[catalog] = "Не все onec_manager однозначно найдены в справочнике пользователей 1С"
    return users


def _field(fields, name):
    """Resolve an explicit owner attribute to its published scalar property."""
    for field in (name + "_Key", name):
        if fields.get(field) == "Edm.Guid" or field in fields and field + "_Type" in fields:
            return field
    return None


def _owners(fields, entity=None):
    exact = _field(fields, "Ответственный")
    if exact:
        return [exact]
    responsible = [field for name in RESPONSIBLE_FIELDS if (field := _field(fields, name))]
    if responsible:
        return responsible
    manager = _field(fields, "Менеджер")
    if not manager and entity == "Document_ТТК_Тендер":
        manager = _field(fields, "ВедущийМенеджер")
    return [manager] if manager else []


def _condition(field, ref, fields):
    value = f"guid'{ref}'"
    if field + "_Type" in fields:
        value = f"cast({value}, 'Catalog_Пользователи')"
    return field + " eq " + value


def _moment(value, deadline=False):
    if not value or str(value).startswith("0001-"):
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    midnight = moment.time().replace(tzinfo=None) == dt.time()
    if moment.tzinfo:
        moment = moment.astimezone(EKB).replace(tzinfo=None)
    if deadline and midnight:
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=0)
    return moment.isoformat(timespec="seconds")


def _date_fields(entity, fields, specific, generic):
    names = specific.get(entity, ()) if entity in (FORWARDER_ENTITY, TRANSPORT_ENTITY, RECEIPT_ENTITY) else (*specific.get(entity, ()), *generic)
    return [name for name in names
            if fields.get(name) in ("Edm.DateTime", "Edm.DateTimeOffset")]


def _native_time(raw, fields, deadline=False):
    for field in fields:
        value = _moment(raw.get(field), deadline=deadline)
        if value:
            return value, field
    return None, None


def _row_owners(raw, owner_fields, author, users, fields):
    result = {}
    for field in [*owner_fields, *([author] if author else [])]:
        if field + "_Type" in fields and raw.get(field + "_Type") != "StandardODATA.Catalog_Пользователи":
            continue
        for login in sorted(users.get(_ref(raw.get(field)), set())):
            if (login in AUTHOR_ONLY) == (field == author):
                result.setdefault(login, field)
    return result


def _navigation_url(onec, entity, ref):
    parsed = urllib.parse.urlsplit(onec.base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return onec.base.rstrip("/") + "/#e1cib/data/Документ." + entity[len("Document_"):] + "?ref=" + ref.replace("-", "")


TENDER_ENTITY = "Document_ТТК_Тендер"
TENDER_OPEN_STATUSES = {"Черновик", "Подготовлен", "Действует"}
SUPPLY_ENTITY = "Document_ТТК_ЗаявкаВОтделСнабжения"
FORWARDER_ENTITY = "Document_ПоручениеЭкспедитору"
TRANSPORT_ENTITY = "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов"
RECEIPT_ENTITY = "Document_ПриобретениеТоваровУслуг"
STATUS_FIELDS = {TENDER_ENTITY: "Статус", SUPPLY_ENTITY: "СтатусЗаявки", FORWARDER_ENTITY: "ТТК_Статус",
                 TRANSPORT_ENTITY: "Статус"}


def document_closed(row):
    if row.get("entity") == RECEIPT_ENTITY:
        return True
    if row.get("entity") == TENDER_ENTITY and row.get("status"):
        return row["status"] not in TENDER_OPEN_STATUSES
    if row.get("entity") == SUPPLY_ENTITY:
        if row.get("status"):
            return row["status"] in REQUEST_STAGES
        return bool(row.get("closed_at"))
    if row.get("entity") == FORWARDER_ENTITY:
        return bool(row.get("closed_at"))
    if row.get("entity") == TRANSPORT_ENTITY:
        return row.get("status") == "Отработана"
    return bool(row["posted"])


def _transport_deadlines(onec, metadata, documents, due_fields, page_size):
    """The request form displays its primary tender/KP basis expiration when no native deadline is published."""
    references = {}
    for raw in documents:
        if _native_time(raw, due_fields, deadline=True)[0]:
            continue
        entity = str(raw.get("ДокументОснование_Type") or "").removeprefix("StandardODATA.")
        ref = _ref(raw.get("ДокументОснование"))
        if entity in (TENDER_ENTITY, "Document_КоммерческоеПредложениеКлиенту") and ref:
            references.setdefault(entity, set()).add(ref)
    deadlines = {}
    for entity, refs in references.items():
        if metadata.get(entity, {}).get("СрокДействия") not in ("Edm.DateTime", "Edm.DateTimeOffset"):
            continue
        selected = "Ref_Key,Number,СрокДействия"
        conditions = ["Ref_Key eq guid'" + ref + "'" for ref in sorted(refs)]
        for condition in _filters(conditions, selected):
            for basis in _pages(onec, entity, {"$select": selected, "$filter": condition}, page_size):
                deadline = _moment(basis.get("СрокДействия"), deadline=True)
                if deadline:
                    deadlines[(entity, _ref(basis.get("Ref_Key")))] = (
                        deadline, "СрокДействия документа-основания " + str(basis.get("Number") or "").strip())
    return deadlines


def _transport_finished(raw):
    history = raw.get("ИсторияСтатусов") or []
    if not isinstance(history, list):
        raise ValueError("Некорректная история статусов транспортной заявки")
    moments = [_moment(event.get("ДатаУстановки")) for event in history if event.get("Статус") == "Отработана"]
    return max((moment for moment in moments if moment), default=None)


def fetch(onec, config, date_from):
    """Return rows, successful entity names, safe errors and the closed-document date window.

    Open documents survive the date window. A schema-level owner choice prevents
    empty responsible attributes from falling through to a manager or author.
    """
    since = date_from.date() if isinstance(date_from, dt.datetime) else dt.date.fromisoformat(str(date_from)[:10])
    result = {"rows": [], "entities": [], "errors": {}, "since": since.isoformat()}
    errors = result["errors"]
    try:
        metadata = _metadata(onec)
    except Exception as exc:
        errors["$metadata"] = _error(exc)
        return result
    page_size = max(1, min(int(config.get("workload", {}).get("onec_page_size", 200)), 1000))
    users = _users(onec, config, metadata, errors, page_size)
    if "Catalog_Пользователи" in errors:
        return result
    for entity, fields in sorted(metadata.items()):
        if not entity.startswith("Document_") or "LineNumber" in fields:
            continue
        if not set(COMMON).issubset(fields):
            errors[entity] = "Не опубликованы обязательные реквизиты документа 1С"
            continue
        owner_fields, author = _owners(fields, entity), _field(fields, "Автор")
        due_fields = _date_fields(entity, fields, DEADLINE_FIELDS, GENERIC_DEADLINES)
        closed_fields = _date_fields(entity, fields, COMPLETION_FIELDS, GENERIC_COMPLETIONS)
        selected = list(dict.fromkeys([*COMMON, *owner_fields, *([author] if author else []), *due_fields, *closed_fields]))
        status_field = STATUS_FIELDS.get(entity)
        if status_field in fields:
            selected.append(status_field)
        if entity == TRANSPORT_ENTITY:
            selected.extend(name for name in ("ИсторияСтатусов", "ДокументОснование", "ДокументОснование_Type") if name in fields)
        selected += [field + "_Type" for field in [*owner_fields, *([author] if author else [])] if field + "_Type" in fields]
        conditions = []
        for ref, logins in sorted(users.items()):
            if logins - AUTHOR_ONLY:
                conditions.extend(_condition(field, ref, fields) for field in owner_fields)
            if logins & AUTHOR_ONLY and author:
                conditions.append(_condition(author, ref, fields))
        try:
            documents = {}
            prefix = "DeletionMark eq false and (Posted eq false or Date ge datetime'" + since.isoformat() + "T00:00:00') and "
            if entity in STATUS_FIELDS:
                # Status-controlled documents can be posted while still open before the date window.
                prefix = "DeletionMark eq false and "
            for condition in _filters(conditions, ",".join(selected), prefix):
                for raw in _pages(onec, entity, {"$select": ",".join(selected), "$filter": condition}, page_size):
                    ref = _ref(raw.get("Ref_Key"))
                    if ref in documents and documents[ref] != raw:
                        raise ValueError("Документ изменился во время загрузки 1С")
                    documents[ref] = raw
            transport_deadlines = (_transport_deadlines(onec, metadata, documents.values(), due_fields, page_size)
                                   if entity == TRANSPORT_ENTITY else {})
            rows = []
            for ref, raw in documents.items():
                posted = raw.get("Posted") is True
                source_date = _moment(raw.get("Date"))
                if not source_date or raw.get("Posted") not in (True, False) or raw.get("DeletionMark") not in (True, False):
                    raise ValueError("Некорректные обязательные реквизиты документа 1С")
                status = raw.get(status_field)
                if entity == RECEIPT_ENTITY:
                    native_closed_at = source_date
                else:
                    native_closed_at = _transport_finished(raw) if entity == TRANSPORT_ENTITY else _native_time(raw, closed_fields)[0]
                closed = document_closed({"entity": entity, "posted": posted, "status": status, "closed_at": native_closed_at})
                if raw.get("DeletionMark") or closed and source_date[:10] < since.isoformat():
                    continue
                deadline, deadline_source = _native_time(raw, due_fields, deadline=True)
                if entity == TRANSPORT_ENTITY and not deadline:
                    basis_entity = str(raw.get("ДокументОснование_Type") or "").removeprefix("StandardODATA.")
                    deadline, deadline_source = transport_deadlines.get((basis_entity, _ref(raw.get("ДокументОснование"))), (None, None))
                closed_at = native_closed_at if closed else None
                for login, owner_field in _row_owners(raw, owner_fields, author, users, fields).items():
                    rows.append({"id": identity(onec, entity, ref) + ":" + login, "emp": login,
                                 "entity": entity, "ref": ref, "number": str(raw.get("Number") or ""),
                                 "date": source_date, "posted": posted, "status": status, "deadline": deadline,
                                 "deadline_source": deadline_source, "closed_at": closed_at,
                                 "owner_field": owner_field, "url": _navigation_url(onec, entity, ref)})
            result["rows"].extend(rows)
            result["entities"].append(entity)
        except Exception as exc:
            errors[entity] = _error(exc)
    return result
