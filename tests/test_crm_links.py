import copy
import unittest
from unittest import mock

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.funnel import deal_funnel
from app import call_records
from tests.test_engine import CONFIG, TODAY


class CRMLinksTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:"); self.addCleanup(self.con.close)
        self.config = copy.deepcopy(CONFIG)
        self.config["bitrix24"] = {"webhook": "https://user:secret@crm.example.test/rest/1/private-token/"}
        self.config["employees"][2]["b24_user_id"] = 56
        self.engine = Engine(self.con, load_rules(), self.config)
        patch = mock.patch("app.engine.today", return_value=TODAY)
        patch.start(); self.addCleanup(patch.stop)

    def test_urls_are_typed_and_never_expose_webhook_credentials(self):
        from app.crm_links import object_url, reference_url, record_url
        for kind in ("lead", "deal", "company", "contact"):
            self.assertEqual(object_url(self.config, kind, 42), f"https://crm.example.test/crm/{kind}/details/42/")
        self.assertEqual(reference_url(self.config, "CONTACT:7"), "https://crm.example.test/crm/contact/details/7/")
        self.assertEqual(object_url(self.config, "task", 7, 56), "https://crm.example.test/company/personal/user/56/tasks/task/view/7/")
        self.assertEqual(record_url(self.config, "leads", {"id": "lead42"}), "https://crm.example.test/crm/lead/details/42/")
        for kind, value in (("company", "bad"), ("lead", "0"), ("deal", "-1"), ("javascript", "1"), ("contact", "42/evil")):
            self.assertIsNone(object_url(self.config, kind, value))
        self.assertIsNone(object_url({}, "lead", 42))
        self.assertIsNone(object_url({"webhook": "javascript:alert(1)"}, "lead", 42))
        self.assertIsNone(record_url(self.config, "sales", {"id": "deal42"}))
        self.assertIsNone(record_url(self.config, "leads", {"id": "1c-uuid"}))

    def test_funnel_keeps_object_company_and_responsible_links(self):
        replace_rows(self.con, "crm_deals", [{"id": "deal42", "entity": "deal", "title": "Одинаковое имя",
            "client": "Одинаковое имя", "client_ref": "COMPANY:7", "responsible_id": "56", "responsible": "Тест",
            "emp": "tender", "category_id": "0", "category": "Продажи", "stage_id": "PREPARATION", "stage": "Подготовка",
            "semantic": "P", "amount": 100}])
        row = deal_funnel(self.con, self.config)["sections"][0]["deals"][0]
        self.assertEqual(row["url"], "https://crm.example.test/crm/deal/details/42/")
        self.assertEqual(row["client_url"], "https://crm.example.test/crm/company/details/7/")
        self.assertEqual(row["responsible_url"], "https://crm.example.test/company/personal/user/56/")

    def test_qualified_lead_titles_link_by_id_despite_duplicate_names(self):
        replace_rows(self.con, "leads", [{"id": "lead"+str(i), "emp": "pm", "title": "Дубликат <А>",
            "qualified_date": "2026-10-01", "need": 1, "budget": 1, "lpr": 1, "solvency": 1, "next_step": 1} for i in (7,42)])
        item = next(i for i in self.engine.compute("pm", "2026-10")["items"] if i["code"] == "qualified_leads")
        self.assertEqual([r[0] for r in item["details"]["links"]],
                         ["https://crm.example.test/crm/lead/details/7/", "https://crm.example.test/crm/lead/details/42/"])

    def test_audit_and_recording_clients_have_separate_exact_urls(self):
        replace_rows(self.con, "activities", [{"id": "call1", "emp": "tender", "dt": "2026-10-01T10:00:00", "kind": "call",
            "direction": "out", "duration": 180, "contact": "CONTACT:7", "client_ref": "COMPANY:42", "client": "Компания",
            "is_lpr": 1, "next_step": 1, "secretary": 0}])
        call_records.upsert(self.con, [{"id": "call1", "emp": "tender", "dt": "2026-10-01T10:00:00", "direction": "out",
            "crm_entity": "CONTACT:7", "client_ref": "COMPANY:42", "client": "Компания", "crm_activity_id": "99"}])
        data = self.engine.activity_audit("tender", "2026-10", "base_development", "calls")
        row = data["records"][0]
        self.assertEqual(row["client_url"], "https://crm.example.test/crm/company/details/42/")
        self.assertIn("/crm/contact/details/7/", row["url"])
        self.assertIn("99", row["url"])
        recording = call_records.listing(self.con, self.config)[0]
        self.assertEqual(recording["client_url"], row["client_url"])
        self.assertEqual(recording["url"], row["url"])

    def test_meeting_and_task_urls_do_not_use_call_statistic_id(self):
        from app.crm_links import record_url
        self.assertEqual(record_url(self.config,"activities",{"id":"call999","contact":"LEAD:7"}),
                         "https://crm.example.test/crm/lead/details/7/")
        meeting=record_url(self.config,"activities",{"id":"act99","contact":"COMPANY:42"})
        self.assertIn("/crm/company/details/42/",meeting)
        self.assertIn("99",meeting)
        self.assertEqual(record_url(self.config,"tasks",{"id":"task7","emp":"tender"}),
                         "https://crm.example.test/company/personal/user/56/tasks/task/view/7/")

    def test_fact_funnel_keeps_closed_lead_identity_and_company_reference(self):
        row={"id":"lead7","entity":"lead","title":"Закрытый лид","client":"Компания",
             "client_ref":"COMPANY:42","responsible_id":"56","responsible":"Тест","emp":"tender",
             "category_id":"lead","category":"Лиды","stage_id":"CONVERTED","stage":"Завершён","semantic":"S"}
        replace_rows(self.con,"fact_records",[row])
        replace_rows(self.con,"stage_moves",[{"id":"lead7:99","rid":"lead7","entity":"lead","stage_id":"NEW",
            "stage":"Распределено","semantic":"P","category_id":"lead","category":"Лиды","ts":"2026-10-05"}])
        data=deal_funnel(self.con,self.config,kind="leads",date_from="2026-10-01",date_to="2026-10-31",mode="fact")
        result=next(s for s in data["sections"] if s["deals"])["deals"][0]
        self.assertEqual(result["url"],"https://crm.example.test/crm/lead/details/7/")
        self.assertEqual(result["client_url"],"https://crm.example.test/crm/company/details/42/")

    def test_normalization_preserves_company_id_across_storage(self):
        from app.bitrix24 import Bitrix24
        b24=Bitrix24(self.config["bitrix24"],log=lambda *a:None)
        b24.deal_stages=mock.Mock(return_value={"0":{"name":"Продажи","stages":{}}})
        b24.list_all=mock.Mock(return_value=[{"ID":"42","ASSIGNED_BY_ID":"56","COMPANY_ID":"7","TITLE":"Сделка","CATEGORY_ID":"0"}])
        b24.by_ids=mock.Mock(return_value={"7":{"TITLE":"Компания"}})
        b24._user_names=mock.Mock(return_value={"56":"Тест"})
        rows=b24.fetch_crm_deals({56:"tender"},{})
        self.assertEqual(rows[0]["client_ref"],"COMPANY:7")
        replace_rows(self.con,"crm_deals",rows)
        self.assertEqual(self.con.execute("SELECT client_ref FROM crm_deals").fetchone()[0],"COMPANY:7")

    def test_calendar_event_links_and_private_event_remains_anonymous(self):
        import datetime as dt
        from app.b24_calendar import _events
        b24=mock.Mock()
        b24.call.return_value={"result":[{"ID":"42","NAME":"Встреча","DATE_FROM":"2026-11-03T12:00:00","DATE_TO":"2026-11-03T13:00:00"},
            {"ID":"43","NAME":"Личное","PRIVATE_EVENT":"Y","OWNER_ID":"57","DATE_FROM":"2026-11-03T14:00:00"}]}
        events=_events(b24,56,dt.date(2026,11,3),"https://crm.example.test")
        self.assertEqual(events[0]["url"],"https://crm.example.test/company/personal/user/56/calendar/?EVENT_ID=42")
        self.assertEqual((events[1]["title"],events[1]["url"]),("Занят(а)",None))

    def test_call_without_recording_keeps_its_explicit_activity_id(self):
        replace_rows(self.con,"activities",[{"id":"call99","emp":"tender","dt":"2026-10-01T10:00:00","kind":"call",
            "duration":180,"direction":"out","contact":"LEAD:7","crm_activity_id":"88","next_step":1,"is_lpr":1}])
        data=self.engine.activity_audit("tender","2026-10","base_development","calls")
        self.assertEqual(data["records"][0]["url"],"https://crm.example.test/crm/lead/details/7/?activity_id=88")
        call_records.upsert(self.con,[{"id":"call99","emp":"tender","dt":"2026-10-01T10:00:00","crm_activity_id":None}])
        data=self.engine.activity_audit("tender","2026-10","base_development","calls")
        self.assertEqual(data["records"][0]["url"],"https://crm.example.test/crm/lead/details/7/?activity_id=88")
