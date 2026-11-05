import copy
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

from app.common import connect, load_rules, replace_rows, db_lock
from app.engine import Engine
from app.server import App, Handler, set_password
from tests.test_engine import CONFIG, call


class KPIActivityAuditTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules, self.config = load_rules(), copy.deepcopy(CONFIG)
        self.engine = Engine(self.con, self.rules, self.config)

    def put(self, table, rows):
        replace_rows(self.con, table, rows)

    def audit(self, kind, login="tender", month="2026-10"):
        code = "operational_activity" if login == "pm" else "base_development"
        return self.engine.activity_audit(login, month, code, kind)

    def row(self, data, id):
        return next(r for r in data["records"] if r["id"] == id)

    def states(self, row):
        return {c["key"]: c["state"] for c in row["checks"]}

    def test_all_calls_and_actual_daily_dedup_are_explained(self):
        rows = [call(0, "tender", "2026-10-05", lpr=0, contact="CONTACT:7"),
                call(1, "tender", "2026-10-05", contact="CONTACT:7"),
                call(2, "tender", "2026-10-05", contact="CONTACT:7"),
                {**call(3, "tender", "2026-10-05"), "direction": "in"},
                call(4, "tender", "2026-10-05", duration=119),
                call(5, "tender", "2026-10-05", secretary=1),
                call(6, "tender", "2026-10-05", next_step=0),
                call(7, "pm", "2026-10-05")]
        self.put("activities", rows)
        r = self.audit("calls")
        self.assertEqual(len(r["records"]), 7)
        self.assertEqual(r["fact"], 2)
        self.assertEqual(sum(x["counted"] is True for x in r["records"]), r["fact"])
        self.assertFalse(self.row(r, "ctender0")["counted"])
        self.assertTrue(self.row(r, "ctender1")["counted"])
        self.assertTrue(self.row(r, "ctender2")["duplicate"])
        self.assertEqual(self.states(self.row(r, "ctender3"))["outgoing"], "pass")
        self.assertEqual(self.states(self.row(r, "ctender4"))["duration"], "fail")
        self.assertEqual(self.states(self.row(r, "ctender5"))["not_secretary"], "fail")
        self.assertEqual(self.states(self.row(r, "ctender6"))["next_step"], "fail")
        self.assertTrue(self.row(r, "ctender6")["actions"])

    def test_pm_lpr_is_not_required_and_manual_secretary_override_is_used(self):
        self.put("activities", [call(1, "pm", "2026-10-05", lpr=0, secretary=1)])
        self.con.execute("INSERT INTO call_marks VALUES ('cpm1',0,'boss','2026-10-06')")
        r = self.audit("calls", "pm")
        row = self.row(r, "cpm1")
        self.assertTrue(row["counted"])
        self.assertEqual(self.states(row)["lpr"], "na")
        self.assertEqual(self.states(row)["not_secretary"], "pass")
        self.assertIn("руководитель", " ".join(c["detail"] for c in row["checks"]))

    def test_next_step_guidance_accepts_any_subsequent_non_call_activity(self):
        self.put("activities", [call(1, "tender", "2026-10-05", next_step=0)])
        with mock.patch("app.kpi_activity_audit._now", return_value=__import__('datetime').datetime(2026, 11, 3)):
            r = self.audit("calls")
        text = " ".join(self.row(r, "ctender1")["actions"]).lower()
        self.assertIn("связанной компании", text)
        self.assertIn("кроме повторного звонка", text)
        self.assertNotIn("в течение 24 часов", " ".join(r["rules"]))

    def test_lpr_guidance_follows_company_only_source_setting(self):
        self.config["bitrix24"] = {"fields": {"company_lpr": "UF_LPR"}}
        self.put("activities", [call(1, "tender", "2026-10-05", lpr=0)])
        r = self.audit("calls")
        self.assertIn("карточке компании", " ".join(r["records"][0]["actions"]).lower())

    def test_historical_secretary_guidance_does_not_offer_an_inactive_override(self):
        self.put("activities", [call(1, "tender", "2026-09-05", secretary=1)])
        self.con.execute("INSERT INTO call_marks VALUES ('ctender1',0,'boss','2026-09-06')")
        r = self.audit("calls", "tender", "2026-09")
        self.assertFalse(r["records"][0]["counted"])
        self.assertNotIn("попросите руководителя", " ".join(r["records"][0]["actions"]).lower())
        self.assertIn("CRM", " ".join(r["rules"]))

    def test_deal_binding_without_lpr_has_specific_source_guidance(self):
        self.put("activities", [call(1, "tender", "2026-10-05", lpr=0, contact="DEAL:7")])
        r = self.audit("calls")
        self.assertIn("сделк", " ".join(r["records"][0]["actions"]).lower())

    def test_pm_meeting_completion_protocol_summary_and_close_uncertainty(self):
        p = next(i["params"] for i in self.rules["positions"]["pm"]["items"] if i["code"] == "operational_activity")
        p["meeting_same_day_close"] = True
        base = {"emp": "pm", "dt": "2026-10-05T10:00:00", "kind": "meeting", "status": "done",
                "has_protocol": 1, "has_summary": 1, "closed_dt": "2026-10-05T11:00:00"}
        self.put("activities", [{**base, "id": "m1"}, {**base, "id": "m2", "status": "planned"},
             {**base, "id": "m3", "has_protocol": 0}, {**base, "id": "m4", "has_summary": 0},
             {**base, "id": "m5", "closed_dt": "2026-10-06T11:00:00"}, {**base, "id": "m6", "closed_dt": None}])
        r = self.audit("meetings", "pm")
        self.assertEqual(len(r["records"]), 6)
        self.assertEqual(r["fact"], 2)
        self.assertTrue(self.row(r, "m6")["counted"])
        self.assertEqual(self.states(self.row(r, "m6"))["same_day_close"], "unknown")
        self.assertEqual(self.states(self.row(r, "m2"))["completed"], "fail")
        self.assertEqual(self.states(self.row(r, "m3"))["protocol"], "fail")
        self.assertEqual(self.states(self.row(r, "m4"))["summary"], "fail")
        self.assertEqual(self.states(self.row(r, "m5"))["same_day_close"], "fail")
        self.assertEqual(sum(x["counted"] is True for x in r["records"]), r["fact"])

    def test_tender_meetings_do_not_invent_protocol_requirements(self):
        self.put("activities", [{"id": "m", "emp": "tender", "kind": "meeting", "dt": "2026-10-05",
                                 "status": "done", "has_protocol": 0, "has_summary": 0, "is_lpr": 0}])
        r = self.audit("meetings")
        self.assertTrue(r["records"][0]["counted"])
        self.assertEqual(self.states(r["records"][0])["protocol"], "na")

    def test_current_kp_union_shows_all_sources_and_the_matched_duplicate(self):
        self.put("kp", [{"id": "k1", "emp": "tender", "dt": "2026-10-05", "client": "ООО Альфа", "doc": "КП-1"},
                        {"id": "k2", "emp": "tender", "dt": "2026-10-06", "client": "Бета", "out_of_tender": 0, "confirmed": 0}])
        self.put("kp_letters", [{"id": "l1", "emp": "tender", "dt": "2026-10-07", "deal": "АЛЬФА", "subject": "КП"},
                                {"id": "l2", "emp": "tender", "dt": "2026-10-08", "deal": "Гамма", "subject": "КП"}])
        r = self.audit("kp")
        self.assertEqual((len(r["records"]), r["fact"]), (4, 3))
        self.assertTrue(self.row(r, "kp:k2")["counted"])
        self.assertTrue(self.row(r, "letter:l1")["duplicate"])
        self.assertFalse(self.row(r, "letter:l1")["counted"])
        self.assertEqual(sum(x["counted"] is True for x in r["records"]), r["fact"])
        self.assertIn("7", " ".join(r["rules"]))

    def test_historical_minimum_does_not_fabricate_individual_letter_links(self):
        self.put("kp", [{"id": str(i), "emp": "pm", "dt": "2026-09-05"} for i in range(3)])
        self.put("kp_letters", [{"id": "l1", "emp": "pm", "dt": "2026-09-05", "deal": "Клиент"}])
        r = self.audit("kp", "pm", "2026-09")
        self.assertEqual(r["fact"], 1)
        self.assertTrue(all(x["counted"] is None for x in r["records"] if x["id"].startswith("kp:")))
        self.assertEqual(r["summary"]["unallocated"], 1)
        self.assertIn("нельзя определить", " ".join(r["rules"]).lower())

    def test_only_active_visible_component_can_be_requested(self):
        with self.assertRaises(ValueError):
            self.audit("calls", "lead")  # active lead KPI2 only counts newly active clients
        with self.assertRaises(ValueError):
            self.audit("unknown")

    def test_legacy_confirmed_and_outside_tender_rules_match_fact(self):
        p = next(i["params"] for i in self.rules["positions"]["pm"]["items"] if i["code"] == "operational_activity")
        p["kp_confirmation"] = "1c"
        self.put("kp", [{"id": "yes", "emp": "pm", "dt": "2026-09-05", "confirmed": 1},
                        {"id": "no", "emp": "pm", "dt": "2026-09-05", "confirmed": 0}])
        r = self.audit("kp", "pm", "2026-09")
        self.assertEqual((r["fact"], sum(x["counted"] is True for x in r["records"])), (1, 1))
        self.put("kp", [{"id": "yes", "emp": "tender", "dt": "2026-09-05", "out_of_tender": 1},
                        {"id": "no", "emp": "tender", "dt": "2026-09-05", "out_of_tender": 0}])
        r = self.audit("kp", "tender", "2026-09")
        self.assertEqual((r["fact"], sum(x["counted"] is True for x in r["records"])), (1, 1))

    def test_legacy_journal_only_does_not_claim_a_kp_has_no_matching_letter(self):
        self.config["letters_sheet"] = {"url": "https://example.test/journal"}
        self.put("kp", [{"id": "k", "emp": "tender", "dt": "2026-09-05", "client": "Альфа"}])
        self.put("kp_letters", [{"id": "l", "emp": "tender", "dt": "2026-09-05", "deal": "Альфа"}])
        r = self.audit("kp", "tender", "2026-09")
        row = self.row(r, "kp:k")
        self.assertFalse(row["counted"])
        self.assertNotIn("Письмо в реестре", [c["label"] for c in row["checks"]])
        self.assertTrue(self.row(r, "letter:l")["counted"])

    def test_audit_is_not_eagerly_embedded_in_kpi_response(self):
        item = next(i for i in self.engine.compute("tender", "2026-10")["items"] if i["code"] == "base_development")
        self.assertEqual({c.get("audit") for c in item["components"]}, {"calls", "meetings", "kp"})
        self.assertNotIn("records", item)


class KPIAuditHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = connect(":memory:")
        cfg = copy.deepcopy(CONFIG)
        cfg["letters_sheet"] = {"url": "https://example.test/journal", "topics": ["КП"]}
        cls.app = App.__new__(App)
        cls.app.con, cls.app.config, cls.app.rules = cls.con, cfg, load_rules()
        cls.app.engine = Engine(cls.con, cls.app.rules, cfg)
        cls.app.lock, cls.app.login_fails = db_lock(), {}
        replace_rows(cls.con, "activities", [{**call(1, "tender", "2026-10-05"), "client": "Тестовая компания"},
            {**call(2, "tender", "2026-10-06", duration=60, lpr=0, next_step=0), "client": "Клиент <проверка>"},
            {"id": "meeting1", "emp": "tender", "kind": "meeting", "dt": "2026-10-07T14:00:00", "status": "done", "client": "Тестовая компания"},
            {"id": "meeting2", "emp": "tender", "kind": "meeting", "dt": "2026-10-08T14:00:00", "status": "planned", "client": "Плановая встреча"}])
        replace_rows(cls.con, "kp", [{"id": "kp1", "emp": "tender", "dt": "2026-10-05", "doc": "КП-123", "client": "Тестовая компания"}])
        replace_rows(cls.con, "kp_letters", [{"id": "letter1", "emp": "tender", "dt": "2026-10-06", "deal": "Тестовая компания", "subject": "Отправка КП"},
            {"id": "letter2", "emp": "tender", "dt": "2026-10-09", "deal": "Другой клиент", "subject": "Расчёт КП"}])
        class AuditHandler(Handler):
            app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), AuditHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.cookies = {}
        for login in ("boss", "tender", "pm"):
            set_password(cls.con, login, "audit-fixture")
            request = urllib.request.Request(cls.base + "/api/login", method="POST",
                data=json.dumps({"login": login, "password": "audit-fixture"}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                cls.cookies[login] = response.headers["Set-Cookie"].split(";")[0]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.con.close()

    def get(self, login, query="login=tender&month=2026-10&code=base_development&kind=calls"):
        request = urllib.request.Request(self.base + "/api/kpi-records?" + query,
                                         headers={"Cookie": self.cookies[login]} if login else {})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_self_and_manager_can_read_but_other_employee_cannot(self):
        self.assertEqual(self.get("tender")[0], 200)
        self.assertEqual(self.get("boss")[0], 200)
        code, data = self.get("pm")
        self.assertEqual(code, 403)
        self.assertNotIn("records", data)

    def test_anonymous_and_invalid_parameters_are_rejected(self):
        self.assertEqual(self.get(None)[0], 401)
        self.assertEqual(self.get("boss", "login=tender&month=oops&code=base_development&kind=calls")[0], 400)
        self.assertEqual(self.get("boss", "login=tender&month=2026-10&code=base_development&kind=bad")[0], 400)


if __name__ == "__main__":
    unittest.main()
