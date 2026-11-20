import unittest

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.manual import set_kpi_decision, set_kpi_exclusion
from tests.test_engine import CONFIG, call


class ActivityRecordReviewTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), CONFIG)
        self.month = "2026-10"
        self.code = "base_development"

    def audit(self, kind):
        return self.engine.activity_audit("tender", self.month, self.code, kind)

    def exclude(self, kind, ref, value=True):
        set_kpi_exclusion(self.con, "tender", self.month, self.code, kind, ref, value, "boss")

    def approve(self, kind, ref, value):
        set_kpi_decision(self.con, "tender", self.month, self.code, kind, ref, value, "boss")

    def test_exclusion_bypasses_selected_call_and_restore_keeps_saved_decision(self):
        first, second = [call(i, "tender", self.month + "-05", contact="CONTACT:7") for i in (1, 2)]
        replace_rows(self.con, "activities", [first, second])
        self.approve("calls", first["id"], 0)
        self.assertEqual(self.audit("calls")["fact"], 0)
        self.exclude("calls", first["id"])
        data = self.audit("calls")
        self.assertEqual(data["fact"], 1)
        rows = {r["id"]: r for r in data["records"]}
        self.assertTrue(rows[second["id"]]["counted"])
        self.assertTrue(rows[first["id"]]["review"]["excluded"])
        self.exclude("calls", first["id"], False)
        self.assertEqual(self.audit("calls")["fact"], 0)

    def test_company_extra_members_use_current_crm_and_active_members_use_month_snapshot(self):
        replace_rows(self.con, "clients", [
            {"id": "co1", "emp": "lead", "name": "А", "need": 1, "strategy": 0},
            {"id": "co2", "emp": "lead", "name": "Б", "need": 0, "strategy": 1},
            {"id": "co3", "emp": "pm", "name": "В", "need": 1, "strategy": 1}])
        self.con.execute("INSERT INTO clients_monthly VALUES(?,?,?,?,?,?)", (self.month, "co4", "lead", "Г", 1, 1))
        ctx = {**self.engine._ctx(self.month), "in_progress": False}
        result = self.engine.calc_base_development({"login": "lead"}, ctx, {"active_clients": 7})
        for extra, names in zip(result["extra"], (["Г"], ["А", "Б"], ["А"], ["Б"])):
            self.assertEqual(extra["value"], len(names))
            self.assertEqual([r["label"] for r in extra["members"]], names)

    def test_excluded_kp_releases_paired_letter(self):
        replace_rows(self.con, "kp", [{"id": "1", "dt": "2026-10-01", "client": "Альфа", "emp": "tender"}])
        replace_rows(self.con, "kp_letters", [{"id": "2", "dt": "2026-10-01", "deal": "Альфа", "emp": "tender"}])
        self.exclude("kp", "kp:1")
        data = self.audit("kp")
        self.assertEqual(data["fact"], 1)
        self.assertTrue(next(r for r in data["records"] if r["id"] == "letter:2")["counted"])

    def test_meeting_force_yes_and_exclusion(self):
        replace_rows(self.con, "activities", [{"id": "m", "dt": "2026-10-01", "kind": "meeting", "emp": "tender"}])
        self.approve("meetings", "m", 1)
        self.assertEqual(self.audit("meetings")["fact"], 1)
        self.exclude("meetings", "m")
        self.assertEqual(self.audit("meetings")["fact"], 0)

    def test_tender_no_adds_failure_exclusion_removes_it(self):
        self.code = "tender_operations"
        replace_rows(self.con, "tenders_1c", [{"id": "t", "dt": "2026-10-01", "status": "Новый", "emp": "tender"}])
        def fact():
            return self.engine.calc_tender_operations({"login": "tender"}, self.engine._ctx(self.month), {})["components"][0]["fact"]
        self.assertEqual(fact(), 0)
        self.approve("tenders", "t", 0)
        self.assertEqual(fact(), 1)
        self.exclude("tenders", "t")
        self.assertEqual(fact(), 0)
