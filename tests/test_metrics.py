"""Тесты раздела «Метрики». Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import metrics  # noqa: E402
from app.common import connect, replace_rows  # noqa: E402

TODAY = dt.date(2026, 10, 22)
NOW = "2026-10-22T12:00:00"


def req(i, status, created, closed=None, due=None, order_dt=None, customer_order=None, emp="sup"):
    return {"id": f"r{i}", "emp": emp, "dt": created[:10], "doc": f"З-{i}", "status": status, "created_at": created,
            "closed_at": closed, "due_at": due, "order_dt": order_dt, "customer_order": customer_order,
            "stage": "open"}


def order(i, placed, plan=None, fact=None, customer_order=None, emp="sup"):
    return {"id": f"o{i}", "emp": emp, "doc": f"ЗП-{i}", "dt": placed, "supplier": "Завод", "amount_novat": 1000.0,
            "planned_date": plan, "actual_date": fact, "complete": 1, "customer_order": customer_order}


class SupplyMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.tmp.name, "kpi.sqlite"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def run_supply(self, reqs, orders, date_from="2026-10-01", date_to="2026-10-31"):
        replace_rows(self.con, "supply_funnel", reqs)
        replace_rows(self.con, "supplier_orders", orders)
        return metrics.supply(self.con, {"sup": "Снабженец"}, date_from, date_to, today=TODAY, now=NOW)

    def stage(self, res, key):
        return next(s for s in res["stages"] if s["key"] == key)

    def test_open_request_is_on_calc_and_overdue_by_deadline(self):
        res = self.run_supply([req(1, "ВРаботе", "2026-10-10T09:00:00", due="2026-10-20T18:00:00"),
                               req(2, "Поступила", "2026-10-21T09:00:00", due="2026-10-24T18:00:00")], [])
        calc = self.stage(res, "calc")
        self.assertEqual((calc["now"], calc["came"], calc["left"], calc["overdue"], calc["max_days"]), (2, 2, 0, 1, 12))
        self.assertEqual([r["late_days"] for r in res["requests"]], [2, 0])
        self.assertEqual(res["tiles"]["overdue"], 1)

    def test_request_moves_through_handed_and_approve_one_stage_at_a_time(self):
        res = self.run_supply([req(1, "Отработана", "2026-10-01T09:00:00", closed="2026-10-03T10:00:00",
                                   order_dt="2026-10-06", customer_order="ЗК-1")], [])
        self.assertEqual([self.stage(res, k)["now"] for k in ("calc", "handed", "approve")], [0, 0, 1])
        self.assertEqual(self.stage(res, "handed")["left"], 1)
        self.assertEqual(res["tiles"]["done_calc"], 1)
        # появился заказ поставщику — заявка уходит с «Закупка согласуется», дальше идёт заказ
        res = self.run_supply([req(1, "Отработана", "2026-10-01T09:00:00", closed="2026-10-03T10:00:00",
                                   order_dt="2026-10-06", customer_order="ЗК-1")],
                              [order(1, "2026-10-08", plan="2026-12-01", customer_order="ЗК-1")])
        self.assertEqual([self.stage(res, k)["now"] for k in ("approve", "placed", "shipping")], [0, 1, 0])
        self.assertEqual(res["tiles"]["in_work"], 1)

    def test_handed_request_without_order_leaves_queue_after_window(self):
        res = self.run_supply([req(1, "Отработана", "2026-08-01T09:00:00", closed="2026-08-02T10:00:00")], [])
        self.assertEqual(self.stage(res, "handed")["now"], 0)
        self.assertEqual(res["tiles"]["in_work"], 0)

    def test_cancelled_request_is_not_in_queue(self):
        res = self.run_supply([req(1, "Отменена", "2026-10-01T09:00:00", closed="2026-10-02T10:00:00")], [])
        self.assertEqual(res["tiles"]["in_work"], 0)
        self.assertEqual(self.stage(res, "calc")["left"], 1)

    def test_supplier_order_stages_by_planned_date(self):
        res = self.run_supply([], [order(1, "2026-10-01", plan="2026-11-30"),          # далеко — размещён
                                   order(2, "2026-10-01", plan="2026-10-25"),          # скоро — ожидается отгрузка
                                   order(3, "2026-10-01", plan="2026-10-15"),          # срок прошёл — просрочен
                                   order(4, "2026-10-01", plan="2026-10-15", fact="2026-10-14"),  # поставлен
                                   order(5, "2026-10-01")])                            # без даты — размещён
        self.assertEqual([self.stage(res, k)["now"] for k in ("placed", "shipping")], [2, 2])
        self.assertEqual(self.stage(res, "shipping")["overdue"], 1)
        self.assertEqual(res["tiles"]["done_orders"], 1)
        late = {o["doc"]: o["late_days"] for o in res["orders"]}
        self.assertEqual((late["ЗП-3"], late["ЗП-4"], late["ЗП-5"]), (7, 0, None))

    def test_unmapped_responsible_shows_1c_name(self):
        res = self.run_supply([req(1, "ВРаботе", "2026-10-20T09:00:00", emp="1c:Момот Руслан")], [])
        self.assertEqual([p["name"] for p in res["people"]], ["Момот Руслан"])

    def test_old_rows_without_status_use_funnel_stage(self):
        r = req(1, None, "2026-10-20T09:00:00")
        r.update(stage="cancelled", created_at=None)
        res = self.run_supply([r], [])
        self.assertEqual(res["tiles"]["in_work"], 0)


class LogisticsMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.tmp.name, "kpi.sqlite"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_all_transport_requests_in_period_any_status(self):
        rows = [{"id": "t1", "emp": "1c:Ваганова Наталия", "dt": "2026-10-20", "created_at": "2026-10-20T14:07:00",
                 "doc": "000002338", "status": "Отработана", "posted": 1, "deleted": 0},
                {"id": "t2", "emp": "1c:", "dt": "2026-10-21", "created_at": "2026-10-21T14:18:21", "doc": "000002345",
                 "status": "ВРаботе", "posted": 0, "deleted": 0},
                {"id": "t3", "emp": "1c:Якимова Алёна", "dt": "2026-10-21", "created_at": "2026-10-21T15:00:00",
                 "doc": "000002341", "status": "Отработана", "posted": 1, "deleted": 1},
                {"id": "t4", "emp": "1c:Якимова Алёна", "dt": "2026-09-30", "doc": "000002200", "status": "Отработана"}]
        replace_rows(self.con, "transport_requests", rows)
        r = metrics.logistics(self.con, {}, "2026-10-01", "2026-10-31")
        self.assertEqual(r["tiles"], {"total": 3, "deleted": 1, "on_calc": 1})  # сентябрьская не в периоде, удалённая считается
        on_calc = next(x for x in r["rows"] if x["key"] == "on_calc")
        self.assertEqual((on_calc["total"], on_calc["statuses"]), (1, {"В работе": 1}))
        self.assertEqual(dict(r["by_status"]), {"Отработана": 2, "В работе": 1})
        self.assertEqual([x["logist"] for x in r["requests"]], ["Якимова Алёна", "Не назначен", "Ваганова Наталия"])
        self.assertTrue(r["available"])
        self.assertEqual(r["statuses"][:4], ["Поступила", "В работе", "Запрос информации", "Отработана"])
        self.assertEqual([(p["name"], p["statuses"], p["total"]) for p in r["people"]],
                         [("Ваганова Наталия", {"Отработана": 1}, 1), ("Якимова Алёна", {"Отработана": 1}, 1),
                          ("Не назначен", {"В работе": 1}, 1)])
        self.assertEqual(r["people"][2]["metrics"], {"total": 1, "on_calc": 1})


if __name__ == "__main__":
    unittest.main()
