import datetime as dt
import unittest
from unittest import mock

from app.engine import Engine


class PayoutYearTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)

    def test_past_year_contains_twelve_months(self):
        with mock.patch('app.engine.today', return_value=dt.date(2026, 11, 12)), \
             mock.patch.object(self.engine, 'summary', side_effect=lambda login, month: {'month': month, 'payout': 500, 'share': .05}) as summary:
            rows = self.engine.year_trend('supply', '2025')
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]['month'], '2025-01')
        self.assertEqual(rows[-1]['month'], '2025-12')
        self.assertEqual(summary.call_count, 12)

    def test_future_months_have_no_payout(self):
        with mock.patch('app.engine.today', return_value=dt.date(2026, 11, 12)), \
             mock.patch.object(self.engine, 'summary', return_value={'payout': 500, 'share': .05}) as summary:
            rows = self.engine.year_trend('supply', '2026')
            self.assertEqual(summary.call_count, 11)
            self.assertEqual(rows[-1], {'month': '2026-12', 'payout': None, 'share': None, 'future': True})
            summary.reset_mock()
            future = self.engine.year_trend('supply', '2027')
            summary.assert_not_called()
            self.assertEqual(len(future), 12)

