import unittest

from app.common import connect, load_rules
from app.engine import Engine
from app.manual import set_kpi_decision, set_kpi_exclusion, set_logistics_budget_inclusion
from test_engine import insert, order


class ProcurementLogisticsReviewTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), {})
        self.month = "2026-10"

    def result(self, code, role):
        params = next(i["params"] for i in self.engine.rules["positions"][role]["items"] if i["code"] == code)
        result = getattr(self.engine, "calc_" + code)({"login": role, "name": role}, self.engine._ctx(self.month), params)
        for counter in result["extra"]:
            if isinstance(counter["value"], int):
                self.assertEqual(len(counter["members"]), counter["value"], counter["label"])
                self.assertTrue(all(isinstance(label, str) and label for label in counter["members"]))
        return result

    def check_cycle(self, code, role, ref, auto, expected):
        result = self.result(code, role)
        self.assertEqual(len(result["review_rows"]), 1)
        row = result["review_rows"][0]
        self.assertEqual((row["ref"], row["auto"], row["value"]), (ref, auto, None))
        for decision, fact in zip((1, 0, None), expected):
            with self.subTest(code=code, decision=decision):
                set_kpi_decision(self.con, role, self.month, code, row["kind"], ref, decision, "boss")
                result = self.result(code, role)
                self.assertEqual(result["components"][0]["fact"], fact)
                self.assertEqual(result["review_rows"][0]["value"], decision)
                if decision == 1:
                    key = (self.con, role, self.month, code, row["kind"], ref)
                    set_kpi_exclusion(*key, True, "boss")
                    excluded = self.result(code, role)
                    self.assertIsNone(excluded["components"][0]["fact"])
                    self.assertTrue(excluded["review_rows"][0]["excluded"])
                    self.assertEqual(excluded["review_rows"][0]["value"], 1)
                    set_kpi_exclusion(*key, False, "boss")
                    restored = self.result(code, role)
                    self.assertFalse(restored["review_rows"][0]["excluded"])
                    self.assertEqual(restored["components"][0]["fact"], fact)

    def test_grouped_supply_delivery_and_order_deferral(self):
        insert(self.con, "supplier_orders", [
            order(i, "2026-10-01", amount, customer_order_key="client", planned_date="2026-10-03",
                  customer_actual_date="2026-10-04", complete=1, deferred_share=None)
            for i, amount in ((1, 600000), (2, 50))])
        self.check_cycle("on_time_delivery", "supply", "client", False, (0, 1, 1))
        self.check_cycle("deferred_payment", "supply", "o1", None, (1, 0, None))

    def test_competitive_purchase_groups_lot_and_keeps_exclusions(self):
        insert(self.con, "supplier_orders", [
            order(1, "2026-10-01", 300000, lot="LOT", kp_count=1),
            order(2, "2026-10-02", 200000, lot="lot", kp_count=2),
            order(3, "2026-10-03", 600000, emergency=1),
            order(4, "2026-10-04", 600000, single_source=1)])
        self.check_cycle("competitive_purchases", "supply", "lot:lot", False, (1, 0, 0))
        total, accepted = self.result("competitive_purchases", "supply")["extra"]
        self.assertIn("ЗП-1, ЗП-2", total["members"][0])
        self.assertEqual(accepted["members"], [])

    def test_logistics_orders_pending_data_and_prior_budget_exclusion(self):
        self.con.execute("INSERT INTO dataset_state VALUES('logistics_deliveries','2026-10-01','2026-10-31','test')")
        for identity in ("a", "b"):
            self.con.execute("INSERT INTO logistics_deliveries "
                             "(id,emp,dt,source_key,source_doc,order_key,order_doc,order_status) "
                             "VALUES(?,'logistics','2026-10-03',?,?,'client','Заказ','ТТК_ВЗакуп')",
                             (identity, identity, identity))
        for code in ("logistics_delivery", "logistics_budget"):
            self.check_cycle(code, "logistics", "client", None, (1, 0, None))
        set_logistics_budget_inclusion(self.con, "logistics", self.month, "client", 0, "boss")
        self.assertEqual(self.result("logistics_budget", "logistics")["review_rows"], [])

    def test_logistics_speed_documents_and_discipline(self):
        for table in ("transport_requests", "forwarder_orders", "forwarder_contracts"):
            self.con.execute("INSERT INTO dataset_state VALUES(?,'2026-10-01','2026-10-31','test')", (table,))
        self.con.execute("INSERT INTO transport_requests(id,emp,dt,doc) VALUES('tr','logistics','2026-10-04','ТР')")
        self.con.execute("INSERT INTO forwarder_orders(id,emp,dt,doc,deleted) VALUES('fo','logistics','2026-10-04','ПЭ',0)")
        self.con.execute("INSERT INTO forwarder_contracts(id,emp,dt,doc,has_contract) VALUES('fo','logistics','2026-10-04','ПЭ',0)")
        self.check_cycle("logistics_request_speed", "logistics", "tr", None, (1, 0, None))
        for code in ("logistics_documents", "logistics_discipline"):
            self.check_cycle(code, "logistics", "fo", False, (1, 0, 0))
