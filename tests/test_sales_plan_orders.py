import copy
import unittest

from app.common import connect, load_rules, migrate, replace_rows, table_columns
from app.engine import Engine
from app.onec import OneC
from tests.test_engine import CONFIG


class SalesPlanOrdersTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.engine = Engine(self.con, self.rules, copy.deepcopy(CONFIG))

    def loaded(self):
        self.con.execute("INSERT OR REPLACE INTO dataset_state VALUES ('conversion_orders','2026-01-01','2026-12-31','test')")

    def put(self, rows):
        replace_rows(self.con, "conversion_orders", rows)

    def order(self, key, emp="pm", **changes):
        return {"id": key, "order_key": key, "emp": emp, "dt": "2026-10-26", "doc": "ЗК-" + key,
                "role": "sales", "status": "ТТК_ВЗакуп", "amount": 123.45, **changes}

    def calc(self, login="pm"):
        return self.engine.calc_gross_profit(self.engine.employee(login), self.engine._ctx("2026-10"),
                                              {"metric": "revenue", "team_plan": True})

    def test_raw_order_sum_is_selected_and_normalized_without_vat_subtraction(self):
        class API(OneC):
            def _odata(api, entity, params):
                self.assertEqual(entity, "Document_ЗаказКлиента")
                self.assertIn("СуммаДокумента", params["$select"])
                return [{"Ref_Key": "o1", "Number": "139", "Date": "2026-10-26", "Статус": "ТТК_ВЗакуп",
                         "СуммаДокумента": "257020,16", "Менеджер": {"Description": "Анна"}}]
        api = API({"base_url": "https://example.test/", "datasets": {"conversion_orders": {"source": "conversion_orders"}}}, log=lambda *a: None)
        import datetime as dt
        rows = api.fetch(self.con, {"анна": "lead"}, dt.date(2026, 10, 1), dt.date(2026, 10, 31))["conversion_orders"]
        self.assertEqual(rows[0]["amount"], 257020.16)

    def test_existing_order_schema_gains_nullable_amount_without_losing_rows(self):
        self.con.execute("DROP TABLE conversion_orders")
        self.con.execute("CREATE TABLE conversion_orders (id TEXT PRIMARY KEY,emp TEXT,dt TEXT,doc TEXT,client TEXT,"
                         "status TEXT,role TEXT,order_key TEXT,request_docs TEXT)")
        self.con.execute("INSERT INTO conversion_orders (id,emp,dt,role,status,order_key) "
                         "VALUES ('old','pm','2026-10-01','sales','ТТК_ВЗакуп','old')")
        migrate(self.con)
        self.assertIn("amount", table_columns(self.con, "conversion_orders"))
        self.assertIsNone(self.con.execute("SELECT amount FROM conversion_orders WHERE id='old'").fetchone()[0])

    def test_sales_plan_sums_qualifying_orders_once_for_all_sales_profiles(self):
        self.loaded()
        self.con.execute("INSERT INTO sales (id,emp,dt,revenue,signed,signed_date) "
                         "VALUES ('upd','pm','2026-10-01',999999,1,'2026-10-01')")
        for login in ("pm", "tender", "lead"):
            with self.subTest(login=login):
                rows = [self.order("a", login), self.order("b", login, amount=10.11),
                        self.order("closed", login, status="Закрыт", amount=1000000),
                        self.order("planned", login, status="КОбеспечению"),
                        self.order("old", login, dt="2026-09-30"), self.order("future", login, dt="2026-11-01"),
                        self.order("supply", login, role="supply", amount=1000000),
                        self.order("foreign", "supply", amount=1000000),
                        {**self.order("a", login), "id": "duplicate"}]
                self.put(rows)
                r = self.calc(login)
                self.assertEqual(r["components"][0]["fact"], 133.56)
                self.assertEqual(len(r["details"]["rows"]), 2)
                self.assertEqual(r["plan"]["metric"], "revenue")
                self.assertEqual(r["plan"]["value"], 50_000_000)

    def test_unknown_amount_is_not_zero_but_real_zero_is_valid(self):
        self.loaded()
        self.put([self.order("old-schema", amount=None)])
        r = self.calc()
        self.assertIsNone(r["components"][0]["fact"])
        self.assertTrue(r["warnings"])
        self.put([self.order("zero", amount=0)])
        self.assertEqual(self.calc()["components"][0]["fact"], 0)

    def test_no_orders_after_successful_load_is_zero_but_missing_source_is_unknown(self):
        self.assertIsNone(self.calc()["components"][0]["fact"])
        self.loaded()
        self.assertEqual(self.calc()["components"][0]["fact"], 0)

    def test_individual_plan_override_is_preserved(self):
        self.loaded()
        self.put([self.order("a")])
        self.con.execute("INSERT INTO plans VALUES ('pm','2026-10','revenue',1250000)")
        r = self.calc()
        self.assertEqual(r["plan"], {"metric": "revenue", "value": 1250000, "is_default": False})
        self.assertEqual(r["components"][0]["target"], 1250000)


if __name__ == "__main__":
    unittest.main()
