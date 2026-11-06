"""Bounded, parameterized, read-only access to synchronized dashboard data."""
import datetime as dt
import json
import re

from . import call_analysis, money, sync
from .common import current_month, db_lock, month_bounds
from .crm_links import record_url
from .engine import Engine
from .anonymize import GENERIC
from .gpt_live import LiveSources, LIVE_TOOLS

TOOLS = ["employees", "kpi", "search", "calls", "call_detail", "money", "sync_status", *LIVE_TOOLS]
SEARCH = {
    "clients": ("id,name,emp,need,strategy,updated", ["name"], "name"),
    "crm_deals": ("id,entity,title,client,emp,stage,amount,created,stage_since,client_ref", ["title", "client"], "title"),
    "conversion_orders": ("id,doc,client,emp,dt,status,amount,role", ["doc", "client"], "doc"),
    "supplier_orders": ("id,doc,supplier,emp,dt,amount_novat,planned_date,actual_date,customer_order", ["doc", "supplier", "customer_order"], "doc"),
    "receivables": ("id,doc,client,emp,amount,due_date,paid_date,order_date", ["doc", "client"], "doc"),
}


class DataTools:
    def __init__(self, con, config, rules):
        self.con, self.config, self.engine = con, config, Engine(con, rules, config)
        self.live = LiveSources(config)
        con.create_function("gpt_casefold",1,lambda value:str(value or "").casefold())

    def seed(self, privacy):
        privacy.config_secrets(self.config)
        for employee in self.config.get("employees", []):
            label = privacy.register("СОТРУДНИК", employee["name"])
            privacy.alias(label, employee["login"])
            privacy.alias(label, employee.get("onec_manager"))
            for word in re.findall(r"[А-Яа-яЁёA-Za-z-]{3,}",employee["name"]):
                privacy.register("СОТРУДНИК",word)
        # The existing call masker intentionally preserves our company; GPT must mask it too.
        for name in ("ТрансТехКом", "Транстехком", "ТТК"):
            privacy.register("КОМПАНИЯ", name)
        names = []
        with db_lock():
            for table, field in (("clients", "name"), ("crm_deals", "client"), ("conversion_orders", "client"),
                                 ("supplier_orders", "supplier"), ("receivables", "client")):
                for row in self.con.execute(f"SELECT DISTINCT {field} FROM {table} WHERE {field} IS NOT NULL"):
                    names.append(row[0])
        for name in names:
            privacy.register("КОМПАНИЯ", name)
            for word in re.findall(r"[А-Яа-яЁёA-Za-z-]{4,}",name or ""):
                if word.casefold() not in GENERIC:
                    privacy.register("КОМПАНИЯ",word)

    def employee(self, value):
        value = value.strip().casefold()
        choices = [e for e in self.config.get("employees", []) if e.get("position")]
        exact = [e for e in choices if value in (e["login"].casefold(), e["name"].casefold())]
        matches = exact or [e for e in choices if value and value in e["name"].casefold()]
        if len(matches) != 1:
            raise ValueError("Уточните сотрудника через employees: имя не найдено или неоднозначно")
        return matches[0]

    def execute(self, tool, query, employee, month, privacy):
        if tool not in TOOLS:
            raise ValueError("Неизвестный инструмент; произвольный SQL и изменение данных недоступны")
        if tool in LIVE_TOOLS:
            return self.live.execute(tool, query, privacy)
        query, employee = privacy.restore(query or ""), privacy.restore(employee or "")
        month = month or current_month()
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Месяц должен быть в формате ГГГГ-ММ")
        first, last = month_bounds(month)
        with db_lock():
            if tool == "employees":
                rows = []
                for e in self.config.get("employees", []):
                    sid = privacy.source("employee:" + e["login"], e["name"], "#emp-" + e["login"])
                    aliases = [privacy.register("СОТРУДНИК", word) for word in re.findall(r"[А-Яа-яЁёA-Za-z-]{3,}", e["name"])]
                    rows.append({"employee": e["name"], "aliases":list(dict.fromkeys(aliases)), "position": e.get("position"), "department": e.get("department"), "b24_id":privacy.register("ОБЪЕКТ",e["b24_user_id"]) if e.get("b24_user_id") else None, "source": sid})
                result = {"employees": rows}
            elif tool == "kpi":
                e = self.employee(employee or query)
                report = self.engine.compute(e["login"], month)
                result = {"employee": e["name"], "month": month, "payout": report["payout"], "items": []}
                for item in report["items"]:
                    result["items"].append({key: item[key] for key in ("n", "code", "title", "pct", "amount", "payout", "status", "components")})
                    for field in ("funnel", "extra", "details"):
                        if field in item:
                            value = item[field]
                            if field == "details":
                                value = {"columns": value.get("columns"), "rows": value.get("rows", [])[:20],
                                         "total_rows": len(value.get("rows", []))}
                            result["items"][-1][field] = value
                result["source"] = privacy.source("kpi:" + e["login"] + ":" + month,
                                                   "KPI " + e["name"] + " за " + month, "#emp-" + e["login"])
                privacy.sources[result["source"]]["month"] = month
            elif tool == "search":
                if not query.strip() or len(query) > 300:
                    raise ValueError("Укажите имя, компанию или номер документа (до 300 символов)")
                rows, totals = [], {}
                for table, (columns, fields, title) in SEARCH.items():
                    sql = f"SELECT {columns} FROM {table} WHERE " + " OR ".join(f"gpt_casefold({field}) LIKE ?" for field in fields) + " LIMIT 12"
                    parameters=["%" + query.casefold() + "%"] * len(fields)
                    found = self.con.execute(sql, parameters).fetchall()
                    count_sql=f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(f"gpt_casefold({field}) LIKE ?" for field in fields)
                    totals[table]=self.con.execute(count_sql,parameters).fetchone()[0]
                    for row in found:
                        data = dict(row)
                        raw_id = str(data["id"])
                        sid = privacy.source(table + ":" + raw_id, str(data.get(title) or raw_id), record_url(self.config, table, data),
                                             "Битрикс24" if table in ("clients", "crm_deals") else "1С")
                        data["id"] = privacy.register("ОБЪЕКТ", raw_id)
                        rows.append({"dataset": table, "source": sid, **data})
                result = {"rows": rows, "totals":totals,"period":"Все сохранённые периоды; поиск не ограничен выбранным месяцем", "limit_per_dataset": 12, "note": "Поиск по сохранённым данным; список может быть неполным"}
            elif tool in ("calls", "call_detail"):
                args = []
                where = "c.id=?" if tool == "call_detail" else "c.dt>=? AND c.dt<=?"
                args += [query] if tool == "call_detail" else [first.isoformat(), last.isoformat() + "T23:59:59"]
                if employee:
                    where += " AND c.emp=?"
                    args.append(self.employee(employee)["login"])
                if query and tool == "calls":
                    where += " AND gpt_casefold(c.client) LIKE ?"
                    args.append("%" + query.casefold() + "%")
                rows = []
                for row in self.con.execute("SELECT c.id,c.emp,c.dt,c.direction,c.duration,c.client,c.client_ref,c.crm_activity_id,c.purged "
                                            "FROM call_records c WHERE " + where + " ORDER BY c.dt DESC LIMIT 20", args):
                    data = dict(row)
                    rid = data["id"]
                    sid = privacy.source("call:" + rid, "Звонок " + data["dt"], record_url(self.config, "call_records", data), "Битрикс24")
                    data["analysis"] = call_analysis.get(self.con, rid)
                    if tool == "call_detail" and not data["purged"]:
                        transcript = self.con.execute("SELECT segments FROM call_transcripts WHERE id=?", (rid,)).fetchone()
                        if transcript:
                            lines = call_analysis.sentences(json.loads(transcript["segments"] or "[]"))
                            data["sentences"] = lines[:120]
                            data["total_sentences"] = len(lines)
                    data.pop("crm_activity_id", None)
                    data.pop("client_ref", None)
                    data.update(id=privacy.register("ОБЪЕКТ", rid), source=sid)
                    rows.append(data)
                result = {"calls": rows,"total_count":self.con.execute("SELECT COUNT(*) FROM call_records c WHERE "+where,args).fetchone()[0], "limit": 20}
            elif tool == "money":
                report = money.build(self.con, date_from=first.isoformat(), date_to=last.isoformat())
                # Full calendar is not useful in a prompt; keep scalar summaries and bounded lists.
                result = {key: value[:15] if isinstance(value, list) else value for key, value in report.items() if key != "calendar"}
                result["source"] = privacy.source("money:" + month, "Деньги за " + month, "#money", "1С / Дашборд")
            else:
                latest = sync.last_sync(self.con)
                result = {key: latest.get(key) for key in ("started", "finished", "status")} if latest else {"status": "Нет синхронизаций"}
                result["source"] = privacy.source("sync", "Статус обновления данных", None)
        # All identifiers and known employee logins are replaced before general text masking.
        return self.protect(result, privacy)

    def protect(self, value, privacy):
        title = " ".join(str(value.get(key) or "") for key in ("title", "name", "filename", "file_name", "doc", "basis", "entity", "type")) if isinstance(value, dict) else ""
        with privacy.document(title):
            return self._protect(value, privacy)

    def _protect(self, value, privacy):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "id":
                    out[key] = item if isinstance(item,str) and item.startswith("[") else privacy.register("ОБЪЕКТ", item)
                elif key in ("doc","customer_order","basis"):
                    out[key] = privacy.register("ДОКУМЕНТ",item) if item else item
                elif key in ("phone","inn","kpp","account","bik","email","address","bank_account"):
                    out[key] = privacy.register("РЕКВИЗИТ",item) if item else item
                elif key in ("client","supplier","party"):
                    out[key] = privacy.register("КОМПАНИЯ",item) if item else item
                elif key in ("emp", "login", "responsible_id"):
                    e = next((e for e in self.config.get("employees", []) if e["login"] == item), None)
                    out[key] = privacy.register("СОТРУДНИК", e["name"] if e else item) if item else item
                elif key in ("client_ref", "order_key", "customer_order_key", "author_key"):
                    out[key] = privacy.register("ОБЪЕКТ", item) if item else item
                elif key not in ("url", "pii", "links", "file", "record_url"):
                    out[key] = self.protect(item, privacy)
            return out
        if isinstance(value, (list, tuple)):
            return [self.protect(item, privacy) for item in value]
        return privacy.mask(value) if isinstance(value, str) else value
