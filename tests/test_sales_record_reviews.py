import unittest

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.manual import set_kpi_decision, set_kpi_exclusion
from tests.test_engine import CONFIG


class SalesRecordReviewsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), CONFIG)
        self.ctx = self.engine._ctx("2026-09")
        self.emp = self.engine.employee("pm")

    def decide(self, code, kind, ref, value):
        set_kpi_decision(self.con, "pm", "2026-09", code, kind, ref, value, "boss")

    def exclude(self, code, kind, ref, value=True):
        set_kpi_exclusion(self.con, "pm", "2026-09", code, kind, ref, value, "boss")

    def calc(self, code, **params):
        result = getattr(self.engine, "calc_" + code)(self.emp, self.ctx, params)
        return [component["fact"] for component in result["components"]]

    def test_order_and_denominator_decisions_are_independent_of_sales_plan(self):
        self.con.executemany("INSERT INTO dataset_state VALUES (?,'2026-01-01','2026-12-31','test')",
                             [(kind,) for kind in ("kp", "tenders_1c", "conversion_orders")])
        for ref, status, amount in (("one", "ТТК_ВЗакуп", 100), ("two", "Закрыт", 30)):
            self.con.execute("INSERT INTO conversion_orders (id,order_key,emp,role,dt,status,amount) "
                             "VALUES (?,?,'pm','sales','2026-09-12',?,?)", ("id-" + ref, ref, status, amount))
        self.con.executemany("INSERT INTO kp (id,emp,dt) VALUES (?,'pm','2026-09-10')", [("k1",), ("k2",)])
        self.assertEqual(self.calc("conversion_sales"), [.5])
        self.decide("conversion_sales", "order", "two", 1)
        self.decide("conversion_sales", "kp", "k1", 0)
        self.assertEqual(self.calc("conversion_sales"), [2])
        lists = self.engine.calc_conversion_sales(self.emp, self.ctx, {})["extra"]
        self.assertEqual([len(extra["members"]) for extra in lists], [2, 1, 0, 1])
        self.assertEqual(lists[3]["members"], lists[1]["members"])
        self.assertIn("КП k2", lists[1]["members"][0])
        self.assertEqual(self.calc("gross_profit", metric="revenue"), [100])
        self.decide("gross_profit", "order", "one", 0)
        self.decide("gross_profit", "order", "two", 1)
        self.assertEqual(self.calc("gross_profit", metric="revenue"), [30])
        sums = self.engine.calc_gross_profit(self.emp, self.ctx, {"metric": "revenue"})["extra"]
        self.assertEqual(len(sums[1]["members"]), 1)
        self.assertIn("id-two", sums[1]["members"][0])
        self.assertIn("30,00 ₽", sums[1]["members"][0])
        self.exclude("gross_profit", "order", "two")
        self.assertEqual(self.calc("gross_profit", metric="revenue"), [0])
        self.exclude("gross_profit", "order", "two", False)
        self.assertEqual(self.calc("gross_profit", metric="revenue"), [30])
        self.decide("conversion_sales", "order", "two", None)
        self.assertEqual(self.calc("conversion_sales"), [1])

    def test_missing_cost_does_not_become_zero_and_unsigned_sale_can_be_included(self):
        replace_rows(self.con, "sales", [
            {"id": "unsigned", "emp": "pm", "dt": "2026-09-02", "signed": 0, "revenue": 90, "cost": 30},
            {"id": "unknown", "emp": "pm", "dt": "2026-09-10", "signed": 1, "revenue": 25}])
        self.decide("gross_profit", "sale", "unsigned", 1)
        self.assertEqual(self.calc("gross_profit"), [None])
        self.assertNotIn("members", self.engine.calc_gross_profit(self.emp, self.ctx, {})["extra"][1])
        self.decide("gross_profit", "sale", "unknown", 0)
        self.assertEqual(self.calc("gross_profit"), [60])

    def test_fine_and_late_invoice_reviews_can_restore_receivables(self):
        self.con.execute("INSERT INTO sales (id,emp,dt,revenue,due_date) "
                         "VALUES ('r','pm','2026-08-01',100,'2026-09-01')")
        self.con.execute("INSERT INTO penalties (id,emp,dt,amount) VALUES ('fine','pm','2026-09-15',20)")
        code = "receivables_overdue"
        self.assertGreater(self.calc(code, penalties_zero=True)[0], 0)
        self.decide(code, "sale_due", "r", 0)
        self.decide(code, "penalty", "1c:fine", 0)
        result = self.engine.calc_receivables_overdue(self.emp, self.ctx, {"penalties_zero": True})
        self.assertEqual([c["fact"] for c in result["components"]], [0, 0])
        self.assertIsNone(result["penalty_zero"])
        self.assertEqual(result["extra"][0]["members"], [])
        self.assertEqual(result["extra"][-1]["members"], [])
        self.decide("receivables_pm", "payment_on_time", "r", 1)
        share, overdue = self.calc("receivables_pm", overdue_days_max=7)
        self.assertEqual(share, 1)
        self.assertGreater(overdue, 7)
        self.exclude("receivables_pm", "sale_due", "r")
        self.assertEqual(self.calc("receivables_pm", overdue_days_max=7), [None, 0])

    def test_tender_application_review_keeps_all_applications_in_denominator(self):
        self.con.executemany("INSERT INTO guarantees (id,emp,kind,unblock_date,return_date) "
                             "VALUES (?,'pm','application','2026-09-01',?)", [("ok", "2026-09-01"), ("late", None)])
        params = {"contract_return_days_max": 7, "unblock_share_min": 1, "penalties_max": 0}
        self.assertEqual(self.calc("tender_receivables", **params)[1], .5)
        self.decide("tender_receivables", "application_return", "late", 1)
        self.assertEqual(self.calc("tender_receivables", **params)[1], 1)
        self.decide("tender_receivables", "application_return", "late", 0)
        self.assertEqual(self.calc("tender_receivables", **params)[1], .5)
        self.exclude("tender_receivables", "application_return", "late")
        self.assertEqual(self.calc("tender_receivables", **params)[1], 1)

    def test_unloaded_sources_are_distinct_from_loaded_empty_lists(self):
        self.con.execute("INSERT INTO dataset_state VALUES ('kp','2026-01-01','2026-12-31','test')")
        items = self.engine.calc_conversion_sales(self.emp, self.ctx, {})["extra"]
        self.assertIsNone(items[0]["value"])
        self.assertNotIn("members", items[0])
        self.assertEqual((items[1]["value"], items[1]["members"]), (0, []))
        self.assertIsNone(items[3]["value"])
