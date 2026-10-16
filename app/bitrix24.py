"""Загрузка данных из Битрикс24 через входящий вебхук (REST API) и нормализация в таблицы дашборда."""
import datetime as dt
import json
import re
import time
import urllib.request

from .common import parse_date

OWNER_TYPES = {"LEAD": 1, "DEAL": 2, "CONTACT": 3, "COMPANY": 4}


class B24Error(Exception):
    pass


def order_key(num):
    """Номер заказа клиента 1С для сопоставления: «00ЦБ-000067», «ЦБ-67» и «67» дают одно и то же — 67."""
    m = re.search(r"(\d+)\s*$", str(num or "").strip())
    return str(int(m.group(1))) if m else None


class Bitrix24:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.url = cfg["webhook"].rstrip("/") + "/"
        self.log = log
        self.f = cfg.get("fields", {})

    # ---------- транспорт
    def call(self, method, params=None):
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(self.url + method + ".json", data=data,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    res = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                if "QUERY_LIMIT_EXCEEDED" in body and attempt < 4:
                    time.sleep(2 + attempt * 2)
                    continue
                raise B24Error(f"{method}: HTTP {e.code} {body[:300]}")
        if "error" in res:
            raise B24Error(f"{method}: {res.get('error')} {res.get('error_description', '')}")
        time.sleep(0.4)  # лимит Б24 — около 2 запросов в секунду
        return res

    def list_all(self, method, params, key=None):
        """Постраничная выборка (по 50 записей)."""
        out, start = [], 0
        while True:
            res = self.call(method, {**params, "start": start})
            chunk = res.get("result", [])
            if key:
                chunk = chunk.get(key, [])
            out.extend(chunk)
            if "next" not in res:
                return out
            start = res["next"]

    def by_ids(self, method, ids, select):
        ids = list({int(i) for i in ids if i})
        out = {}
        for i in range(0, len(ids), 50):
            for r in self.list_all(method, {"filter": {"ID": ids[i:i + 50]}, "select": select}):
                out[str(r["ID"])] = r
        return out

    # ---------- утилиты
    @staticmethod
    def flag(v):
        if v is None or v is False:
            return 0
        if isinstance(v, (list, dict)):
            return int(bool(v))
        s = str(v).strip().lower()
        return 0 if s in ("", "n", "0", "нет", "false", "none") else 1

    @staticmethod
    def num(v):
        """Число из поля Б24 (поле типа «Деньги» приходит как «100000|RUB»)."""
        if v in (None, "", False):
            return None
        try:
            return float(str(v).split("|")[0].replace(" ", "").replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def iso(v):
        if not v:
            return None
        return str(v)[:19]

    # ---------- сбор
    def fetch(self, employees, date_from):
        """employees: {b24_user_id: login}. Возвращает словарь таблица → список строк."""
        uids = [int(u) for u in employees]
        emp = lambda uid: employees.get(int(uid)) if uid not in (None, "") else None
        df = date_from.isoformat()
        df_long = (date_from - dt.timedelta(days=150)).isoformat()
        out = {}

        # активности CRM (встречи, следующие шаги)
        self.log("Б24: дела CRM")
        acts = self.list_all("crm.activity.list", {
            "filter": {"RESPONSIBLE_ID": uids, ">=CREATED": df},
            "select": ["ID", "OWNER_TYPE_ID", "OWNER_ID", "TYPE_ID", "PROVIDER_ID", "DIRECTION", "CREATED", "START_TIME",
                       "END_TIME", "COMPLETED", "SUBJECT", "DESCRIPTION", "RESPONSIBLE_ID", "STORAGE_ELEMENT_IDS", "FILES",
                       "LAST_UPDATED"],
            "order": {"ID": "ASC"}})
        by_owner = {}
        for a in acts:
            by_owner.setdefault((int(a["OWNER_TYPE_ID"] or 0), str(a["OWNER_ID"])), []).append(a)

        # звонки (телефония)
        self.log("Б24: звонки")
        calls = self.list_all("voximplant.statistic.get", {
            "FILTER": {">=CALL_START_DATE": df, "PORTAL_USER_ID": uids, "CALL_TYPE": 1},
            "SORT": "CALL_START_DATE", "ORDER": "ASC"})
        # к чему привязаны звонки и встречи: контакт, компания или лид
        bound = {"CONTACT": set(), "COMPANY": set(), "LEAD": set()}
        for c in calls:
            if c.get("CRM_ENTITY_TYPE") in bound and c.get("CRM_ENTITY_ID"):
                bound[c["CRM_ENTITY_TYPE"]].add(str(c["CRM_ENTITY_ID"]))
        type_names = {v: k for k, v in OWNER_TYPES.items()}
        for a in acts:
            t = type_names.get(int(a.get("OWNER_TYPE_ID") or 0))
            if t in bound and a.get("OWNER_ID"):
                bound[t].add(str(a["OWNER_ID"]))

        lpr_field = self.f.get("contact_is_lpr")
        lead_lpr_field = self.f.get("lead_lpr")
        contact_select = ["ID", "NAME", "LAST_NAME", "POST", "COMPANY_ID"] + ([lpr_field] if lpr_field else [])
        contacts = self.by_ids("crm.contact.list", bound["CONTACT"], contact_select)
        companies_of_calls = self.by_ids("crm.company.list",
                                         bound["COMPANY"] | {str(c["COMPANY_ID"]) for c in contacts.values() if c.get("COMPANY_ID")},
                                         ["ID", "TITLE"])
        leads_of_calls = self.by_ids("crm.lead.list", bound["LEAD"], ["ID", "TITLE"] + ([lead_lpr_field] if lead_lpr_field else []))
        secr = [s.lower() for s in self.cfg.get("secretary_posts", [])]

        def contact_flags(c):
            if not c:
                return 0, 0
            post = (c.get("POST") or "").lower()
            is_secr = int(any(s in post for s in secr))
            is_lpr = self.flag(c.get(lpr_field)) if lpr_field else int(bool(post) and not is_secr)
            return is_lpr, is_secr

        # компании, у которых в CRM уже есть контакт-ЛПР: звонок по такой компании даёт «информацию об ЛПР»
        company_has_lpr = set()
        company_ids = sorted(bound["COMPANY"], key=int)
        for i in range(0, len(company_ids), 50):
            for c in self.list_all("crm.contact.list", {"filter": {"COMPANY_ID": company_ids[i:i + 50]},
                                                        "select": contact_select}):
                if contact_flags(c)[0]:
                    company_has_lpr.add(str(c.get("COMPANY_ID")))

        def entity_info(etype, eid):
            """(есть ЛПР, секретарь, название клиента) для сущности CRM, к которой привязан звонок или встреча."""
            eid = str(eid)
            if etype == "CONTACT":
                c = contacts.get(eid)
                if not c:
                    return 0, 0, ""
                name = companies_of_calls.get(str(c.get("COMPANY_ID")), {}).get("TITLE") or \
                    " ".join(x for x in (c.get("NAME"), c.get("LAST_NAME")) if x)
                return (*contact_flags(c), name)
            if etype == "COMPANY":
                return int(eid in company_has_lpr), 0, companies_of_calls.get(eid, {}).get("TITLE", "")
            if etype == "LEAD":
                ld = leads_of_calls.get(eid, {})
                return (self.flag(ld.get(lead_lpr_field)) if lead_lpr_field else 0), 0, ld.get("TITLE", "")
            return 0, 0, ""

        rows = []
        for c in calls:
            login = emp(c.get("PORTAL_USER_ID"))
            if not login:
                continue
            start = str(c.get("CALL_START_DATE"))[:19]
            is_lpr, is_secr, client = entity_info(c.get("CRM_ENTITY_TYPE"), c.get("CRM_ENTITY_ID"))
            owner = (OWNER_TYPES.get(c.get("CRM_ENTITY_TYPE"), 0), str(c.get("CRM_ENTITY_ID")))
            t0 = dt.datetime.fromisoformat(start)
            nxt = 0
            for a in by_owner.get(owner, []):
                if str(a["ID"]) == str(c.get("CRM_ACTIVITY_ID")):
                    continue
                created = dt.datetime.fromisoformat(str(a["CREATED"])[:19])
                if t0 <= created <= t0 + dt.timedelta(days=1):
                    nxt = 1
                    break
            rows.append({"id": "call" + str(c["ID"]), "emp": login, "dt": start, "kind": "call", "direction": "out",
                         "duration": int(c.get("CALL_DURATION") or 0), "is_lpr": is_lpr, "secretary": is_secr,
                         "next_step": nxt, "status": "done", "subject": "", "client": client})
        for a in acts:
            if str(a.get("TYPE_ID")) != "1":  # 1 — встреча
                continue
            login = emp(a.get("RESPONSIBLE_ID"))
            if not login:
                continue
            etype = type_names.get(int(a.get("OWNER_TYPE_ID") or 0))
            is_lpr, _, client = entity_info(etype, a["OWNER_ID"])
            if etype not in ("CONTACT", "LEAD"):
                is_lpr = 1  # встреча по сделке или компании
            files = a.get("STORAGE_ELEMENT_IDS") or a.get("FILES")
            done = a.get("COMPLETED") == "Y"
            rows.append({"id": "act" + str(a["ID"]), "emp": login, "dt": self.iso(a.get("START_TIME")), "kind": "meeting",
                         "direction": "out", "duration": 0, "is_lpr": is_lpr, "secretary": 0, "next_step": 1,
                         "status": "done" if done else "planned", "has_protocol": int(bool(files)),
                         "has_summary": int(len((a.get("DESCRIPTION") or "").strip()) >= 20),
                         # точной даты закрытия дела в REST нет — берём дату последнего изменения
                         "closed_dt": self.iso(a.get("LAST_UPDATED")) if done else None,
                         "subject": a.get("SUBJECT"), "client": client})
        out["activities"] = rows

        # исходящие письма с КП и вложением в карточках сделок — подтверждение отправки КП (ПМ, KPI 4)
        kw = [k.lower() for k in self.cfg.get("kp_email_keywords", [])]
        letters = []
        for a in acts:
            if str(a.get("TYPE_ID")) != "4" or str(a.get("DIRECTION")) != "2" or str(a.get("OWNER_TYPE_ID")) != "2":
                continue  # 4 — e-mail, 2 — исходящее, 2 — сделка
            text = f"{a.get('SUBJECT') or ''} {a.get('DESCRIPTION') or ''}".lower()
            if kw and not any(k in text for k in kw):
                continue
            login = emp(a.get("RESPONSIBLE_ID"))
            if not login or not (a.get("STORAGE_ELEMENT_IDS") or a.get("FILES")):
                continue
            letters.append({"id": "mail" + str(a["ID"]), "emp": login, "dt": self.iso(a.get("START_TIME") or a.get("CREATED")),
                            "deal": str(a["OWNER_ID"]), "subject": a.get("SUBJECT")})
        out["kp_letters"] = letters

        # тендеры: сделки воронки тендеров + история стадий
        self.log("Б24: торги")
        cat = self.cfg.get("tender_category_id")
        uf = [self.f.get("deal_planned_cost"), self.f.get("deal_actual_cost")]
        deals = self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": cat, "ASSIGNED_BY_ID": uids, ">=DATE_MODIFY": df_long},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "CLOSEDATE", "STAGE_ID",
                       "STAGE_SEMANTIC_ID", "OPPORTUNITY"] + [u for u in uf if u]})
        hist = self.list_all("crm.stagehistory.list", {
            "entityTypeId": 2, "filter": {"CATEGORY_ID": cat, ">=CREATED_TIME": df_long},
            "select": ["OWNER_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CREATED_TIME"], "order": {"ID": "ASC"}}, key="items")
        h_by = {}
        for h in hist:
            h_by.setdefault(str(h["OWNER_ID"]), []).append(h)
        st = self.cfg.get("tender_stages", {})
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        rows = []
        for d in deals:
            login = emp(d.get("ASSIGNED_BY_ID"))
            if not login:
                continue
            hs = h_by.get(str(d["ID"]), [])
            visited = {h["STAGE_ID"] for h in hs} | {d.get("STAGE_ID")}

            def first_time(stages):
                ts = [h["CREATED_TIME"] for h in hs if h["STAGE_ID"] in stages]
                return str(min(ts))[:10] if ts else None

            sem = d.get("STAGE_SEMANTIC_ID")
            result = "won" if sem == "S" else "lost" if sem == "F" else "open"
            final_ts = [h["CREATED_TIME"] for h in hs if h.get("STAGE_SEMANTIC_ID") in ("S", "F")]
            result_date = (str(max(final_ts))[:10] if final_ts else str(d.get("CLOSEDATE") or "")[:10]) if result != "open" else None
            rows.append({
                "id": "deal" + str(d["ID"]), "emp": login, "title": d.get("TITLE"),
                "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                "created": str(d.get("DATE_CREATE"))[:10],
                "in_work": int(bool(visited & set(st.get("in_work", [])))),
                "price_review": int(bool(visited & set(st.get("price_review", [])))),
                "submitted_date": first_time(set(st.get("submitted", []))),
                "result": result, "result_date": result_date, "price": float(d.get("OPPORTUNITY") or 0),
                # проигрыш по ошибке тендерного отдела (KPI «Операционная дисциплина и качество подачи»)
                "rejected": int(result == "lost" and d.get("STAGE_ID") in set(st.get("rejected", []))),
                "planned_cost": self.num(d.get(uf[0])) if uf[0] else None,
                "actual_cost": self.num(d.get(uf[1])) if uf[1] else None})
        out["tenders"] = rows + self._legacy_tenders(emp, df_long, deals)

        # сделки воронки продаж (конверсия проектного менеджера)
        self.log("Б24: сделки продаж")
        deals = self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": self.cfg.get("sales_category_id", 0), "ASSIGNED_BY_ID": uids, "CLOSED": "Y", ">=CLOSEDATE": df},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "CLOSEDATE", "STAGE_SEMANTIC_ID", "OPPORTUNITY"]})
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        out["deals"] = [{"id": "deal" + str(d["ID"]), "emp": emp(d["ASSIGNED_BY_ID"]), "title": d.get("TITLE"),
                         "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                         "created": str(d.get("DATE_CREATE"))[:10], "closed_date": str(d.get("CLOSEDATE"))[:10],
                         "result": "won" if d.get("STAGE_SEMANTIC_ID") == "S" else "lost", "amount": float(d.get("OPPORTUNITY") or 0)}
                        for d in deals if emp(d.get("ASSIGNED_BY_ID"))]

        # лиды
        self.log("Б24: лиды")
        lf = {k: self.f.get("lead_" + k) for k in ("need", "budget", "lpr", "solvency", "next_step", "first_contact")}
        leads = self.list_all("crm.lead.list", {
            "filter": {"ASSIGNED_BY_ID": uids, ">=DATE_MODIFY": df},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "DATE_CREATE"] + [v for v in lf.values() if v]})
        lhist = self.list_all("crm.stagehistory.list", {
            "entityTypeId": 1, "filter": {">=CREATED_TIME": df}, "select": ["OWNER_ID", "STATUS_ID", "CREATED_TIME"],
            "order": {"ID": "ASC"}}, key="items")
        qstat = set(self.cfg.get("lead_qualified_statuses", []))
        qdate = {}
        for h in lhist:
            if h.get("STATUS_ID") in qstat:
                qdate.setdefault(str(h["OWNER_ID"]), str(h["CREATED_TIME"])[:19])
        rows = []
        for l in leads:
            login = emp(l.get("ASSIGNED_BY_ID"))
            if not login or l.get("STATUS_ID") not in qstat:
                continue
            first_contact = str(l.get(lf["first_contact"]) or l.get("DATE_CREATE"))[:10] if lf["first_contact"] else str(l.get("DATE_CREATE"))[:10]
            q = qdate.get(str(l["ID"]))
            if not q:
                continue
            # лид относится к месяцу, когда и первый контакт, и квалификация уже состоялись
            q = max(q, first_contact)
            rows.append({"id": "lead" + str(l["ID"]), "emp": login, "title": l.get("TITLE"), "first_contact": first_contact,
                         "qualified_date": q, "status": l.get("STATUS_ID"),
                         **{k: self.flag(l.get(v)) if v else 0 for k, v in lf.items() if k != "first_contact"}})
        out["leads"] = rows

        # компании: активные клиенты с выявленной потребностью и стратегией
        self.log("Б24: компании")
        cf = [self.f.get("company_need"), self.f.get("company_strategy")]
        if all(cf):
            comps = self.list_all("crm.company.list", {"filter": {"ASSIGNED_BY_ID": uids},
                                                       "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "DATE_MODIFY"] + cf})
            out["clients"] = [{"id": "co" + str(c["ID"]), "emp": emp(c["ASSIGNED_BY_ID"]), "name": c.get("TITLE"),
                               "need": self.flag(c.get(cf[0])), "strategy": self.flag(c.get(cf[1])),
                               "updated": str(c.get("DATE_MODIFY"))[:19]} for c in comps if emp(c.get("ASSIGNED_BY_ID"))]

        # задачи
        self.log("Б24: задачи")
        tasks = self.list_all("tasks.task.list", {
            "filter": {"RESPONSIBLE_ID": uids, ">=DEADLINE": df},
            "select": ["ID", "TITLE", "RESPONSIBLE_ID", "CREATED_DATE", "DEADLINE", "CLOSED_DATE", "STATUS"]}, key="tasks")
        out["tasks"] = [{"id": "task" + str(t["id"]), "emp": emp(t.get("responsibleId")), "title": t.get("title"),
                         "created": self.iso(t.get("createdDate")), "deadline": self.iso(t.get("deadline")),
                         "closed": self.iso(t.get("closedDate")), "status": str(t.get("status"))}
                        for t in tasks if emp(t.get("responsibleId")) and t.get("deadline")]
        return out

    # ---------- воронки для вкладки «Воронки»: текущий срез сделок и лидов CRM
    @staticmethod
    def _semantics(st):
        """Смысл стадии: S — успех, F — провал, P — в работе."""
        sem = str((st.get("EXTRA") or {}).get("SEMANTICS") or st.get("SEMANTICS") or "").lower()
        return "S" if sem in ("s", "success") else "F" if sem in ("f", "failure", "apology") else "P"

    def _legacy_tenders(self, emp, df_long, current):
        """Торги, которые до появления воронки тендеров велись в другой воронке (bitrix24.legacy_tenders).

        Прежние стадии той воронки удалены, поэтому дошли ли торги до рассмотрения заявки, видно только по итогу:
        победа — сделка перешла на стадии исполнения договора (won_stages), рассмотрены и проиграны — reviewed_lost_stages.
        Копии выигранных тендеров, которые создаются в этой воронке после победы, пропускаются (совпадение названия)."""
        lc = self.cfg.get("legacy_tenders")
        if not lc:
            return []
        self.log("Б24: торги до воронки тендеров")
        won_st, lost_st = set(lc.get("won_stages", [])), set(lc.get("reviewed_lost_stages", []))
        titles = {d.get("TITLE") for d in self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": self.cfg.get("tender_category_id")}, "select": ["TITLE"]})}
        deals = [d for d in self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": lc["category_id"], ">=DATE_MODIFY": df_long},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "CLOSEDATE", "STAGE_ID",
                       "STAGE_SEMANTIC_ID", "OPPORTUNITY"]}) if d.get("TITLE") not in titles and emp(d.get("ASSIGNED_BY_ID"))]
        hist = {}
        ids = [d["ID"] for d in deals]
        for i in range(0, len(ids), 50):
            for h in self.list_all("crm.stagehistory.list", {
                    "entityTypeId": 2, "filter": {"OWNER_ID": ids[i:i + 50]},
                    "select": ["OWNER_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CREATED_TIME"], "order": {"ID": "ASC"}}, key="items"):
                hist.setdefault(str(h["OWNER_ID"]), []).append(h)
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        rows = []
        for d in deals:
            hs = hist.get(str(d["ID"]), [])
            won_ts = [str(h["CREATED_TIME"]) for h in hs if h["STAGE_ID"] in won_st]
            if d.get("STAGE_ID") in won_st and not won_ts:
                won_ts = [str(d.get("CLOSEDATE") or d.get("DATE_CREATE"))]
            if won_ts:
                result, result_date = "won", min(won_ts)[:10]
            elif d.get("STAGE_SEMANTIC_ID") == "F":
                final = [str(h["CREATED_TIME"]) for h in hs if h.get("STAGE_SEMANTIC_ID") == "F"]
                result, result_date = "lost", (max(final) if final else str(d.get("CLOSEDATE") or ""))[:10]
            else:
                result, result_date = "open", None
            rows.append({
                "id": "deal" + str(d["ID"]), "emp": emp(d.get("ASSIGNED_BY_ID")), "title": d.get("TITLE"),
                "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                "created": str(d.get("DATE_CREATE"))[:10], "in_work": 1,
                "price_review": int(result == "won" or d.get("STAGE_ID") in lost_st),
                "submitted_date": None, "result": result, "result_date": result_date,
                "rejected": int(result == "lost" and d.get("STAGE_ID") in set(lc.get("rejected_stages", []))),
                "price": float(d.get("OPPORTUNITY") or 0), "planned_cost": None, "actual_cost": None})
        return rows

    def order_owners(self, field):
        """Номер заказа клиента 1С, записанный в сделке (поле field) → [(ID ответственного, дата создания сделки)].
        В поле можно указать несколько номеров через запятую или пробел. Номера в 1С каждый год начинаются заново,
        поэтому вместе с ответственным отдаётся дата сделки — по ней выбирается заказ нужного года."""
        out = {}
        for d in self.list_all("crm.deal.list", {"filter": {"!" + field: ""},
                                                  "select": ["ID", "ASSIGNED_BY_ID", "DATE_CREATE", field]}):
            for num in re.split(r"[,;\s]+", str(d.get(field) or "")):
                key = order_key(num)
                if key:
                    out.setdefault(key, []).append((int(d["ASSIGNED_BY_ID"]), str(d.get("DATE_CREATE"))[:10]))
        return out

    def _stages(self, entity_id):
        # фильтр по ENTITY_ID принимает только строку, поэтому справочники запрашиваются по одному
        return {st["STATUS_ID"]: {"name": st.get("NAME"), "sort": int(st.get("SORT") or 0), "semantics": self._semantics(st)}
                for st in self.list_all("crm.status.list", {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}})}

    def deal_stages(self):
        """Воронки и стадии сделок: {category_id: {"name": ..., "stages": {stage_id: {"name", "sort", "semantics"}}}}."""
        cats = self.call("crm.category.list", {"entityTypeId": 2})["result"].get("categories", [])
        res = {str(c["id"]): {"name": c.get("name") or "Общая", "stages": {}} for c in cats}
        res.setdefault("0", {"name": "Общая", "stages": {}})
        for cid in res:
            res[cid]["stages"] = self._stages("DEAL_STAGE" if cid == "0" else f"DEAL_STAGE_{cid}")
        return res

    def lead_statuses(self):
        """Статусы лидов: {status_id: {"name", "sort", "semantics"}}."""
        return self._stages("STATUS")

    def _user_names(self, ids):
        uids = sorted({int(i) for i in ids if i})
        users = {}
        for i in range(0, len(uids), 50):
            for u in self.list_all("user.get", {"FILTER": {"ID": uids[i:i + 50]}}):
                users[str(u["ID"])] = " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x) or f"Пользователь {u['ID']}"
        return users

    def fetch_crm_deals(self, employees, fcfg):
        """Текущий срез открытых сделок для воронки сделок."""
        self.log("Б24: сделки для воронки")
        stages = self.deal_stages()
        flt = {"CLOSED": "N"}
        if fcfg.get("include_closed"):
            flt = {}
        if fcfg.get("category_ids"):
            flt["CATEGORY_ID"] = fcfg["category_ids"]
        deals = self.list_all("crm.deal.list", {
            "filter": flt, "order": {"ID": "ASC"},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "CATEGORY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY",
                       "DATE_CREATE", "MOVED_TIME", "COMPANY_ID"]})
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        users = self._user_names(d.get("ASSIGNED_BY_ID") for d in deals)
        snap = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        for d in deals:
            cid = str(d.get("CATEGORY_ID") or "0")
            cat = stages.get(cid, {"name": cid, "stages": {}})
            st = cat["stages"].get(d.get("STAGE_ID"), {})
            rid = str(d.get("ASSIGNED_BY_ID") or "")
            rows.append({
                "id": "deal" + str(d["ID"]), "entity": "deal", "emp": employees.get(int(rid)) if rid else None,
                "responsible_id": rid, "responsible": users.get(rid, ""), "category_id": cid, "category": cat["name"],
                "stage_id": d.get("STAGE_ID"), "stage": st.get("name") or d.get("STAGE_ID"), "stage_sort": st.get("sort", 0),
                "semantic": d.get("STAGE_SEMANTIC_ID"), "title": d.get("TITLE"),
                "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                "amount": self.num(d.get("OPPORTUNITY")) or 0, "created": self.iso(d.get("DATE_CREATE")),
                "stage_since": self.iso(d.get("MOVED_TIME")) or self.iso(d.get("DATE_CREATE")), "snapshot": snap})
        return rows

    def fetch_crm_leads(self, employees, fcfg):
        """Текущий срез открытых лидов для воронки лидов (хранится в той же таблице crm_deals, entity = lead)."""
        self.log("Б24: лиды для воронки")
        statuses = self.lead_statuses()
        flt = {} if fcfg.get("include_closed") else {"STATUS_SEMANTIC_ID": "P"}
        leads = self.list_all("crm.lead.list", {
            "filter": flt, "order": {"ID": "ASC"},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "STATUS_SEMANTIC_ID", "OPPORTUNITY", "DATE_CREATE",
                       "MOVED_TIME", "COMPANY_TITLE"]})
        users = self._user_names(d.get("ASSIGNED_BY_ID") for d in leads)
        snap = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        for d in leads:
            st = statuses.get(d.get("STATUS_ID"), {})
            rid = str(d.get("ASSIGNED_BY_ID") or "")
            rows.append({
                "id": "lead" + str(d["ID"]), "entity": "lead", "emp": employees.get(int(rid)) if rid else None,
                "responsible_id": rid, "responsible": users.get(rid, ""), "category_id": "lead", "category": "Лиды",
                "stage_id": d.get("STATUS_ID"), "stage": st.get("name") or d.get("STATUS_ID"), "stage_sort": st.get("sort", 0),
                "semantic": d.get("STATUS_SEMANTIC_ID"), "title": d.get("TITLE"), "client": d.get("COMPANY_TITLE") or "",
                "amount": self.num(d.get("OPPORTUNITY")) or 0, "created": self.iso(d.get("DATE_CREATE")),
                "stage_since": self.iso(d.get("MOVED_TIME")) or self.iso(d.get("DATE_CREATE")), "snapshot": snap})
        return rows
