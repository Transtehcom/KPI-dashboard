import datetime as dt
import unittest
from unittest import mock

from app.bitrix24 import Bitrix24, B24Error


def activity(**changes):
    return {"ID": "201", "OWNER_TYPE_ID": "1", "OWNER_ID": "2414", "RESPONSIBLE_ID": "56",
            "CREATED": "2026-11-03T10:30:00+05:00", "DEADLINE": "2026-11-11T16:00:00+05:00",
            "TYPE_ID": "6", "PROVIDER_ID": "CRM_TODO", "COMPLETED": "N", **changes}


def call(**changes):
    return {"ID": "101", "CRM_ACTIVITY_ID": "100", "CRM_ENTITY_TYPE": "LEAD", "CRM_ENTITY_ID": "2414",
            "PORTAL_USER_ID": "56", "CALL_START_DATE": "2026-11-03T10:00:00+05:00", **changes}


class CRMActivityDatesTest(unittest.TestCase):
    def test_deadline_fallback_and_service_dates(self):
        from app.crm_activities import activity_deadline
        for value in (None, "", "0001-01-01T00:00:00", "9999-12-31T23:59:59", "not a date"):
            with self.subTest(value=value):
                self.assertIsNone(activity_deadline({"DEADLINE": value}))
        self.assertEqual(activity_deadline({"DEADLINE": "2026-11-11T16:00:00", "START_TIME": "2026-11-12T17:00:00"}),
                         dt.datetime(2026, 11, 11, 16))
        self.assertEqual(activity_deadline({"START_TIME": "2026-11-11T16:00:00"}), dt.datetime(2026, 11, 11, 16))

    def test_offset_conversion_across_midnight(self):
        from app.crm_activities import activity_deadline
        tz = dt.timezone(dt.timedelta(hours=5))
        self.assertEqual(activity_deadline({"DEADLINE": "2026-11-10T22:30:00Z"}, tz=tz), dt.datetime(2026, 11, 11, 3, 30))

    def test_main_and_secondary_bindings_are_normalized(self):
        from app.crm_activities import activity_bindings
        activity = {"OWNER_TYPE_ID": "2", "OWNER_ID": "396", "BINDINGS": [
            {"entityTypeId": 3, "entityId": 27}, {"OWNER_TYPE_ID": "4", "OWNER_ID": "394"},
            {"entityTypeId": 2, "entityId": 396}, {"entityTypeId": 3, "entityId": 0}]}
        self.assertEqual(activity_bindings(activity), {(2, "396"), (3, "27"), (4, "394")})


class NextStepTest(unittest.TestCase):
    def test_dated_todo_confirms_call_even_when_later_completed(self):
        from app.crm_activities import is_next_step_for_call
        self.assertTrue(is_next_step_for_call(activity(), call()))
        self.assertTrue(is_next_step_for_call(activity(COMPLETED="Y"), call()))

    def test_wrong_card_and_repeat_calls_do_not_confirm(self):
        from app.crm_activities import is_next_step_for_call
        cases = [{"OWNER_ID": "2413"}, {"OWNER_TYPE_ID": "4"},
                 {"ID": "100"}, {"TYPE_ID": "2", "PROVIDER_ID": "VOXIMPLANT_CALL"},
                 {"TYPE_ID": "2", "PROVIDER_ID": "CRM_CALL"}, {"SUBJECT": "Повторный звонок клиенту"},
                 {"SUBJECT": "Перезвонить клиенту"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(is_next_step_for_call(activity(**changes), call()))

    def test_any_later_activity_without_deadline_or_assignee_restriction(self):
        from app.crm_activities import is_next_step_for_call
        for kind, provider in (("4", "CRM_EMAIL"), ("6", "CRM_TODO"), ("3", "CRM_TASKS_TASK"), ("1", "CRM_MEETING")):
            for card, owner in (("LEAD", "1"), ("DEAL", "2"), ("CONTACT", "3"), ("COMPANY", "4")):
                with self.subTest(kind=kind, card=card):
                    self.assertTrue(is_next_step_for_call(activity(TYPE_ID=kind, PROVIDER_ID=provider,
                        DEADLINE=None, RESPONSIBLE_ID="28", CREATED="2026-11-09T12:00:00+05:00", OWNER_TYPE_ID=owner),
                        call(CRM_ENTITY_TYPE=card)))

    def test_only_creation_after_call_matters(self):
        from app.crm_activities import is_next_step_for_call
        cases = [("2026-11-03T09:59:59+05:00", False), ("2026-11-03T10:00:00+05:00", False),
                 ("2026-11-04T10:00:00+05:00", True), ("2026-11-04T10:00:01+05:00", True)]
        for created, expected in cases:
            with self.subTest(created=created):
                self.assertEqual(is_next_step_for_call(activity(CREATED=created), call()), expected)
        self.assertTrue(is_next_step_for_call(activity(DEADLINE="2026-11-03T09:59:59+05:00"), call()))

    def test_index_keeps_late_email_for_a_different_responsible(self):
        b24 = Bitrix24({"webhook": "https://crm.example.test/rest/1/test/"}, log=lambda *a: None)
        a = activity(TYPE_ID="4", PROVIDER_ID="CRM_EMAIL", DEADLINE=None,
                     RESPONSIBLE_ID="28", CREATED="2026-11-09T12:00:00+05:00")
        b24.batch = mock.Mock(return_value=({"binding201": []}, {}))
        index = b24.next_step_index([a], [call()])
        self.assertEqual([r["ID"] for r in index[(1, "2414")]], ["201"])

    def test_secondary_binding_confirms_contact_call(self):
        from app.crm_activities import is_next_step_for_call
        a = activity(OWNER_TYPE_ID="2", OWNER_ID="396", BINDINGS=[{"entityTypeId": 3, "entityId": 27}])
        self.assertTrue(is_next_step_for_call(a, call(CRM_ENTITY_TYPE="CONTACT", CRM_ENTITY_ID="27")))

    def test_batch_bindings_index_exposes_secondary_card_and_skips_old_candidates(self):
        b24 = Bitrix24({"webhook": "https://crm.example.test/rest/1/test/"}, log=lambda *a: None)
        rows = [activity(OWNER_TYPE_ID="2", OWNER_ID="396"), activity(ID="202", CREATED="2026-10-01T00:00:00+05:00")]
        b24.batch = mock.Mock(return_value=({"binding201": [{"entityTypeId": 3, "entityId": 27}]}, {}))
        index = b24.next_step_index(rows, [call(CRM_ENTITY_TYPE="CONTACT", CRM_ENTITY_ID="27")])
        self.assertEqual([r["ID"] for r in index[(3, "27")]], ["201"])
        self.assertEqual(set(b24.batch.call_args.args[0]), {"binding201"})

    def test_binding_read_failure_does_not_silently_zero_kpi(self):
        b24 = Bitrix24({"webhook": "https://crm.example.test/rest/1/test/"}, log=lambda *a: None)
        b24.batch = mock.Mock(return_value=({}, {"binding201": {"error": "ACCESS_DENIED"}}))
        with self.assertRaises(B24Error):
            b24.next_step_index([activity()], [call()])


if __name__ == "__main__":
    unittest.main()
