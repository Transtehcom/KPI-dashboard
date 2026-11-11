import datetime as dt
import unittest
from unittest import mock

from app.bitrix24 import Bitrix24
from app.common import connect
from app import tender_followups
from tests.test_crm_activities import activity, call
from tests.test_engine import CONFIG, call as dashboard_call
from app.engine import Engine
from app.common import load_rules, replace_rows


class TenderFollowupSourceTest(unittest.TestCase):
    def test_completed_planned_activity_is_available_to_tender_kpi(self):
        client = Bitrix24({"webhook": "https://crm.example.test/rest/1/test/"}, log=lambda *args: None)
        plan = activity(OWNER_TYPE_ID="4", OWNER_ID="42", COMPLETED="Y", SUBJECT="Связаться с клиентом")

        def listing(method, params, key=None):
            if method == "crm.activity.list":
                return [] if params["filter"].get("COMPLETED") == "N" else [plan]
            if method == "voximplant.statistic.get":
                return [call(CRM_ENTITY_TYPE="COMPANY", CRM_ENTITY_ID="42", CALL_TYPE="2", CALL_DURATION=180)]
            return []

        client.list_all = listing
        client.batch = mock.Mock(return_value=({"binding201": []}, {}))
        client.resolve_field = mock.Mock(return_value=None)
        client._meeting_calendar_files = mock.Mock(return_value={})
        client._legacy_tenders = mock.Mock(return_value=[])
        client._enum_yes = mock.Mock(return_value={})
        client.lead_statuses = mock.Mock(return_value={})
        data = client.fetch({56: "tender"}, dt.date(2026, 11, 1))
        self.assertEqual(len(data.get("crm_followups", [])), 1)
        self.assertEqual(data["activities"][0]["company_id"], "42")

    def test_fetch_includes_the_retained_historical_activity_window(self):
        client = Bitrix24({"webhook": "https://crm.example.test/rest/1/test/"}, log=lambda *args: None)
        old_plan = activity(OWNER_TYPE_ID="4", OWNER_ID="42", COMPLETED="Y", CREATED="2026-04-02T12:00:00+05:00")
        requests = []
        def listing(method, params, key=None):
            requests.append((method, params))
            if method == "crm.activity.list" and params["filter"].get("<CREATED"):
                return [old_plan]
            return []
        client.list_all = listing
        client.batch = mock.Mock(return_value=({"binding201": []}, {}))
        client.resolve_field = mock.Mock(return_value=None)
        client._meeting_calendar_files = mock.Mock(return_value={})
        client._legacy_tenders = mock.Mock(return_value=[])
        client._enum_yes = mock.Mock(return_value={})
        client.lead_statuses = mock.Mock(return_value={})
        data = client.fetch({56: "tender"}, dt.date(2026, 11, 1), followup_from=dt.date(2026, 4, 1))
        self.assertEqual(len(data["crm_followups"]), 1)
        self.assertTrue(any(params.get("filter", {}).get("<CREATED") == "2026-11-01"
                            for _, params in requests))


class TenderFollowupRulesTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.now = dt.datetime(2026, 11, 10, 12)

    def plan(self, **changes):
        values = {"OWNER_TYPE_ID": "4", "OWNER_ID": "42", "DEADLINE": "2099-11-11T16:00:00+05:00",
                  "CREATED": "2026-11-03T10:30:00", "SUBJECT": "Связаться с клиентом", **changes}
        return tender_followups.normalize(activity(**values))

    def store(self, *rows):
        tender_followups.store(self.con, rows, "2026-11-01")
        self.con.execute("UPDATE dataset_state SET date_to='2026-11-30' WHERE dataset='crm_followups'")

    def selected(self, **changes):
        row = {"id": "call101", "dt": "2026-11-03T10:00:00", "company_id": "42", "crm_activity_id": "100", **changes}
        tender_followups.mark_calls(self.con, [row], self.now)
        return row

    def test_universal_plan_is_valid_and_actual_telephone_record_is_excluded(self):
        self.assertTrue(tender_followups.assigned_activity(activity(SUBJECT="Связаться с клиентом")))
        self.assertTrue(tender_followups.assigned_activity(activity(SUBJECT="Позвонить клиенту")))
        self.assertFalse(tender_followups.assigned_activity(activity(TYPE_ID="2")))
        self.assertFalse(tender_followups.assigned_activity(activity(PROVIDER_ID="VOXIMPLANT_CALL")))
        self.assertFalse(tender_followups.assigned_activity(activity(TYPE_ID="4", PROVIDER_ID="CRM_EMAIL")))

    def test_24_hour_window_and_company_binding(self):
        for created, expected in (("2026-11-03T09:59:59", 0), ("2026-11-03T10:00:00", 1),
                                  ("2026-11-04T10:00:00", 1), ("2026-11-04T10:00:01", 0)):
            with self.subTest(created=created):
                self.store(self.plan(CREATED=created))
                self.assertEqual(self.selected()["next_step"], expected)
        self.store(self.plan())
        self.assertEqual(self.selected(company_id="43")["next_step"], 0)

    def test_completed_plan_survives_deadline_and_open_plan_expires(self):
        self.store(self.plan(COMPLETED="Y", DEADLINE="2026-11-04T10:00:00"))
        self.assertEqual(self.selected()["followup_status"], "completed")
        self.store(self.plan(DEADLINE="2026-11-04T10:00:00"))
        self.assertEqual(self.selected()["followup_status"], "overdue")
        self.store(self.plan(DEADLINE=self.now.isoformat()))
        self.assertEqual(self.selected()["followup_status"], "open")

    def test_completed_activity_precedes_open_and_multiple_activities_count_once(self):
        self.store(self.plan(), self.plan(ID="202", COMPLETED="Y"))
        row = self.selected()
        self.assertEqual(row["next_step"], 1)
        self.assertEqual(row["followup_status"], "completed")
        self.assertEqual(len(row["followups"]), 2)

    def test_observed_changes_and_manual_baseline_continue_after_next_sync(self):
        self.store(self.plan())
        self.store(self.plan())
        self.assertEqual(self.selected()["followups"][0]["moves"], 0)
        self.store(self.plan(DEADLINE="2099-11-12T16:00:00+05:00"))
        tender_followups.set_moves(self.con, "201", "5", "boss")
        self.assertEqual(self.selected()["next_step"], 1)
        self.store(self.plan(DEADLINE="2099-11-13T16:00:00+05:00", COMPLETED="Y"))
        row = self.selected()
        self.assertEqual(row["followups"][0]["total_moves"], 6)
        self.assertEqual(row["followup_status"], "overdue")
        tender_followups.set_moves(self.con, "201", None, "boss")
        self.assertEqual(self.selected()["followups"][0]["total_moves"], 2)
        self.assertEqual(self.selected()["next_step"], 1)

    def test_deleted_activity_is_excluded_and_manual_values_are_retained(self):
        self.store(self.plan())
        tender_followups.set_moves(self.con, "201", 6, "boss")
        self.store()
        self.assertEqual(self.selected()["next_step"], 0)
        self.store(self.plan())
        self.assertEqual(self.selected()["followups"][0]["total_moves"], 6)

    def test_clearing_deadline_does_not_reset_the_last_observed_date(self):
        self.store(self.plan())
        self.store(self.plan(DEADLINE=None))
        self.store(self.plan(DEADLINE="2099-11-12T16:00:00+05:00"))
        self.assertEqual(self.selected()["followups"][0]["moves"], 1)

    def test_unknown_legacy_company_and_uncovered_period_are_not_confirmed(self):
        self.store(self.plan())
        self.assertIsNone(self.selected(company_id=None)["next_step"])
        self.assertIsNone(self.selected(dt="2026-10-31T10:00:00")["next_step"])
        self.con.execute("UPDATE dataset_state SET date_to='2026-11-02' WHERE dataset='crm_followups'")
        self.assertIsNone(self.selected()["next_step"])

    def test_call_timestamp_offset_is_normalized_for_window(self):
        self.store(self.plan(CREATED="2026-11-03T12:30:00+05:00"))
        row = self.selected(dt="2026-11-03T08:00:00", call_started_at="2026-11-03T08:00:00Z")
        self.assertEqual(row["next_step"], 0)  # The plan predates the actual call by 30 minutes.

    def test_invalid_manual_counts_and_missing_activity(self):
        self.store(self.plan())
        for value in (-1, 1.5, True, "1.5", "abc", 2**64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tender_followups.set_moves(self.con, "201", value, "boss")
        with self.assertRaises(ValueError):
            tender_followups.set_moves(self.con, "404", 1, "boss")

    def test_engine_and_audit_share_planned_activity_rule_for_sales_and_tender(self):
        self.store(self.plan(COMPLETED="Y"))
        rows = [{**dashboard_call(1, login, "2026-11-03", next_step=0), "company_id": "42"}
                for login in ("tender", "pm")]
        replace_rows(self.con, "activities", rows)
        engine = Engine(self.con, load_rules(), CONFIG)
        self.assertEqual(len(engine._result_calls("tender", engine._ctx("2026-11"))[0]), 1)
        self.assertEqual(len(engine._result_calls("pm", engine._ctx("2026-11"))[0]), 1)
        audit = engine.activity_audit("tender", "2026-11", "base_development", "calls")
        self.assertEqual(audit["fact"], 1)
        self.assertTrue(audit["records"][0]["counted"])
        self.assertEqual(audit["records"][0]["followups"][0]["total_moves"], 0)
        item = engine.calc_base_development(engine.employee("tender"), engine._ctx("2026-11"), {"result_calls_per_day": 3})
        self.assertEqual([r[1] for r in item["followup_summary"]["rows"]], [0, 1, 0])
        funnel = engine.call_funnel("tender", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        self.assertEqual(funnel.get("followup_summary", {}).get("rows"), item["followup_summary"]["rows"])

    def test_sales_summary_and_audit_use_lpr_window_status_and_manual_moves(self):
        self.store(self.plan(COMPLETED="Y"),
                   self.plan(ID="202", OWNER_ID="43", CREATED="2026-11-01T10:30:00"),
                   self.plan(ID="203", OWNER_ID="44", DEADLINE="2000-11-03T12:00:00"),
                   self.plan(ID="204", OWNER_ID="45", COMPLETED="Y"),
                   self.plan(ID="205", OWNER_ID="46"))
        tender_followups.set_moves(self.con, "204", 6, "boss")
        rows = [{**dashboard_call(i, "pm", "2026-11-03"), "company_id": str(41 + i),
                 "direction": "in" if i % 2 else "out"} for i in range(1, 6)]
        rows.append({**dashboard_call(6, "pm", "2026-11-03", lpr=0), "company_id": "42"})
        replace_rows(self.con, "activities", rows)
        engine = Engine(self.con, load_rules(), CONFIG)
        report = engine.compute("pm", "2026-11")
        item = next(row for row in report["items"] if row["code"] == "operational_activity")
        self.assertEqual(item["components"][0]["fact"], 2)
        self.assertEqual([r[1] for r in item["followup_summary"]["rows"]], [1, 1, 2])
        funnel = engine.call_funnel("pm", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        self.assertTrue(funnel["require_lpr"])
        self.assertEqual(funnel["result"], 2)
        self.assertEqual([r[1] for r in funnel["followup_summary"]["rows"]], [1, 1, 2])
        audit = engine.activity_audit("pm", "2026-11", "operational_activity", "calls")
        self.assertEqual(audit["fact"], 2)
        self.assertEqual(sum(r["counted"] is True for r in audit["records"]), 2)
        no_lpr = next(row for row in audit["records"] if row["id"] == "cpm6")
        self.assertEqual(next(c["state"] for c in no_lpr["checks"] if c["key"] == "lpr"), "fail")
        moved = next(row for row in audit["records"] if row["id"] == "cpm4")
        self.assertEqual(moved["followups"][0]["total_moves"], 6)

    def test_sales_missing_company_keeps_call_fact_unknown(self):
        self.store(self.plan(COMPLETED="Y"))
        replace_rows(self.con, "activities", [dashboard_call(1, "pm", "2026-11-03")])
        engine = Engine(self.con, load_rules(), CONFIG)
        item = next(row for row in engine.compute("pm", "2026-11")["items"]
                    if row["code"] == "operational_activity")
        self.assertIsNone(item["components"][0]["fact"])
        self.assertEqual(item["followup_summary"]["unknown"], 1)

    def test_daily_cap_does_not_show_a_successful_contact_as_overdue(self):
        self.store(self.plan(CREATED="2026-11-03T09:00:00", DEADLINE="2026-11-03T12:00:00"),
                   self.plan(ID="202", CREATED="2026-11-04T10:00:00", COMPLETED="Y"))
        rows = [{**dashboard_call(1, "tender", "2026-11-03", next_step=0, contact="COMPANY:42"),
                 "dt": "2026-11-03T08:00:00", "company_id": "42"},
                {**dashboard_call(2, "tender", "2026-11-03", next_step=0, contact="COMPANY:42"),
                 "dt": "2026-11-03T20:00:00", "company_id": "42"}]
        replace_rows(self.con, "activities", rows)
        engine = Engine(self.con, load_rules(), CONFIG)
        result = engine.calc_base_development(engine.employee("tender"), engine._ctx("2026-11"),
                                              {"result_calls_per_day": 3, "one_call_per_contact_day": True})
        self.assertEqual(result["components"][0]["fact"], 1)
        self.assertEqual([r[1] for r in result["followup_summary"]["rows"]], [0, 1, 0])

    def test_unknown_duplicate_does_not_hide_a_fully_confirmed_daily_call(self):
        self.store(self.plan(COMPLETED="Y"))
        rows = [{**dashboard_call(1, "tender", "2026-11-03", contact="COMPANY:42"), "company_id": "42"},
                {**dashboard_call(2, "tender", "2026-11-03", contact="COMPANY:42"), "company_id": None}]
        replace_rows(self.con, "activities", rows)
        engine = Engine(self.con, load_rules(), CONFIG)
        result = engine.calc_base_development(engine.employee("tender"), engine._ctx("2026-11"),
                                              {"result_calls_per_day": 3, "one_call_per_contact_day": True})
        self.assertEqual(result["components"][0]["fact"], 1)
        self.assertEqual(result["followup_summary"]["unknown"], 0)


if __name__ == "__main__":
    unittest.main()
