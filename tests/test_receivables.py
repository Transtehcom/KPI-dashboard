"""Тесты дебиторки по заказам клиентов из 1С. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.onec_receivables import stage_rows  # noqa: E402


class StageRowsTest(unittest.TestCase):
    def test_paid_in_full_after_shipment(self):
        # заказ 00ЦБ-000098: отгрузка 14.07, срок по правилам оплаты 13.10, оплата 21.10
        rows = stage_rows([("2026-10-13", 50153.16)], [("2026-07-14", "Receipt", 50153.16), ("2026-10-21", "Expense", 50153.16)])
        self.assertEqual(rows, [{"n": 1, "amount": 50153.16, "ship_date": "2026-07-14", "due_date": "2026-10-13",
                                 "paid_date": "2026-10-21"}])

    def test_not_shipped_means_no_debt(self):
        self.assertEqual(stage_rows([("2027-01-19", 36681.5)], []), [])

    def test_partial_shipment_limits_debt_and_unpaid_stays_open(self):
        rows = stage_rows([("2026-10-01", 1000)], [("2026-09-01", "Receipt", 550)])
        self.assertEqual((rows[0]["amount"], rows[0]["paid_date"]), (550, None))

    def test_prepayment_and_credit_stages(self):
        stages = [("2026-09-01", 200), ("2026-10-15", 800)]  # 20% до отгрузки, 80% после
        moves = [("2026-08-25", "Expense", 200), ("2026-09-05", "Receipt", 1000), ("2026-10-20", "Expense", 800)]
        rows = stage_rows(stages, moves)
        self.assertEqual([(r["amount"], r["due_date"], r["paid_date"]) for r in rows],
                         [(200, "2026-09-01", "2026-08-25"), (800, "2026-10-15", "2026-10-20")])

    def test_due_is_payment_date_from_order_rules(self):
        # срок оплаты — «Дата платежа» из правил оплаты как есть, без поправки на дату отгрузки
        rows = stage_rows([("2026-10-13", 100)], [("2026-07-14", "Receipt", 100)])
        self.assertEqual((rows[0]["due_date"], rows[0]["paid_date"]), ("2026-10-13", None))


if __name__ == "__main__":
    unittest.main()
