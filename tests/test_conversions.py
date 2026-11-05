import copy
import contextlib
import datetime as dt
import io
import unittest
from unittest import mock

from app.common import connect, load_rules, load_json, replace_rows, current_month, month_add
from app.engine import Engine
from app.onec import OneC
from tests.test_engine import CONFIG


class ConversionSourceTest(unittest.TestCase):
    def test_order_manager_and_old_request_owner_are_independent_and_deduplicated(self):
        from app.onec_conversions import fetch_conversion_orders
        calls = []
        class API:
            def _odata(self, entity, params):
                calls.append((entity, params))
                if entity == "Document_ЗаказКлиента":
                    return [{"Ref_Key": "order160", "Number": "00ЦБ-000160 ", "Date": "2026-10-26T10:36:20",
                             "Статус": "ТТК_ВЗакуп", "Менеджер": {"Description": "Анна"},
                             "Контрагент": {"Description": "Клиент"}, "ДокументОснование": "tender1"},
                            {"Ref_Key": "deleted", "Date": "2026-10-26", "DeletionMark": True},
                            {"Ref_Key": "old", "Date": "2026-09-30"}]
                if entity == "Document_ТТК_ЗаявкаВОтделСнабжения":
                    return [{"Ref_Key": "r1", "Number": "Т-000678 ", "Date": "2026-08-05T09:43:56",
                             "ДокументОснования": "tender1", "ОтветственныйСнабжение": {"Description": "Юрий"}},
                            {"Ref_Key": "r2", "Number": "Т-000679", "Date": "2026-09-01",
                             "ДокументОснования": "tender1", "ОтветственныйСнабжение": {"Description": "Юрий"}},
                            {"Ref_Key": "r3", "DeletionMark": True, "ДокументОснования": "tender1",
                             "ОтветственныйСнабжение": {"Description": "Другой"}}]
                raise AssertionError(entity)
        rows = fetch_conversion_orders(API(), {}, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        self.assertEqual({(r["role"], r["manager"]) for r in rows}, {("sales", "Анна"), ("supply", "Юрий")})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["dt"] == "2026-10-26" and r["doc"] == "00ЦБ-000160" for r in rows))
        supply = next(r for r in rows if r["role"] == "supply")
        self.assertIn("Т-000678", supply["request_docs"])
        self.assertIn("Т-000679", supply["request_docs"])
        request_filter = next(p["$filter"] for entity, p in calls if entity.endswith("Снабжения"))
        self.assertNotIn("Date ge", request_filter)

    def test_onec_dataset_dispatch_maps_both_responsibles(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        class API(OneC):
            def _odata(self, entity, params):
                if entity == "Document_ЗаказКлиента":
                    return [{"Ref_Key": "o1", "Number": "160", "Date": "2026-10-26", "Статус": "ТТК_ВЗакуп",
                             "Менеджер": {"Description": "Анна"}, "ДокументОснование": "basis"}]
                return [{"Number": "678", "Date": "2026-08-05", "ДокументОснования": "basis",
                         "ОтветственныйСнабжение": {"Description": "Юрий"}}]
        api = API({"base_url": "https://example.test/odata/", "datasets": {"conversion_orders": {"source": "conversion_orders"}}})
        out = api.fetch(con, {"анна": "lead", "юрий": "supply"}, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        self.assertIn("conversion_orders", out)
        self.assertEqual({(r["role"], r["emp"]) for r in out["conversion_orders"]}, {("sales", "lead"), ("supply", "supply")})


class OrderConversionTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules, self.config = load_rules(), copy.deepcopy(CONFIG)
        self.engine = Engine(self.con, self.rules, self.config)

    def loaded(self, first="2026-01-01", last="2026-12-31", datasets=("conversion_orders", "kp", "tenders_1c", "supply_funnel")):
        for dataset in datasets:
            self.con.execute("INSERT OR REPLACE INTO dataset_state VALUES (?,?,?,?)", (dataset, first, last, "now"))

    def put(self, table, rows):
        replace_rows(self.con, table, rows, emps=["__none__"])

    def order(self, key, emp, **changes):
        return {"id": key + emp, "order_key": key, "emp": emp, "dt": "2026-10-26", "doc": "00ЦБ-000160",
                "status": "ТТК_ВЗакуп", "role": "sales", **changes}

    def calc(self, emp, code, month="2026-10", **params):
        return getattr(self.engine, "calc_" + code)(self.engine.employee(emp), self.engine._ctx(month),
                                                   {"target": .10, **params})

    def test_tender_and_sales_orders_divided_by_kp_plus_all_tenders_once(self):
        self.loaded()
        for emp, code in (("tender", "conversion_kp_tenders"), ("lead", "conversion_kp_tenders"), ("pm", "conversion_sales")):
            self.put("kp", [{"id": emp + str(i), "emp": emp, "dt": "2026-10-03", "ordered": 1} for i in range(3)])
            self.put("tenders_1c", [{"id": emp + str(i), "emp": emp, "dt": "2026-10-04", "status": status, "ordered": 1}
                                      for i, status in enumerate(("Выиграно", "Отменено"))])
            self.put("conversion_orders", [self.order("160", emp)])
            r = self.calc(emp, code)
            self.assertAlmostEqual(r["components"][0]["fact"], 1 / 5)
            self.assertEqual(r["components"][0]["target"], .10)

    def test_status_date_owner_role_and_duplicate_order_exclusions(self):
        self.loaded()
        self.put("kp", [{"id": "k", "emp": "pm", "dt": "2026-10-03"},
                        {"id": "old", "emp": "pm", "dt": "2026-09-30"}])
        self.put("conversion_orders", [self.order("ok", "pm"), self.order("closed", "pm", status="Закрыт"),
            self.order("planned", "pm", status="КОбеспечению"), self.order("old", "pm", dt="2026-09-30"),
            self.order("new", "pm", dt="2026-11-01"), self.order("foreign", "lead"),
            self.order("supply", "pm", role="supply"), {**self.order("ok", "pm"), "id": "duplicate"}])
        self.assertEqual(self.calc("pm", "conversion_sales")["components"][0]["fact"], 1)

    def test_october_order_counts_for_both_anna_and_yuri_despite_august_request(self):
        self.loaded()
        self.put("kp", [{"id": "k", "emp": "lead", "dt": "2026-10-03"}])
        self.put("supply_funnel", [{"id": "aug", "emp": "supply", "dt": "2026-08-05", "stage": "ordered"}] +
                 [{"id": str(i), "emp": "supply", "dt": "2026-10-05", "stage": stage}
                  for i, stage in enumerate(("ordered", "done", "open", "cancelled"))])
        self.put("conversion_orders", [self.order("160", "lead"),
                                      self.order("160", "supply", role="supply", request_docs="Т-000678")])
        self.assertEqual(self.calc("lead", "conversion_kp_tenders")["components"][0]["fact"], 1)
        r = self.calc("supply", "supply_funnel")
        self.assertEqual(r["components"][0]["fact"], 1 / 4)
        self.assertTrue(any("00ЦБ-000160" in row for row in r["details"]["rows"]))

    def test_empty_loaded_orders_is_zero_but_unknown_or_incomplete_loading_is_undefined(self):
        self.put("kp", [{"id": "k", "emp": "pm", "dt": "2026-10-03"}])
        self.assertIsNone(self.calc("pm", "conversion_sales")["components"][0]["fact"])
        self.loaded("2026-10-02")
        self.assertIsNone(self.calc("pm", "conversion_sales")["components"][0]["fact"])
        self.loaded()
        self.assertEqual(self.calc("pm", "conversion_sales")["components"][0]["fact"], 0)

    def test_zero_denominator_is_undefined(self):
        self.loaded()
        self.put("conversion_orders", [self.order("160", "pm")])
        self.assertIsNone(self.calc("pm", "conversion_sales")["components"][0]["fact"])

    def test_missing_denominator_coverage_does_not_become_an_empty_known_dataset(self):
        self.loaded(datasets=("conversion_orders", "kp"))
        self.put("kp", [{"id": "k", "emp": "pm", "dt": "2026-10-03"}])
        self.put("conversion_orders", [self.order("160", "pm"), self.order("160", "supply", role="supply")])
        sales = self.calc("pm", "conversion_sales")
        self.assertIsNone(sales["components"][0]["fact"], "Unknown tenders must not inflate sales conversion")
        self.assertTrue(sales["warnings"])
        self.loaded(datasets=("tenders_1c",))  # A genuinely empty, successfully loaded tender source is valid.
        self.assertEqual(self.calc("pm", "conversion_sales")["components"][0]["fact"], 1)
        self.put("supply_funnel", [{"id": "r", "emp": "supply", "dt": "2026-10-01", "stage": "open"}])
        self.assertIsNone(self.calc("supply", "supply_funnel")["components"][0]["fact"])
        self.loaded(datasets=("supply_funnel",))
        self.assertEqual(self.calc("supply", "supply_funnel")["components"][0]["fact"], 1)

    def test_historical_sales_target_growth_is_preserved(self):
        self.loaded()
        self.con.execute("INSERT OR REPLACE INTO baselines VALUES ('pm','conversion_last_year',.2)")
        self.assertAlmostEqual(self.calc("pm", "conversion_sales", "2026-09", target=.07,
                                        growth_vs_last_year=.1)["components"][0]["target"], .22)

    def test_demo_provides_orders_and_complete_conversion_coverage(self):
        from app import demo
        cfg = load_json("config.example.json")
        demo.generate(self.con, cfg)
        self.assertGreater(self.con.execute("SELECT COUNT(*) FROM conversion_orders").fetchone()[0], 0)
        self.assertIsNotNone(self.con.execute("SELECT 1 FROM dataset_state WHERE dataset='conversion_orders'").fetchone())
        engine = Engine(self.con, self.rules, cfg)
        person = next(e for e in cfg["employees"] if e.get("position") == "pm")
        item = next(i for i in engine.compute(person["login"], month_add(current_month(), -1))["items"] if i["n"] == 1)
        self.assertIsNotNone(item["components"][0]["fact"])

    def test_sync_marks_successful_empty_load_and_preserves_coverage_on_failure(self):
        from app import sync
        cfg = {**self.config, "mode": "live", "sync": {"months_back": 6},
               "onec": {"base_url": "https://example.test/", "datasets": {"conversion_orders": {"source": "conversion_orders"}}}}
        self.loaded("2025-01-01", "2025-12-31")
        with mock.patch("app.sync.OneC") as api, mock.patch("app.sync.onec_sales_register.fetch", return_value=[]), \
                mock.patch("app.sync.onec_money.fetch", return_value={"cash": [], "plan": []}):
            api.return_value.fetch.return_value = {"conversion_orders": []}
            r = sync.run(self.con, cfg)
            self.assertEqual(r["status"], "ok")
            state = tuple(self.con.execute("SELECT * FROM dataset_state WHERE dataset='conversion_orders'").fetchone())
            self.assertNotEqual(state[1:3], ("2025-01-01", "2025-12-31"))
            api.return_value.fetch.side_effect = ValueError("Source unavailable")
            with contextlib.redirect_stderr(io.StringIO()):
                failed = sync.run(self.con, cfg)
            self.assertEqual(failed["status"], "error")
            self.assertEqual(tuple(self.con.execute("SELECT * FROM dataset_state WHERE dataset='conversion_orders'").fetchone()), state)


if __name__ == "__main__":
    unittest.main()
