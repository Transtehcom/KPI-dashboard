import copy
import datetime as dt
import unittest
from contextlib import ExitStack
from unittest import mock

from app import manual
from app.common import connect, load_rules, replace_rows
from app.engine import Engine, comp
from tests.test_engine import CONFIG


class ProportionalPayoutTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules, self.config = load_rules(), copy.deepcopy(CONFIG)
        self.engine = Engine(self.con, self.rules, self.config)
        clock = mock.patch("app.engine.today", return_value=dt.date(2026, 11, 3))
        clock.start(); self.addCleanup(clock.stop)

    def calculate(self, login, result):
        position = self.engine.employee(login)["position"]
        with ExitStack() as stack:
            for code in {i["code"] for i in self.rules["positions"][position]["items"]}:
                stack.enter_context(mock.patch.object(self.engine, "calc_" + code, return_value=copy.deepcopy(result)))
            return self.engine.compute(login, "2026-11")

    def test_all_positions_pay_below_old_threshold_and_cap_overachievement(self):
        self.assertEqual(self.rules["payout"]["mode"], "proportional")
        for login in ("lead", "tender", "pm", "supply"):
            for fact, paid in ((0, 0), (1, .01), (29, .29), (29.49, .29), (50.5, .51), (79, .79), (100, 1), (114, 1)):
                with self.subTest(login=login, fact=fact):
                    result = self.calculate(login, {"components": [comp("Выполнение", fact, 100)], "payout_share": 0})
                    for item in result["items"]:
                        self.assertAlmostEqual(item["pct"], fact / 100)
                        self.assertEqual(item["payout"], round(item["amount"] * paid))
                    self.assertLessEqual(result["payout"], result["kpi_bonus"])

    def test_fines_missing_data_and_pending_override_proportional_payout(self):
        for changes in ({"penalty_zero": "Штраф"}, {"payout_pending": True},
                        {"components": [comp("Выполнение", 29, 100), comp("Нет данных", None, 100)]}):
            result = self.calculate("tender", {"components": [comp("Выполнение", 29, 100)], **changes})
            self.assertEqual(result["payout"], 0)

    def test_receivables_29_percent_pays_580_instead_of_old_zero_band(self):
        replace_rows(self.con, "receivables", [{"id": "r", "emp": "lead", "doc": "З-1", "client": "Клиент",
            "order_date": "2026-09-01", "ship_date": "2026-09-30", "due_date": "2026-09-30", "amount": 1000}])
        manual.set_penalty_check(self.con, "lead", "2026-11", True, "boss")
        item = next(i for i in self.engine.compute("lead", "2026-11")["items"] if i["code"] == "receivables_overdue")
        self.assertEqual(item["components"][0]["fact"], 24)
        self.assertAlmostEqual(item["pct"], 7 / 24)
        self.assertEqual((item["amount"], item["payout"]), (2000, 580))
        self.assertNotIn("payout_share", item)
        manual.add_penalty(self.con, "lead", "2026-11-03", 0, "Штраф", "boss")
        item = next(i for i in self.engine.compute("lead", "2026-11")["items"] if i["code"] == "receivables_overdue")
        self.assertEqual((item["payout"], item["status"]), (0, "penalty_zero"))

    def test_operational_three_equal_capped_shares(self):
        for values, expected in (((.02,.04,.2), .26/3), ((2,0,0),1/3), ((1,1,1),1), ((0,0,0),0)):
            with self.subTest(values=values):
                with mock.patch.object(self.engine, "calc_operational_activity", return_value={
                    "components":[comp(str(i),v,1) for i,v in enumerate(values)],
                    "aggregation":"equal_mean", "payout_precision":2}):
                    item=next(i for i in self.engine.compute("pm","2026-11")["items"] if i["code"]=="operational_activity")
                self.assertAlmostEqual(item["pct"],expected)
                self.assertEqual(item["payout"],round(item["amount"]*expected,2))

    def test_operational_calculator_declares_equal_shares(self):
        item=next(i for i in self.engine.compute("pm","2026-11")["items"] if i["code"]=="operational_activity")
        self.assertEqual(item.get("aggregation"),"equal_mean")
