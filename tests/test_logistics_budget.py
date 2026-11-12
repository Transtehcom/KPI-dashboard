import datetime as dt
import unittest
from unittest.mock import patch

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.onec import OneC
from app.onec_logistics_delivery import fetch_customer_deliveries


class LogisticsBudgetTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), {'holidays': [], 'employees': [
            {'login': 'logist', 'position': 'logistics', 'name': 'Логист'}]})
        self.con.execute('INSERT INTO dataset_state VALUES(?,?,?,?)',
                         ('logistics_deliveries', '2026-11-01', '2026-11-30', '2026-11-12'))

    def add(self, identity, customer=None, expected=208196.72, actual=49323.72, status='ТТК_ВЗакуп'):
        self.con.execute('INSERT INTO logistics_deliveries '
            '(id,emp,dt,source_kind,source_key,source_doc,order_key,order_doc,order_status,'
            'expected_delivery_novat,actual_delivery_novat) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identity, 'logist', '2026-11-10', 'forwarder', identity, identity, customer or identity,
             customer or identity, status, expected, actual))

    def result(self):
        with patch('app.engine.today', return_value=dt.date(2026, 11, 12)):
            return self.engine.compute('logist', '2026-11')['items'][1]

    def test_customer_order_152_actual_mp_is_within_expected_mp(self):
        self.add('00ЦБ-000152')
        item = self.result()
        self.assertEqual(item['payout'], 2000)
        self.assertEqual(item['details']['rows'][0][2:5], [208196.72, 49323.72, 158873.0])
        self.assertEqual(item['details']['rows'][0][-1], 'Засчитан')
        self.assertEqual(item['details']['money_cols'], [2, 3, 4])

    def test_equal_cost_qualifies_and_excess_fails_without_vat_conversion(self):
        self.add('equal', expected=100, actual=100)
        self.add('excess', expected=100, actual=100.01)
        item = self.result()
        self.assertEqual(item['components'][0]['fact'], .5)
        self.assertEqual(item['payout'], 1000)
        self.assertEqual(item['details']['rows'][1][-1], 'Не засчитан')

    def test_unique_customer_orders_in_procurement_do_not_require_quote_basis(self):
        self.add('first', customer='same')
        self.add('second', customer='same')
        self.add('closed', status='Закрыт')
        item = self.result()
        self.assertEqual(len(item['details']['rows']), 1)
        self.assertEqual(item['payout'], 2000)

    def test_missing_actual_cannot_use_previous_transport_comparison_or_zero(self):
        self.add('missing', actual=None)
        self.con.execute("UPDATE logistics_deliveries SET delivery_novat=208196.72,comparison_amount=254000,"
                         "comparison_kind='transport',quote_key='quote'")
        item = self.result()
        self.assertIsNone(item['components'][0]['fact'])
        self.assertEqual(item['payout'], 0)
        self.assertIn('Фактическая МП', item['details']['rows'][0][-1])

    def test_zero_is_valid_and_missing_expected_or_coverage_waits(self):
        self.add('zero', expected=0, actual=0)
        self.assertEqual(self.result()['payout'], 2000)
        self.add('missing', expected=None)
        self.assertIsNone(self.result()['components'][0]['fact'])
        self.con.execute('DELETE FROM logistics_deliveries WHERE id=?', ('missing',))
        self.con.execute('DELETE FROM dataset_state')
        self.assertIsNone(self.result()['components'][0]['fact'])

    def test_imported_mp_amounts_are_normalized(self):
        self.add('imported')
        raw = dict(self.con.execute('SELECT * FROM logistics_deliveries').fetchone())
        raw.update({'manager': 'Логист', 'expected_delivery_novat': '208 196,72',
                    'actual_delivery_novat': '49 323,72'})
        source = OneC({'datasets': {'logistics_deliveries': {'source': 'http', 'path': '/unused'}}})
        with patch.object(source, '_load', return_value=[raw]):
            rows = source.fetch(self.con, {'логист': 'logist'}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))['logistics_deliveries']
        replace_rows(self.con, 'logistics_deliveries', rows)
        self.assertEqual(self.result()['payout'], 2000)

    def test_odata_uses_order_expected_mp_and_marks_unpublished_actual_unknown(self):
        customer = {'Ref_Key': 'order', 'Number': '00ЦБ-000152', 'Date': '2026-10-08',
                    'Статус': 'ТТК_ВЗакуп', 'ТТК_СуммаДоставкиБезНДС': 208196.72}
        data = {'Document_ЗаказКлиента': [customer],
                'Document_КоммерческоеПредложениеКлиенту': [],
                'Document_ТТК_ЗаявкаВОтделСнабжения': [], 'Document_ЗаказПоставщику': [],
                'Document_ПоручениеЭкспедитору': [{'Ref_Key': 'forwarder', 'Number': 'PE', 'Date': '2026-11-10',
                     'ТТК_ЗаказКлиента_Key': 'order', 'ТТК_СуммаОстаток': 300000}],
                'Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов': []}

        class Source:
            def _odata(self, entity, params):
                if entity == 'Document_ЗаказКлиента':
                    assert 'ТТК_СуммаДоставкиБезНДС' in params['$select']
                return data[entity]

        rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 12))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['expected_delivery_novat'], 208196.72)
        self.assertIsNone(rows[0]['actual_delivery_novat'])
