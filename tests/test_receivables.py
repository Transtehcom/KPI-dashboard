"""Тесты дебиторки по заказам клиентов из 1С. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import os
import datetime as dt
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.onec_receivables import control_rows, fetch_receivables, stage_rows, state_at  # noqa: E402


class StageRowsTest(unittest.TestCase):
    def test_control_keeps_unshipped_stages_without_debt(self):
        row = control_rows([("2026-12-01", 1000)], [])[0]
        self.assertEqual((row["planned_amount"], row["amount"], row["ship_date"]), (1000, 0, None))

    def test_state_at_excludes_later_shipments_and_payments(self):
        row = control_rows([("2026-10-10", 1000)], [
            ("2026-10-01", "Receipt", 600), ("2026-10-20", "Expense", 100),
            ("2026-11-03", "Receipt", 400), ("2026-11-10", "Expense", 900)])[0]
        oct_state = state_at(row, dt.date(2026, 10, 31))
        self.assertEqual((oct_state["amount"], oct_state["paid_date"]), (500, None))
        self.assertEqual(state_at(row, dt.date(2026, 9, 30))["amount"], 0)
        self.assertEqual(state_at(row, dt.date(2026, 11, 30))["paid_date"], "2026-11-10")

    def test_loader_keeps_old_orders_and_future_payment_dates(self):
        queries = {}

        class FakeOneC:
            def _odata(self, entity, params):
                queries[entity] = params
                if entity == "Document_ЗаказКлиента":
                    return [{"Ref_Key": "old", "Number": "З-1", "Date": "2020-01-01T00:00:00",
                             "ОбъектРасчетов_Key": "obj", "Менеджер": {"Description": "Мария"},
                             "ЭтапыГрафикаОплаты": [{"ДатаПлатежа": "2027-02-01T00:00:00", "СуммаПлатежа": 1000}]}]
                return []

        rows = fetch_receivables(FakeOneC(), {}, dt.date(2026, 10, 1), dt.date(2026, 11, 2))
        self.assertEqual((rows[0]["order_date"], rows[0]["due_date"]), ("2020-01-01", "2027-02-01"))
        self.assertNotIn("Date ge", queries["Document_ЗаказКлиента"]["$filter"])
        self.assertNotIn("Period ge", queries["AccumulationRegister_РасчетыСКлиентами_RecordType"]["$filter"])

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

    def test_partial_payment_reduces_open_stage(self):
        # заказ 00ЦБ-000060: 27,71% / 72,29%, отгружено всё, оплачено 8,5 + 5 + 0,783 млн — долг по карточке расчётов 16 099 069,84
        stages = [("2025-05-12", 8418931.41), ("2025-08-15", 21963354.43)]
        moves = [("2025-07-01", "Receipt", 30382285.84), ("2025-05-12", "Expense", 8500000.0),
                 ("2025-09-05", "Expense", 5000000.0), ("2025-09-19", "Expense", 783216.0)]
        rows = stage_rows(stages, moves)
        self.assertEqual([(r["amount"], r["paid_date"]) for r in rows], [(8418931.41, "2025-05-12"), (16099069.84, None)])

    def test_due_is_payment_date_from_order_rules(self):
        # срок оплаты — «Дата платежа» из правил оплаты как есть, без поправки на дату отгрузки
        rows = stage_rows([("2026-10-13", 100)], [("2026-07-14", "Receipt", 100)])
        self.assertEqual((rows[0]["due_date"], rows[0]["paid_date"]), ("2026-10-13", None))


if __name__ == "__main__":
    unittest.main()
