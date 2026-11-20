import unittest

from app import operations_views as views


class PreviousPriceTest(unittest.TestCase):
    def test_previous_completed_request_and_unverified_vat(self):
        item = dict(item_id='i', unit_id='u', selected_supplier_id='s', selected_price=100,
                    currency='RUB', vat_included=False)
        current = dict(kind='supply', id='db:supply:3', closed_at='2026-11-03', status='Отработана', items=[item])
        previous = dict(current, id='db:supply:1', number='Т-1', closed_at='2026-11-01',
                        items=[dict(item, selected_price=90)])
        cancelled = dict(previous, status='Отменена', closed_at='2026-11-02')
        views.compare_supply_prices(current, [previous, cancelled, current])
        comparison = item['previous_price']
        self.assertEqual((comparison['number'], comparison['delta']), ('Т-1', 10))
        self.assertAlmostEqual(comparison['percent'], 100 / 9)
        item['vat_included'] = None
        views.compare_supply_prices(current, [previous])
        self.assertIsNone(item['previous_price']['delta'])
