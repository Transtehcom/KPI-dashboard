import datetime as dt
import unittest
from unittest import mock

from app import b24_calendar
from app.bitrix24 import B24Error


def todo(id="1", owner_type="1", owner="2414", **changes):
    return {"ID": id, "OWNER_TYPE_ID": owner_type, "OWNER_ID": owner,
            "SUBJECT": "Выйти на подрядчиков, собрать инфу", "PROVIDER_ID": "CRM_TODO", "TYPE_ID": "6",
            "RESPONSIBLE_ID": "56", "CREATED": "2026-11-02T16:27:00",
            "DEADLINE": "2026-11-11T16:00:00", "START_TIME": "2026-11-11T16:00:00",
            "COMPLETED": "N", **changes}


class CRMCalendarTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.errors = [], set()
        b24_calendar._cache.clear()
        test = self

        class API:
            def __init__(self, *args, **kwargs):
                pass

            def call(self, method, params):
                if method in test.errors:
                    raise B24Error("insufficient_scope")
                if method == "calendar.event.get":
                    return {"result": []}
                raise AssertionError(method)

            def list_all(self, method, params, key=None):
                if method in test.errors:
                    raise B24Error("insufficient_scope")
                if method == "tasks.task.list":
                    return []
                if method == "crm.activity.list":
                    return test.rows
                raise AssertionError(method)

        self.patch = mock.patch("app.b24_calendar.Bitrix24", API)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(b24_calendar._cache.clear)
        self.cfg = {"webhook": "https://crm.example.test/rest/1/test/"}

    def schedule(self, day="2026-11-11"):
        return b24_calendar.day_schedule(self.cfg, 56, dt.date.fromisoformat(day))

    def test_todo_is_shown_on_due_day_instead_of_creation_day(self):
        self.rows = [todo()]
        self.assertEqual(self.schedule("2026-11-02")["items"], [])
        items = self.schedule()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["kind"], items[0]["title"], items[0]["to"]),
                         ("activity", "Выйти на подрядчиков, собрать инфу", "16:00"))
        self.assertEqual(items[0]["card_url"], "https://crm.example.test/crm/lead/details/2414/")
        self.assertEqual(items[0]["url"], items[0]["card_url"] + "?activity_id=1")
        self.assertEqual(self.schedule("2026-11-12")["items"], [])

    def test_all_crm_card_types_and_duplicate_activity_ids(self):
        self.rows = [todo(str(n), str(n), str(100 + n)) for n in range(1, 5)]
        self.rows += [self.rows[0]]
        items = self.schedule()["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual({x["card_url"] for x in items}, {
            "https://crm.example.test/crm/lead/details/101/", "https://crm.example.test/crm/deal/details/102/",
            "https://crm.example.test/crm/contact/details/103/", "https://crm.example.test/crm/company/details/104/"})

    def test_rest_day_bounds_have_offsets_and_include_local_midnight_todos(self):
        day = dt.date(2026, 11, 11)
        local = dt.datetime.combine(day, dt.time()).astimezone()
        due = local + dt.timedelta(minutes=15)
        portal_tz = dt.timezone(dt.timedelta(hours=3))
        rows = [todo(DEADLINE=due.astimezone(portal_tz).isoformat(), START_TIME=None),
                todo("2", DEADLINE=None, START_TIME=due.astimezone(dt.timezone.utc).isoformat())]
        fields = []
        def rest_list(method, params):
            field = "DEADLINE" if ">=DEADLINE" in params["filter"] else "START_TIME"
            fields.append(field)
            lower = dt.datetime.fromisoformat(params["filter"][">=" + field])
            upper = dt.datetime.fromisoformat(params["filter"]["<=" + field])
            self.assertIsNotNone(lower.utcoffset(), "REST bounds must carry the dashboard timezone")
            self.assertEqual(lower, local)
            self.assertEqual(upper, local + dt.timedelta(days=1, seconds=-1))
            lower, upper = lower.astimezone(portal_tz), upper.astimezone(portal_tz)
            return [row for row in rows if row.get(field) and
                    lower <= dt.datetime.fromisoformat(row[field]).astimezone(portal_tz) <= upper]
        api = mock.Mock()
        api.list_all.side_effect = rest_list
        result = b24_calendar._crm_activities(api, 56, day, "https://crm.example.test")
        self.assertEqual([(r["id"], r["to"]) for r in result], [("1", "00:15"), ("2", "00:15")])
        self.assertEqual(fields, ["DEADLINE", "START_TIME"])

    def test_undated_other_responsible_and_other_activity_kinds_are_excluded(self):
        self.rows = [todo(DEADLINE=None, START_TIME=None), todo("2", RESPONSIBLE_ID="28"),
                     todo("3", DEADLINE="9999-12-31T00:00:00", START_TIME="0001-01-01T00:00:00"),
                     todo("4", PROVIDER_ID="VOXIMPLANT_CALL")]
        self.assertEqual(self.schedule()["items"], [])

    def test_completed_todo_and_start_time_fallback(self):
        self.rows = [todo(COMPLETED="Y", DEADLINE=None)]
        items = self.schedule()["items"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["done"])
        self.assertEqual(items[0]["to"], "16:00")

    def test_calendar_and_tasks_failures_do_not_hide_crm_todos(self):
        self.errors = {"calendar.event.get", "tasks.task.list"}
        self.rows = [todo()]
        result = self.schedule()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["calendar_error"], "scope")
        self.assertTrue(result["tasks_error"])

    def test_crm_failure_is_reported_separately(self):
        self.errors = {"crm.activity.list"}
        self.assertTrue(self.schedule().get("activities_error"))


if __name__ == "__main__":
    unittest.main()
