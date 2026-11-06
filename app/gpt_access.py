"""Server-owned identity, position and source boundaries for GPT."""
import copy
import hashlib
import json
import uuid


PROFILES = {
    "pm": ("sales", "Продажи", {"clients", "crm_deals", "conversion_orders", "receivables"}),
    "tender": ("tender", "Тендеры", {"clients", "crm_deals", "conversion_orders", "receivables"}),
    "tender_lead": ("tender", "Тендеры", {"clients", "crm_deals", "conversion_orders", "receivables"}),
    "supply": ("supply", "Снабжение", {"supplier_orders", "supply_funnel", "supply_contracts"}),
    "logistics": ("logistics", "Логистика", {"transport_requests", "forwarder_orders"}),
}
B24_FIELDS = {
    "company": {"ID", "TITLE", "ASSIGNED_BY_ID", "PHONE", "EMAIL", "INDUSTRY", "COMPANY_TYPE"},
    "contact": {"ID", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "PHONE", "EMAIL", "POST"},
    "lead": {"ID", "TITLE", "NAME", "LAST_NAME", "STATUS_ID", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY"},
    "deal": {"ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY"},
    "activity": {"ID", "SUBJECT", "RESPONSIBLE_ID", "OWNER_ID", "OWNER_TYPE_ID", "DEADLINE", "START_TIME", "COMPLETED", "DESCRIPTION", "PROVIDER_ID", "TYPE_ID"},
    "task": {"id", "title", "responsibleId", "deadline", "status", "description", "ID", "TITLE", "RESPONSIBLE_ID", "DEADLINE", "STATUS", "DESCRIPTION"},
    "user": {"ID", "NAME", "LAST_NAME", "SECOND_NAME", "WORK_POSITION"},
}
ONEC_FIELDS = {"Ref_Key", "Number", "Date", "Posted", "DeletionMark", "Статус", "ТТК_Статус", "СтатусЗаявки"}
ONEC = {
    "sales": {"Document_ЗаказКлиента": ("Менеджер", {"СуммаДокумента", "Сумма", "Контрагент_Key", "Валюта_Key"})},
    "tender": {"Document_ЗаказКлиента": ("Менеджер", {"СуммаДокумента", "Сумма", "Контрагент_Key", "Валюта_Key"})},
    "supply": {"Document_ТТК_ЗаявкаВОтделСнабжения": ("ОтветственныйСнабжение", {"ДокументОснования", "Поставщик1_Key", "Поставщик2_Key", "Поставщик3_Key", "Поставщик4_Key", "Поставщик5_Key"})},
    "logistics": {
        "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов": ("ОтветственныйЛогист", {"ДатаИсполнения", "АдресДоставки", "АдресОтправки", "СтоимостьСНДС"}),
        "Document_ПоручениеЭкспедитору": ("ТТК_ОтветственныйЛогист", {"ТТК_ДатаОтгрузки", "ТТК_ДатаДоставки", "ДатаДоставки", "АдресДоставки", "ТТК_АдресОтгрузки", "ТТК_АдресДоставки", "ТТК_ТранспортнаяКомпания_Key"}),
    },
}


class Access:
    def __init__(self, config, login):
        employee = next((e for e in config.get("employees", []) if e.get("login") == login), None)
        if employee is None:
            raise ValueError("Пользователь GPT не найден")
        self.employee = copy.deepcopy(employee)
        employee = self.employee
        self.login = login
        self.admin = employee.get("role") == "manager"
        self.position = employee.get("position") or ""
        self.domain, self.label, self.tables = PROFILES.get(self.position, ("", "Должность не назначена", set()))
        identity = {key: employee.get(key) for key in ("login", "role", "position", "name", "b24_user_id", "onec_manager")}
        if self.admin:
            identity = {"login": login, "role": "manager"}
        else:
            identity["listen_own"] = config.get("call_records", {}).get("listen_own", True)
        self.signature = "v1:" + hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def public(self):
        return {"admin": self.admin, "domain": self.domain, "label": "Все рабочие данные" if self.admin else self.label,
                "description": "Администратор — все сотрудники и направления" if self.admin else "Только свои данные и вопросы своей должности"}

    def prompt(self):
        return "ПРАВА СЕАНСА (заданы сервером):\n" + json.dumps({**self.public(), "login": self.login, "name": self.employee.get("name"), "position": self.position}, ensure_ascii=False)

    def allows_intent(self, intent):
        if self.admin:
            return True
        if not isinstance(intent, dict):
            return False
        domains = intent.get("domains")
        if not isinstance(domains, list) or not domains or any(not isinstance(d, str) for d in domains) or intent.get("other_people") is not False:
            return False
        return set(domains).issubset({"general", "self", self.domain} - {""})

    def check_employee(self, value):
        if not value:
            return
        allowed = {self.login.casefold(), str(self.employee.get("name") or "").casefold(), "я", "мой", "мои"}
        allowed.update(word.casefold() for word in str(self.employee.get("name") or "").split() if len(word) >= 3)
        if str(value).strip().casefold() not in allowed:
            raise ValueError("Доступны только ваши собственные данные")

    def check_tool(self, tool):
        if self.admin:
            return
        if tool == "money" or tool == "kpi" and self.position not in PROFILES:
            raise ValueError("Этот источник недоступен для вашей должности")

    def uid(self):
        value = str(self.employee.get("b24_user_id") or "")
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("Для доступа к своим данным требуется ID Битрикс24 в карточке сотрудника")
        return value

    def b24_fields(self, entity):
        allowed = {"user", "task", "activity"}
        if self.domain in ("sales", "tender"):
            allowed |= {"company", "contact", "lead", "deal"}
        if entity not in allowed:
            raise ValueError("Тип карточки Б24 недоступен для вашей должности")
        return B24_FIELDS[entity]

    def b24_owns(self, entity, row):
        field = "ID" if entity == "user" else "responsibleId" if entity == "task" else "RESPONSIBLE_ID" if entity == "activity" else "ASSIGNED_BY_ID"
        owner = row.get(field)
        if entity == "task":
            owner = owner or row.get("RESPONSIBLE_ID")
        if str(owner or "") != self.uid():
            raise ValueError("Карточка не относится к вашим данным")

    def onec_entities(self):
        return ONEC.get(self.domain, {})

    def onec_scope(self, entity):
        spec = self.onec_entities().get(entity)
        if spec is None:
            raise ValueError("Тип документа 1С недоступен для вашей должности. Свои связанные документы можно найти через search")
        owner, fields = spec
        names = self.employee.get("onec_manager") or []
        names = [names] if isinstance(names, str) else names
        names = names if isinstance(names, list) else []
        names = list(dict.fromkeys(names or [self.employee.get("name")]))
        names = [str(n).strip() for n in names if isinstance(n, str) and n.strip()]
        if not names:
            raise ValueError("Не настроена связь сотрудника с ответственным в 1С")
        filters = []
        for name in names:
            try:
                identity = str(uuid.UUID(name))
            except ValueError:
                filters.append(owner + "/Description eq '" + name.replace("'", "''") + "'")
            else:
                filters.append(owner + "_Key eq guid'" + identity + "'")
        return ONEC_FIELDS | fields, "(" + " or ".join(filters) + ")"
