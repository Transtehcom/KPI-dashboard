import copy
import unittest
from unittest.mock import patch

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

    def item(self):
        with patch.object(self.app.engine, "trend", return_value=[]):
            report = self.app.build_employee("supply", "2026-11")
        return next(i for i in report["items"] if i["code"] == "deferred_payment")

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
        item = self.item()
        self.assertEqual(item["status"], "absent")
        self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])

    def test_threshold_applies_to_a_draft_profile(self):
        self.app.rules["positions"]["supply"]["draft"] = True
        item = self.item()
        self.assertEqual(item["status"], "draft")
        self.assertEqual([r["ref"] for r in item["entry"]["rows"]], ["o4", "o5"])


if __name__ == "__main__":
    unittest.main()
