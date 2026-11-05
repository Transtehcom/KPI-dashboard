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
    def test_company_open_unexpired_activity_regardless_of_creation(self):
        from app.crm_activities import is_next_step_for_call
        now = dt.datetime(2026, 11, 5, 12)
        c = call(CRM_ENTITY_TYPE="COMPANY", CRM_ENTITY_ID="394")
        a = activity(OWNER_TYPE_ID="4", OWNER_ID="394", CREATED="2026-10-22T10:00:00+05:00")
        self.assertTrue(is_next_step_for_call(a, c, now=now))
        for changes in ({"COMPLETED":"Y"}, {"DEADLINE":"2026-10-16T11:00:00+05:00"},
                        {"DEADLINE":None}, {"OWNER_ID":"395"}, {"OWNER_TYPE_ID":"1"},
                        {"TYPE_ID":"2"}, {"SUBJECT":"Повторный звонок клиенту"}):
            with self.subTest(changes=changes):
                self.assertFalse(is_next_step_for_call({**a, **changes}, c, now=now))

    def test_index_keeps_old_open_company_activity_but_not_completed(self):
        b24 = Bitrix24({"webhook":"https://crm.example.test/rest/1/test/"}, log=lambda *a:None)
        b24.batch = mock.Mock(return_value=({"binding201":[]}, {}))
        a = activity(OWNER_TYPE_ID="4", OWNER_ID="394", CREATED="2020-01-01T00:00:00")
        index = b24.next_step_index([a, activity(ID="202", COMPLETED="Y")], [call()])
        self.assertEqual([r["ID"] for r in index[(4,"394")]], ["201"])

    def test_binding_read_failure_does_not_silently_zero_kpi(self):
        b24 = Bitrix24({"webhook":"https://crm.example.test/rest/1/test/"}, log=lambda *a:None)
        b24.batch = mock.Mock(return_value=({}, {"binding201":{"error":"ACCESS_DENIED"}}))
        with self.assertRaises(B24Error):
            b24.next_step_index([activity()], [call()])


if __name__ == "__main__":
    unittest.main()
