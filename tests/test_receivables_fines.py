import copy
import datetime as dt
import unittest
import json
import urllib.request
import urllib.error
from unittest import mock

from app import manual
from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from tests.test_engine import CONFIG


class ReceivablesFinesTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.engine = Engine(self.con, self.rules, copy.deepcopy(CONFIG))
        self.clock = mock.patch("app.engine.today", return_value=dt.date(2026, 11, 30))
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.row = {"id": "r1", "emp": "tender", "doc": "Заказ-1", "client": "Клиент", "order_date": "2026-10-01",
                    "ship_date": "2026-11-01", "due_date": "2026-11-30", "paid_date": None, "amount": 1000}
        replace_rows(self.con, "receivables", [self.row])

    def item(self, login="tender", month="2026-11"):
        return next(i for i in self.engine.compute(login, month)["items"] if i["code"] == "receivables_overdue")

    def test_october_merge_keeps_september_history_and_total_weights(self):
        for login in ("tender", "lead", "pm"):
            for month in ("2026-10","2026-11"):
                d = self.engine.compute(login, month)
                self.assertEqual(len(d["items"]), 5)
                self.assertAlmostEqual(sum(i["weight"] for i in d["items"]), 1)
                item=self.item(login,month)
                self.assertEqual((item["weight"],item["amount"]),(.2,2000))
                self.assertTrue(item["uses_penalties"])
                self.assertNotIn("tender_receivables",[i["code"] for i in d["items"]])
        september=self.engine.compute("tender","2026-09")["items"]
        self.assertIn("tender_receivables",[i["code"] for i in september])
        self.assertNotIn("receivables_overdue",[i["code"] for i in september])

    def test_october_fine_zeroes_merged_item_only(self):
        manual.set_penalty_check(self.con,"tender","2026-10",True,"boss")
        before=self.engine.compute("tender","2026-10")
        manual.add_penalty(self.con,"tender","2026-10-05",0,"Штраф","boss")
        after=self.engine.compute("tender","2026-10")
        item=self.item(month="2026-10")
        self.assertEqual((item["status"],item["payout"]),("penalty_zero",0))
        self.assertEqual(after["zero_all"],[])
        self.assertEqual(before["payout"]-after["payout"],2000)

    def test_unknown_fines_wait_without_paying_and_confirmation_restores_scale(self):
        r = self.item()
        self.assertEqual((r["status"], r["payout"]), ("awaiting", 0))
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        self.assertEqual(self.item()["payout"], 2000)

    def test_confirmed_no_fines_does_not_replace_missing_receivables_data(self):
        self.con.execute("DELETE FROM receivables")
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        self.assertEqual(self.item()["payout"], 0)
        manual.add_penalty(self.con, "tender", "2026-11-05", 0, "Штраф", "boss")
        self.assertEqual(self.item()["status"], "penalty_zero")

    def test_any_fine_even_zero_zeroes_only_this_item(self):
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        before = self.engine.compute("tender", "2026-11")
        manual.add_penalty(self.con, "tender", "2026-11-05", 0, "Штраф", "boss")
        after = self.engine.compute("tender", "2026-11")
        r = self.item()
        self.assertEqual((r["payout"], r["pct"]), (0, 0))
        self.assertTrue(r["penalty_zero"])
        self.assertEqual(after["zero_all"], [])
        self.assertEqual(before["payout"] - after["payout"], 2000)

    def test_fine_month_and_employee_are_scoped_and_onec_fines_count(self):
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        manual.add_penalty(self.con, "pm", "2026-11-05", 1, "Чужой", "boss")
        manual.add_penalty(self.con, "tender", "2026-10-05", 1, "Прошлый", "boss")
        self.assertEqual(self.item()["payout"], 2000)
        replace_rows(self.con, "penalties", [{"id": "p", "emp": "tender", "dt": "2026-11-05", "amount": 1}])
        self.assertEqual(self.item()["payout"], 0)
        self.assertEqual(len(manual.penalties_block(self.con, "tender", "2026-11")["onec_rows"]), 1)

    def test_manual_deadline_changes_kpi_but_not_source_and_reset_restores_it(self):
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        manual.set_value(self.con, "receivables", "r1", "due_date", "2026-10-01", "boss")
        self.assertEqual(self.con.execute("SELECT due_date FROM receivables WHERE id='r1'").fetchone()[0], "2026-11-30")
        r = self.item()
        self.assertGreater(r["payout"], 0)
        self.assertLess(r["payout"], 2000)
        self.assertEqual(r["details"]["rows"][0][3], "2026-10-01")
        self.assertTrue(r["details"]["date_edit"]["rows"][0]["manual"])
        manual.set_value(self.con, "receivables", "r1", "due_date", "", "boss")
        self.assertEqual(self.item()["payout"], 2000)
        self.assertEqual(self.item()["details"]["rows"][0][3], "2026-11-30")

    def test_override_survives_sync_and_date_metadata_follows_sorted_rows(self):
        manual.set_value(self.con, "receivables", "r1", "due_date", "2026-10-01", "boss")
        replace_rows(self.con, "receivables", [{**self.row, "due_date": "2026-12-01"},
            {**self.row, "id": "r2", "doc": "Заказ-2", "due_date": "2026-09-01"}])
        manual.apply(self.con)
        r = self.item()
        self.assertEqual(r["details"]["rows"][1][3], "2026-10-01")
        self.assertEqual(r["details"]["date_edit"]["rows"][1]["ref"], "r1")
        self.assertEqual(r["details"]["date_edit"]["rows"][1]["source_value"], "2026-12-01")

    def test_only_valid_deadlines_are_editable(self):
        for field, value in (("amount", 9), ("due_date", "2026-02-30")):
            with self.assertRaises(ValueError):
                manual.set_value(self.con, "receivables", "r1", field, value, "boss")

    def test_partial_year_cannot_be_saved_as_a_business_payment_date(self):
        for field in ("paid_date", "due_date"):
            for value in ("0202-11-02", "0002-11-02"):
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    manual.set_value(self.con,"receivables","r1",field,value,"boss")
        self.assertIsNone(self.con.execute("SELECT 1 FROM manual_fields").fetchone())

    def test_manual_full_payment_stops_overdue_and_future_date_does_not_close_early(self):
        replace_rows(self.con, "receivables", [{**self.row, "due_date": "2026-11-01"}])
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        self.assertGreater(self.item()["payout"], 0)
        self.assertLess(self.item()["payout"], 2000)
        manual.set_value(self.con, "receivables", "r1", "paid_date", "2026-11-05", "boss")
        r = self.item()
        self.assertEqual(r["payout"], 2000)
        self.assertEqual(r["details"]["rows"][0][4], "2026-11-05")
        self.assertEqual(r["details"]["rows"][0][6], 0)
        self.assertIsNone(self.con.execute("SELECT paid_date FROM receivables").fetchone()[0])
        self.assertEqual(self.item(month="2026-12")["details"]["rows"], [])
        manual.set_value(self.con, "receivables", "r1", "paid_date", "2026-12-05", "boss")
        r = self.item()
        self.assertGreater(r["payout"], 0)
        self.assertLess(r["payout"], 2000)
        self.assertEqual(r["details"]["rows"][0][4], "не оплачено")
        self.assertEqual(r["details"]["paid_edit"]["rows"][0]["effective_value"], "2026-12-05")

    def test_manual_unpaid_survives_sync_and_reset_restores_source_payment(self):
        source = {**self.row, "due_date": "2026-11-01", "paid_date": "2026-11-05", "planned_amount": 1000,
                  "history": json.dumps([{"at": "2026-11-01", "ship_date": "2026-11-01", "amount": 1000, "paid_date": None},
                    {"at": "2026-11-05", "ship_date": "2026-11-01", "amount": 1000, "paid_date": "2026-11-05"}])}
        replace_rows(self.con, "receivables", [source])
        manual.set_penalty_check(self.con, "tender", "2026-11", True, "boss")
        manual.set_value(self.con, "receivables", "r1", "paid_date", "unpaid", "boss")
        replace_rows(self.con, "receivables", [source, {**self.row, "id": "r2", "due_date": "2026-09-01"}])
        manual.apply(self.con)
        r = self.item()
        row = next(row for row, meta in zip(r["details"]["rows"], r["details"]["paid_edit"]["rows"]) if meta["ref"] == "r1")
        self.assertEqual((row[4], row[6]), ("не оплачено", 1000))
        self.assertGreater(r["payout"], 0)
        self.assertLess(r["payout"], 2000)
        self.assertEqual(self.con.execute("SELECT paid_date FROM receivables WHERE id='r1'").fetchone()[0], "2026-11-05")
        manual.set_value(self.con, "receivables", "r1", "paid_date", "", "boss")
        r = self.item()
        row = next(row for row, meta in zip(r["details"]["rows"], r["details"]["paid_edit"]["rows"]) if meta["ref"] == "r1")
        self.assertEqual((row[4], row[6]), ("2026-11-05", 0))
        for invalid in ("paid", "неверная дата", "2026-02-30"):
            with self.assertRaises(ValueError):
                manual.set_value(self.con, "receivables", "r1", "paid_date", invalid, "boss")


class ReceivableDeadlineHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_kpi_activity_audit import KPIAuditHTTPTest
        KPIAuditHTTPTest.setUpClass.__func__(cls)
        replace_rows(cls.con, "receivables", [{"id": "r1", "emp": "tender", "doc": "Заказ-1", "client": "Тестовый клиент",
            "order_date": "2026-10-01", "ship_date": "2026-11-01", "due_date": "2026-11-30", "amount": 1000}])
        manual.set_penalty_check(cls.con, "tender", "2026-11", True, "boss")
        cls.con.commit()

    @classmethod
    def tearDownClass(cls):
        from tests.test_kpi_activity_audit import KPIAuditHTTPTest
        KPIAuditHTTPTest.tearDownClass.__func__(cls)

    def post(self, login, **changes):
        body = {"table": "receivables", "ref": "r1", "field": "due_date", "value": "2026-10-01", **changes}
        request = urllib.request.Request(self.base + "/api/manual", method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **({"Cookie": self.cookies[login]} if login else {})})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status

    def test_manager_only_date_edit_validation_and_source_reset(self):
        self.assertEqual(self.post(None), 401)
        self.assertEqual(self.post("tender"), 403)
        self.assertEqual(self.post("pm"), 403)
        for change in ({"field": "amount"}, {"value": "2026-02-30"}, {"ref": "missing"}):
            self.assertEqual(self.post("boss", **change), 400)
        self.assertEqual(self.post("boss"), 200)
        self.assertEqual(self.con.execute("SELECT value FROM manual_fields WHERE tbl='receivables'").fetchone()[0], "2026-10-01")
        self.assertEqual(self.con.execute("SELECT due_date FROM receivables").fetchone()[0], "2026-11-30")
        self.assertEqual(self.post("boss", value=""), 200)
        self.assertIsNone(self.con.execute("SELECT 1 FROM manual_fields WHERE tbl='receivables'").fetchone())
        self.assertEqual(self.con.execute("SELECT due_date FROM receivables").fetchone()[0], "2026-11-30")

    def test_payment_date_and_unpaid_require_manager_and_preserve_source(self):
        for login, expected in ((None, 401), ("tender", 403), ("pm", 403)):
            self.assertEqual(self.post(login, field="paid_date", value="unpaid"), expected)
        self.assertEqual(self.post("boss", field="paid_date", value="2026-02-30"), 400)
        self.assertEqual(self.post("boss", field="paid_date", value="0202-11-02"), 400)
        for value in ("2026-11-03", "unpaid"):
            self.assertEqual(self.post("boss", field="paid_date", value=value), 200)
            self.assertEqual(self.con.execute("SELECT value FROM manual_fields WHERE field='paid_date'").fetchone()[0], value)
            self.assertIsNone(self.con.execute("SELECT paid_date FROM receivables").fetchone()[0])
        self.assertEqual(self.post("boss", field="paid_date", value=""), 200)
        self.assertIsNone(self.con.execute("SELECT 1 FROM manual_fields WHERE field='paid_date'").fetchone())


if __name__ == "__main__":
    unittest.main()
