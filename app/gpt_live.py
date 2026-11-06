"""Bounded live source reads using configured Bitrix REST and 1C OData connections."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from .crm_links import portal_origin

from .bitrix24 import Bitrix24
from .onec import OneC
from .common import month_add, month_bounds
from .crm_links import object_url

LIVE_TOOLS = ["b24_search", "b24_card", "b24_fields", "onec_entities", "onec_schema", "onec_search", "onec_card"]
B24 = {
    "company": ("crm.company", "TITLE"), "contact": ("crm.contact", "LAST_NAME"),
    "lead": ("crm.lead", "TITLE"), "deal": ("crm.deal", "TITLE"),
    "activity": ("crm.activity", "SUBJECT"), "task": ("tasks.task", "TITLE"), "user": ("user", "LAST_NAME"), "requisite": ("crm.requisite", "NAME"),
}
RELATIONS = {"COMPANY_ID": "company", "CONTACT_ID": "contact", "ASSIGNED_BY_ID": "user",
             "RESPONSIBLE_ID": "user", "CREATED_BY_ID": "user", "responsibleId": "user", "createdBy": "user"}


def label(name):
    for prefix,title in (("Document_","Документ "),("Catalog_","Справочник "),("InformationRegister_","Регистр сведений "),("AccumulationRegister_","Регистр накопления ")):
        if name.startswith(prefix):
            name=title+name[len(prefix):]
            break
    translations={"TITLE":"Название","NAME":"Имя","LAST_NAME":"Фамилия","SECOND_NAME":"Отчество","SUBJECT":"Тема","ASSIGNED_BY_ID":"Ответственный","RESPONSIBLE_ID":"Ответственный"}
    if name in translations:
        return translations[name]
    name = name.replace("_Key", " ссылка").replace("_", " ")
    name = re.sub(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", " ", name)
    return {"Number": "Номер", "Description": "Наименование", "Date": "Дата", "Ref ссылка": "Ссылка"}.get(name, name)


class LiveSources:
    def __init__(self, config):
        self.config = config
        b24 = config.get("bitrix24", {})
        self.b24 = Bitrix24({**b24, "request_attempts": 1, "request_timeout_seconds": 25,"response_limit_bytes":4*1024*1024}, log=lambda *args: None) if b24.get("webhook") else None
        self.onec = OneC({**config.get("onec", {}), "request_timeout_seconds": 30,"response_limit_bytes":4*1024*1024}, log=lambda *args: None)
        self._b24_fields = {}
        self._metadata = None
        self._metadata_at = 0

    def execute(self, tool, query, privacy):
        if tool not in LIVE_TOOLS:
            raise ValueError("Неизвестный источник")
        query = query or ""
        if not isinstance(query,str) or len(query) > 4000:
            raise ValueError("Слишком длинный запрос источнику")
        try:
            params = json.loads(query) if query.strip().startswith("{") else {"text": query}
        except (TypeError, ValueError):
            raise ValueError("query должен содержать JSON с параметрами поиска") from None
        params = privacy.restore(params)
        if not isinstance(params, dict):
            raise ValueError("Параметры источника должны быть объектом")
        try:
            result = self.bitrix(tool, params, privacy) if tool.startswith("b24_") else self.odata(tool, params, privacy)
        except ValueError:
            raise
        except Exception as exc:
            if str(exc).startswith("Ответ Б24 превышает допустимый размер"):
                raise ValueError("Прямой запрос Б24 превышает лимит размера ответа") from None
            code = getattr(exc, "code", None)
            if code is None:
                match = re.search(r"HTTP\s+(\d{3})", str(exc))
                code = match[1] if match else None
            system = "Б24" if tool.startswith("b24_") else "1С"
            raise ValueError(f"Прямой запрос {system} завершился ошибкой" + (f" HTTP {code}" if code else " связи или прав доступа")) from None
        return {"origin": "Б24 — прямой запрос" if tool.startswith("b24_") else "1С — прямой запрос",
                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), **result}

    def value(self, field, value, privacy, entity="", type_hint="", depth=0):
        if depth > 4:
            return "Вложенность ограничена"
        if "Binary" in type_hint or re.search(r"binary|двоичные|file_content", field, re.I):
            return "Бинарные данные"
        if isinstance(value, dict):
            return {key: self.value(field if key == "VALUE" else key, item, privacy, entity, depth=depth + 1)
                    for key, item in list(value.items())[:40] if not key.startswith(("@", "odata."))}
        if isinstance(value, list):
            values = [self.value(field, item, privacy, entity, type_hint, depth + 1) for item in value[:20]]
            return {"values": values, "total": len(value), "truncated": len(value) > 20}
        if value is None or isinstance(value, bool):
            return value
        name = field.casefold()
        if type_hint in ("Edm.DateTime","Edm.DateTimeOffset") and isinstance(value,str) and re.fullmatch(r"[0-9TZ:.+\-]+",value):
            return value
        if name in ("company_title","rq_company_name","rq_company_full_name"):
            return privacy.register("КОМПАНИЯ",str(value))
        if name in ("rq_first_name","rq_last_name","rq_second_name","rq_full_name"):
            return privacy.register("ИМЯ",str(value))
        if (field == "ID" or name == "id" or name.endswith(("_id", "_key")) or name in ("responsibleid", "createdby")
                or type_hint == "Edm.Guid"):
            return privacy.register("ОБЪЕКТ", str(value))
        if re.search(r"phone|email|address|inn|kpp|bik|account|телефон|почт|адрес|инн|кпп|бик|огрн|счет|счёт", name) or name in ("inn", "kpp", "bik"):
            return privacy.register("РЕКВИЗИТ", str(value))
        if isinstance(value,str) and re.search(r"фио|фамили|отчеств|контактноелицо|ответственн",name):
            return privacy.register("ИМЯ",value)
        if isinstance(value,str) and re.search(r"партнер|партнёр|контрагент|названиеорганизац|company_name",name):
            return privacy.register("КОМПАНИЯ",value)
        if field in ("Number", "Code") or name in ("номер", "номердокумента"):
            return privacy.register("ДОКУМЕНТ", str(value))
        is_person = entity in ("contact", "user") or bool(re.search(r"Пользоват|Сотрудник|Физическ|Контактн.*Лиц", entity))
        is_company = entity == "company" or bool(re.search(r"Контрагент|Партнер|Партнёр|Организац|Банк", entity))
        if is_person and name in ("name", "last_name", "second_name", "description", "наименование", "полноенаименование", "наименованиеполное"):
            return privacy.register("ИМЯ", str(value))
        if is_company and name in ("title", "description", "наименование", "полноенаименование", "наименованиеполное"):
            return privacy.register("КОМПАНИЯ", str(value))
        if isinstance(value,str) and (re.search(r"opportunity|amount|price|сумма|цена|стоимость",name) or type_hint in ("money","double")):
            if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?(?:\|[A-Z]{3})?",value):
                return value
        if isinstance(value, str):
            masked = privacy.mask(value[:6000])
            return masked if len(masked) <= 1500 else {"text": masked[:1500], "truncated": True}
        return value

    def bitrix(self, tool, params, privacy):
        if self.b24 is None:
            raise ValueError("Подключение Б24 не настроено")
        if tool == "b24_card":
            identity = str(params.get("id") or params.get("text") or "")
            if identity.startswith(("http://","https://")):
                parsed = urlsplit(identity)
                if parsed.username or parsed.password or portal_origin({"webhook":identity}) != portal_origin(self.config):
                    raise ValueError("Ссылка должна вести на подключённый портал Б24")
                match = re.fullmatch(r"/crm/(company|contact|lead|deal)/details/([1-9]\d*)/?",parsed.path)
                if not match:
                    raise ValueError("Укажите ссылку на карточку Б24")
                params = {**params,"entity":match[1],"id":match[2]}
        entity = params.get("entity", "company")
        if entity not in B24:
            raise ValueError("Ожидается company, contact, lead, deal, activity, task или user")
        prefix, search_field = B24[entity]
        offset = self.offset(params)
        if tool == "b24_fields":
            method = prefix + (".getFields" if entity == "task" else ".fields")
            fields = self.b24.call(method).get("result", {})
            fields = fields.get("fields", fields) if isinstance(fields, dict) else {}
            text = str(params.get("text", "")).casefold()
            rows = []
            for name, meta in fields.items():
                title = next((meta[k] for k in ("formLabel", "listLabel", "title") if isinstance(meta.get(k), str) and meta[k]), name)
                if text and text not in (name + " " + title).casefold():
                    continue
                rows.append({"field": privacy.register("ПОЛЕ", name), "label": privacy.mask(title), "type": meta.get("type")})
            return {"entity": entity, "fields": rows[offset:offset + 40], "total": len(rows), "next_offset": offset + 40 if len(rows) > offset + 40 else None}
        if tool == "b24_card":
            identity = str(params.get("id", ""))
            if not re.fullmatch(r"[1-9]\d{0,19}", identity):
                raise ValueError("Укажите id карточки из результатов поиска")
            method = prefix + (".get" if entity != "user" else ".get")
            arguments = {"taskId": identity} if entity == "task" else {"ID": identity} if entity == "user" else {"id": identity}
            data = self.b24.call(method, arguments).get("result", {})
            data = data.get("task", data) if isinstance(data, dict) else next(iter(data), {})
            selected = params.get("fields")
            if selected is not None:
                if not isinstance(selected, list) or len(selected) > 40 or any(not isinstance(v, str) or v not in data for v in selected):
                    raise ValueError("Укажите до 40 существующих полей карточки")
                data = {name: data[name] for name in selected}
            if entity not in self._b24_fields:
                metadata_response=self.b24.call(prefix + (".getFields" if entity == "task" else ".fields")).get("result",{})
                self._b24_fields[entity]=metadata_response.get("fields",metadata_response) if isinstance(metadata_response,dict) else {}
            rows = [self.b24_row(entity, data, privacy, identity, offset)]
            return {"rows": rows}
        filters = {}
        text = str(params.get("text", ""))
        if len(text) > 300:
            raise ValueError("Поисковая строка слишком длинная")
        if text:
            filters["%" + search_field] = text
        if params.get("responsible"):
            responsible = str(params["responsible"])
            if not responsible.isdigit():
                raise ValueError("responsible должен быть ссылкой на пользователя Б24")
            filters["RESPONSIBLE_ID" if entity in ("task", "activity") else "ASSIGNED_BY_ID"] = responsible
        if entity == "user":
            filters={"FIND":text} if text else {}
        if entity == "activity":
            if params.get("owner_id"):
                owner_type = {"company": 4, "contact": 3, "deal": 2, "lead": 1}.get(params.get("owner_type", "company"))
                if owner_type is None or not str(params["owner_id"]).isdigit():
                    raise ValueError("Укажите корректную карточку владельца дела")
                filters.update(OWNER_TYPE_ID=owner_type, OWNER_ID=str(params["owner_id"]))
            if params.get("open") or params.get("overdue"):
                filters["COMPLETED"] = "N"
            if params.get("overdue"):
                filters["<DEADLINE"] = dt.datetime.now().isoformat(timespec="seconds")
        arguments = {"filter": filters, "start": offset, "order": {"ID": "ASC"}, "select": ["ID","TITLE","NAME","LAST_NAME","COMPANY_ID","CONTACT_ID","ASSIGNED_BY_ID","RESPONSIBLE_ID","STATUS_ID","STAGE_ID","OPPORTUNITY","DATE_MODIFY","DEADLINE","COMPLETED","SUBJECT"]}
        if entity=="user":
            arguments={"FILTER":filters,"start":offset}
        elif entity=="task":
            arguments["select"]=["ID","TITLE","RESPONSIBLE_ID","CREATED_BY","DEADLINE","STATUS","DESCRIPTION","GROUP_ID"]
        response = self.b24.call(prefix + (".get" if entity == "user" else ".list"), arguments)
        rows = response.get("result", [])
        rows = rows.get("tasks", []) if isinstance(rows, dict) else rows
        return {"rows": [self.b24_row(entity, row, privacy) for row in rows[:20]], "total": response.get("total"),
                "next_offset": offset + min(len(rows), 20) if len(rows) > 20 else response.get("next")}

    def b24_row(self, entity, row, privacy, identity=None, offset=0):
        identity = str(identity or row.get("ID") or row.get("id") or "")
        title = str(row.get("TITLE") or row.get("title") or row.get("SUBJECT") or " ".join(str(row.get(k) or "") for k in ("NAME", "LAST_NAME")).strip() or f"{entity} {identity}")
        sid = privacy.source("live:b24:" + entity + ":" + identity, title, object_url(self.config, entity if entity != "task" else "task", identity, row.get("responsibleId") or row.get("RESPONSIBLE_ID") or 0), "Б24 — прямой запрос")
        privacy.sources[sid].update(title=title,fetched_at=dt.datetime.now().isoformat(timespec="seconds"))
        # Register the full fresh identity before processing other text fields.
        if entity in ("company", "contact", "user"):
            privacy.register("КОМПАНИЯ" if entity == "company" else "ИМЯ", title)
        fields, refs = [], []
        for name, value in list(row.items())[offset:offset + 60]:
            meta=self._b24_fields.get(entity,{}).get(name,{})
            title=next((meta[k] for k in ("formLabel","listLabel","title") if isinstance(meta.get(k),str) and meta[k]),label(name))
            hint=str(meta.get("USER_TYPE_ID") or meta.get("type") or "")
            sensitive_name=name+" "+title
            if name.startswith("UF_") and hint in ("employee","crm","file","iblock_element"):
                protected=privacy.register("ОБЪЕКТ",str(value)) if value is not None else None
            elif name.startswith("UF_") and re.search(r"фио|фамили|имя|отчеств",title,re.I) and value:
                protected=privacy.register("ИМЯ",str(value))
            else:
                protected=self.value(sensitive_name if re.search(r"phone|email|address|inn|kpp|bik|account|телефон|почт|адрес|инн|кпп|бик|огрн|счет|счёт",sensitive_name,re.I) else name,value,privacy,entity,"Edm.DateTime" if hint in ("date","datetime") else hint)
                if name in ("ID","NAME","LAST_NAME","SECOND_NAME","TITLE"):
                    protected=self.value(name,value,privacy,entity,hint)
            fields.append({"field": privacy.register("ПОЛЕ", name), "label": privacy.mask(title), "value": protected})
            if name in RELATIONS and str(value or "").isdigit() and int(value):
                refs.append({"entity": RELATIONS[name], "id": privacy.register("ОБЪЕКТ", str(value))})
        for id_field,type_field in (("OWNER_ID","OWNER_TYPE_ID"),("ENTITY_ID","ENTITY_TYPE_ID")):
            related_type={"1":"lead","2":"deal","3":"contact","4":"company"}.get(str(row.get(type_field)))
            related_id=str(row.get(id_field) or "")
            if related_type and related_id.isdigit() and int(related_id):
                refs.append({"entity":related_type,"id":privacy.register("ОБЪЕКТ",related_id)})
        return {"entity": entity, "id": privacy.register("ОБЪЕКТ", identity), "source": sid, "fields": fields,
                "references": refs, "total_fields": len(row), "next_field_offset": offset + 60 if len(row) > offset + 60 else None}

    def offset(self, params):
        offset = params.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset должен быть целым числом от 0 до 10000")
        return offset

    def metadata(self):
        if self._metadata is not None and time.monotonic() - self._metadata_at < 300:
            return self._metadata
        if not self.config.get("onec", {}).get("base_url"):
            raise ValueError("Подключение 1С не настроено")
        root = ET.fromstring(self.onec.metadata())
        types, entities, associations = {}, {}, {}
        for schema in root.findall(".//{*}Schema"):
            namespace = schema.get("Namespace", "")
            for definition in schema.findall("{*}EntityType"):
                types[namespace + "." + definition.get("Name")] = definition
            for association in schema.findall("{*}Association"):
                associations[namespace + "." + association.get("Name")] = {e.get("Role"): e.get("Type") for e in association.findall("{*}End")}
            for item in schema.findall(".//{*}EntitySet"):
                name = item.get("Name", "")
                if re.fullmatch(r"[\w]+", name) and name.startswith(("Document_", "Catalog_", "InformationRegister_", "AccumulationRegister_", "Enum_", "ChartOf")):
                    entities[name] = {"type": item.get("EntityType"), "fields": {}, "references": {}}
        by_type = {value["type"]: name for name, value in entities.items()}
        for name, value in entities.items():
            definition = types.get(value["type"])
            if definition is None:
                continue
            value["fields"] = {p.get("Name"): p.get("Type", "") for p in definition.findall("{*}Property")}
            for nav in definition.findall("{*}NavigationProperty"):
                target = associations.get(nav.get("Relationship"), {}).get(nav.get("ToRole"))
                if target in by_type:
                    value["references"][nav.get("Name") + "_Key"] = by_type[target]
        self._metadata, self._metadata_at = entities, time.monotonic()
        return entities

    def odata(self, tool, params, privacy):
        metadata = self.metadata()
        offset = self.offset(params)
        if tool == "onec_entities":
            text = str(params.get("text", "")).casefold()
            names = [name for name in metadata if text in name.casefold()]
            return {"entities": [{"entity": privacy.register("ТИП", name), "label": privacy.mask(label(name))} for name in names[offset:offset + 40]],
                    "total": len(names), "next_offset": offset + 40 if len(names) > offset + 40 else None}
        entity = params.get("entity")
        if entity not in metadata:
            raise ValueError("Выберите entity из onec_entities")
        fields = metadata[entity]["fields"]
        if tool == "onec_schema":
            text = str(params.get("text", "")).casefold()
            names = [name for name in fields if text in name.casefold()]
            return {"entity": privacy.register("ТИП", entity), "fields": [
                {"field": privacy.register("ПОЛЕ", name), "label": privacy.mask(label(name)), "type": fields[name] if fields[name].startswith("Edm.") else "Collection" if fields[name].startswith("Collection(") else "Complex"}
                for name in names[offset:offset + 40]], "total": len(names), "next_offset": offset + 40 if len(names) > offset + 40 else None}
        selected = params.get("fields") or [name for name in fields if "Binary" not in fields[name]][:40]
        if not isinstance(selected, list) or len(selected) > 40 or any(name not in fields for name in selected):
            raise ValueError("Укажите до 40 полей из onec_schema")
        selected = list(dict.fromkeys(["Ref_Key", *selected])) if "Ref_Key" in fields else selected
        arguments = {"$format": "json", "$top": 21, "$skip": offset, "$select": ",".join(selected)}
        if "Ref_Key" in fields:
            arguments["$orderby"] = "Ref_Key"
        filters = []
        if tool == "onec_card":
            try:
                identity = str(uuid.UUID(str(params.get("id", ""))))
            except ValueError:
                raise ValueError("Укажите id документа из onec_search") from None
            filters.append("Ref_Key eq guid'" + identity + "'")
        else:
            field = params.get("field") or ("Number" if entity.startswith("Document_") else "Description")
            if field not in fields:
                raise ValueError("Выберите поле поиска из onec_schema")
            text = str(params.get("text", ""))
            if not text or len(text) > 300:
                raise ValueError("Укажите text длиной до 300 символов")
            kind=fields[field]
            operation=params.get("match","contains" if kind=="Edm.String" else "equals")
            operators={"equals":"eq","ge":"ge","gt":"gt","le":"le","lt":"lt"}
            if kind=="Edm.String":
                if operation not in ("contains","equals"):
                    raise ValueError("Для текста поддерживаются contains и equals")
                escaped=text.replace("'","''")
                expression=field+" eq '"+escaped+"'" if operation=="equals" else "substringof('"+escaped+"',"+field+")"
            else:
                if operation not in operators:
                    raise ValueError("Выберите equals/ge/gt/le/lt для числового поля")
                if kind=="Edm.Guid":
                    if operation!="equals":
                        raise ValueError("Ссылки сравниваются через equals")
                    try:
                        literal="guid'"+str(uuid.UUID(text))+"'"
                    except ValueError:
                        raise ValueError("Некорректная ссылка для поля Guid") from None
                elif kind in ("Edm.Decimal","Edm.Double","Edm.Single","Edm.Int16","Edm.Int32","Edm.Int64"):
                    if not re.fullmatch(r"[+-]?\d{1,24}(?:\.\d{1,12})?",text):
                        raise ValueError("Используйте обычное десятичное число до 24 цифр и 12 знаков после точки")
                    try:
                        number=Decimal(text)
                        if not number.is_finite() or abs(number)>Decimal('1e24'):
                            raise ValueError()
                        literal=format(number,"f")
                    except (InvalidOperation,ValueError):
                        raise ValueError("Укажите корректное число") from None
                elif kind=="Edm.Boolean" and text.lower() in ("true","false"):
                    literal=text.lower()
                else:
                    raise ValueError("Для этого типа поля поиск пока недоступен")
                expression=field+" "+operators[operation]+" "+literal
            filters.append(expression)
            if params.get("month"):
                month = str(params["month"])
                first, _last = month_bounds(month)
                next_first, _ = month_bounds(month_add(month, 1))
                if "Date" not in fields:
                    raise ValueError("У этого объекта нет поля Date")
                filters += [f"Date ge datetime'{first.isoformat()}T00:00:00'", f"Date lt datetime'{next_first.isoformat()}T00:00:00'"]
        arguments["$filter"] = " and ".join(filters)
        records = self.onec._odata(entity, arguments)
        rows = []
        for row in records[:20]:
            identity = str(row.get("Ref_Key", ""))
            title = str(row.get("Number") or row.get("Description") or label(entity))
            sid = privacy.source("live:onec:" + entity + ":" + identity, title, None, "1С — прямой запрос")
            privacy.sources[sid].update(title=title,fetched_at=dt.datetime.now().isoformat(timespec="seconds"))
            values, refs = [], []
            for name, value in row.items():
                if name not in fields:
                    continue
                values.append({"field": privacy.register("ПОЛЕ", name), "label": privacy.mask(label(name)), "value": self.value(name, value, privacy, entity, fields[name])})
                target = metadata[entity]["references"].get(name)
                if target and value and str(value) != "00000000-0000-0000-0000-000000000000":
                    refs.append({"entity": privacy.register("ТИП", target), "id": privacy.register("ОБЪЕКТ", str(value))})
            rows.append({"entity": privacy.register("ТИП", entity), "id": privacy.register("ОБЪЕКТ", identity), "source": sid, "fields": values, "references": refs})
        return {"rows": rows, "has_more": len(records) > 20, "next_offset": offset + 20 if len(records) > 20 else None}
