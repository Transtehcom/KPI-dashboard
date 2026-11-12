import copy
import unittest
from unittest.mock import patch

from app import manual
from app.common import connect, load_rules
from app.engine import Engine
from app.server import App
from tests.test_engine import CONFIG, insert, order


class DeferredPaymentEntryTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.app = App.__new__(App)
        self.app.con = self.con
        self.app.config = copy.deepcopy(CONFIG)
        self.app.rules = load_rules()
        self.app.engine = Engine(self.con, self.app.rules, self.app.config)
        insert(self.con, "supplier_orders", [
            order(1, "2026-11-10", 50081.97), order(2, "2026-11-10", 130339.67),
            order(3, "2026-11-30", 499999.99), order(4, "2026-11-01", 500000),
            order(5, "2026-11-30", 700000), order(6, "2026-10-31", 900000),
            order(7, "2026-11-03", 900000, emp="pm"), order(8, "2026-11-05", None),
            order(9, "2026-11-15", 250000),
        ])

    def item(self, code="deferred_payment"):
        with patch.object(self.app.engine, "trend", return_value=[]):
            report = self.app.build_employee("supply", "2026-11")
        return next(i for i in report["items"] if i["code"] == code)

    def test_entry_matches_calculation_threshold_employee_and_month(self):
        item = self.item()
        self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])
        self.assertEqual(item["extra"][0]["value"], 2)
        self.assertEqual({r["cells"][0] for r in item["entry"]["rows"]},
                         {r[0] for r in item["details"]["rows"]})

    def test_custom_threshold_is_shared_with_the_calculation(self):
        rule = next(i for i in self.app.rules["positions"]["supply"]["items"] if i["code"] == "deferred_payment")
        rule["params"]["threshold"] = 200000
        item = self.item()
        self.assertEqual({r["ref"] for r in item["entry"]["rows"]}, {"o3", "o4", "o5", "o9"})
        self.assertEqual(item["extra"][0]["value"], 4)

    def test_threshold_applies_when_the_employee_was_absent_all_month(self):
        employee = self.app.engine.employee("supply")
        employee["absences"] = [{"from": "2026-11-01", "to": "2026-11-30", "kind": "vacation"}]
        for code in ("deferred_payment", "competitive_purchases"):
            with self.subTest(code=code):
                item = self.item(code)
                self.assertEqual(item["status"], "absent")
                self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])

    def test_threshold_applies_to_a_draft_profile(self):
        self.app.rules["positions"]["supply"]["draft"] = True
        for code in ("deferred_payment", "competitive_purchases"):
            with self.subTest(code=code):
                item = self.item(code)
                self.assertEqual(item["status"], "draft")
                self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])

    def test_competitive_entry_shows_only_qualifying_purchases(self):
        item = self.item("competitive_purchases")
        self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])
        self.assertEqual({r["ref"] for r in item["entry"]["lot_candidates"]}, {"o1", "o2", "o3", "o8", "o9"})
        self.assertEqual(item["extra"][0]["value"], 2)

    def test_competitive_entry_includes_smaller_orders_of_an_explicit_qualifying_lot(self):
        insert(self.con, "supplier_orders", [
            order(10, "2026-11-20", 250000, lot="Lot-500", supplier="Завод 1"),
            order(11, "2026-11-21", 250000, lot=" lot-500 ", supplier="Завод 2"),
            order(12, "2026-11-22", 100000, lot="small-lot"),
            order(13, "2026-10-31", 800000, lot="Lot-500"),
            order(14, "2026-11-20", 800000, lot="Lot-500", emp="pm"),
        ])
        item = self.item("competitive_purchases")
        self.assertEqual({r["ref"] for r in item["entry"]["rows"]}, {"o4", "o5", "o10", "o11"})
        self.assertEqual(item["extra"][0]["value"], 3)
        self.assertTrue(any(row[0] == "Lot-500" and row[2] == 500000 for row in item["details"]["rows"]))

    def test_competitive_entry_uses_its_configured_threshold(self):
        rule = next(i for i in self.app.rules["positions"]["supply"]["items"] if i["code"] == "competitive_purchases")
        rule["params"]["threshold"] = 200000
        item = self.item("competitive_purchases")
        self.assertEqual({r["ref"] for r in item["entry"]["rows"]}, {"o3", "o4", "o5", "o9"})

    def test_lot_candidates_move_to_the_main_list_after_explicit_grouping(self):
        manual.set_value(self.con, "supplier_orders", "o3", "lot", "New-lot", "boss")
        self.assertNotIn("o3", {r["ref"] for r in self.item("competitive_purchases")["entry"]["rows"]})
        manual.set_value(self.con, "supplier_orders", "o9", "lot", "New-lot", "boss")
        entry = self.item("competitive_purchases")["entry"]
        self.assertTrue({"o3", "o9"}.issubset({r["ref"] for r in entry["rows"]}))
        self.assertTrue({"o3", "o9"}.isdisjoint({r["ref"] for r in entry["lot_candidates"]}))


if __name__ == "__main__":
    unittest.main()
