import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.common import connect, load_rules
from app.engine import Engine
from app.operations import TZ


class LogisticsRequestSpeedTest(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 13, 10)
        clock = SimpleNamespace(**vars(dt))
        clock.datetime = Mock(wraps=dt.datetime)
        clock.datetime.now.return_value = self.now.replace(tzinfo=TZ)
        for target, value in (("app.engine.dt", clock), ("app.engine.today", Mock(return_value=self.now.date()))):
            replacement = patch(target, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_pending_is_excluded_expired_open_is_late_and_unknown_blocks_payout(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        engine = Engine(con, load_rules(), {"holidays": [], "employees": [
            {"login": "logist", "position": "logistics", "name": "Логист"}]})
        con.execute("INSERT INTO dataset_state VALUES('transport_requests','2026-11-01','2026-11-30','now')")
        now = self.now
        completed = [["Поступила", (now - dt.timedelta(days=3)).isoformat()],
                     ["Отработана", (now - dt.timedelta(days=2)).isoformat()]]
        pending = [["Поступила", (now - dt.timedelta(hours=23)).isoformat()]]
        late = [["Поступила", (now - dt.timedelta(hours=25)).isoformat()]]
        for key, status, history in (("good", "Отработана", completed),
                                      ("pending", "Поступила", pending), ("late", "Поступила", late)):
            con.execute("INSERT INTO transport_requests(id,emp,dt,status,history) VALUES(?,?,?,?,?)",
                        (key, "logist", "2026-11-01", status, json.dumps(history)))
        item = engine.compute("logist", "2026-11")["items"][2]
        self.assertEqual(item["components"][0]["fact"], .5)
        self.assertEqual((item["pct"], item["payout"]), (.53, 795))
        con.execute("UPDATE transport_requests SET status='Отработана' WHERE id='pending'")
        item = engine.compute("logist", "2026-11")["items"][2]
        self.assertIsNone(item["pct"])
        self.assertEqual(item["payout"], 0)

    def test_twenty_four_hours_and_95_percent_target_activate_payout(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        engine = Engine(con, load_rules(), {"holidays": [], "employees": [
            {"login": "logist", "position": "logistics", "name": "Логист"}]})
        con.execute("INSERT INTO dataset_state VALUES('transport_requests','2026-11-01','2026-11-30','now')")
        for good, payout in ((95, 1500), (76, 1200), (0, 0)):
            with self.subTest(good=good):
                con.execute("DELETE FROM transport_requests")
                for index in range(100):
                    finished = "2026-11-02T08:00:00" if index < good else "2026-11-02T08:00:01"
                    history = f'[["Поступила","2026-11-01T08:00:00"],["Отработана","{finished}"]]'
                    con.execute("INSERT INTO transport_requests(id,emp,dt,status,history) VALUES(?,?,?,?,?)",
                                (str(index), "logist", "2026-11-01", "Отработана", history))
                item = engine.compute("logist", "2026-11")["items"][2]
                self.assertEqual(item["payout"], payout)
                self.assertTrue(item["components"])
                self.assertEqual(item["components"][0]["fact"], good / 100)


class RequestTimingTest(unittest.TestCase):
    def timing(self, history, now="2026-11-02T08:00:00", status="Поступила"):
        from app.logistics_request_speed import request_timing
        return request_timing(history, status, dt.datetime.fromisoformat(now), 1440)

    def test_completed_boundary_and_latest_completion(self):
        history = '[["Отработана","2026-11-02T08:00:00"],["Поступила","2026-11-01T08:00:00"]]'
        self.assertEqual(self.timing(history)["category"], "good")
        history = '[["Поступила","2026-11-01T08:00:00"],["Отработана","2026-11-01T09:00:00"],["Отработана","2026-11-02T08:00:01"]]'
        self.assertEqual(self.timing(history, now="2026-11-02T08:00:02")["category"], "late")

    def test_open_request_waits_until_deadline_then_fails(self):
        history = '[["Поступила","2026-11-01T08:00:00"]]'
        self.assertEqual(self.timing(history)["category"], "pending")
        self.assertEqual(self.timing(history, now="2026-11-02T08:00:01")["category"], "late")
        self.assertEqual(self.timing(history, now="2026-12-01T08:00:01")["category"], "late")

    def test_unknown_or_invalid_completed_dates_never_become_timely(self):
        for history in (None, "broken", "{}", '[["Поступила","0001-01-01T00:00:00"]]',
                        '[["Поступила","2026-11-01T09:00:00"],["Отработана","2026-11-01T08:00:00"]]'):
            self.assertEqual(self.timing(history)["category"], "unknown")
        self.assertEqual(self.timing('[["Поступила","2026-11-01T08:00:00"]]',
                                    now="2026-11-03T08:00:00", status="Отработана")["category"], "unknown")

    def test_invalid_relevant_entries_and_future_arrivals_are_unknown(self):
        for history in (
            '[["Поступила","2026-11-01T08:00:00"],["Отработана","2026-11-01T09:00:00"],["Отработана","bad"]]',
            '[["Поступила","bad"],["Поступила","2026-11-01T08:00:00"],["Отработана","2026-11-01T09:00:00"]]',
            '[["Поступила","2026-11-03T08:00:00"]]',
        ):
            with self.subTest(history=history):
                self.assertEqual(self.timing(history)["category"], "unknown")
