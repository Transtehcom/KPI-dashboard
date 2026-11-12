import datetime as dt
import unittest
from unittest.mock import patch

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.onec import OneC
from app.onec_logistics_delivery import fetch_customer_deliveries, transport_amount


class LogisticsBudgetTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), {'holidays': [], 'employees': [
            {'login': 'logist', 'position': 'logistics', 'name': 'Логист'}]})
        self.con.execute('INSERT INTO dataset_state VALUES(?,?,?,?)',
                         ('logistics_deliveries', '2026-11-01', '2026-11-30', '2026-11-12'))

    def add(self, identity, customer=None, budget=208196.72, amount=254000, status='ТТК_ВЗакуп', quote='quote'):
        self.con.execute('INSERT INTO logistics_deliveries '
            '(id,emp,dt,source_kind,source_key,source_doc,order_key,order_doc,order_status,quote_key,quote_doc,quote_date,'
            'delivery_novat,comparison_kind,comparison_doc,comparison_date,comparison_amount) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity, 'logist', '2026-11-10', 'forwarder', identity, identity, customer or identity, customer or identity,
             status, quote, '00ЦБ-000604', '2026-07-29', budget, 'transport', '000001973', '2026-08-04', amount))

    def result(self):
        with patch('app.engine.today', return_value=dt.date(2026, 11, 12)):
            return self.engine.compute('logist', '2026-11')['items'][1]

    def test_vat_rounding_and_confirmed_comparison_direction(self):
        self.add('equal')
        item = self.result()
        self.assertEqual(item['payout'], 2000)
        self.assertEqual(item['details']['rows'][0][4], 254000)
        self.assertEqual(item['details']['rows'][0][7], 0)
        self.add('smaller-source', amount=200000)
        item = self.result()
        self.assertEqual(item['components'][0]['fact'], .5)
        self.assertEqual(item['payout'], 1000)
        self.assertEqual(item['details']['rows'][1][-1], 'Не засчитан')
        self.assertEqual(item['details']['money_cols'], [3, 4, 6, 7])

    def test_order_counts_once_and_only_with_required_status_and_quote(self):
        self.add('first', customer='same')
        self.add('second', customer='same')
        self.add('closed', status='Закрыт')
        self.add('no-quote', quote=None)
        item = self.result()
        self.assertEqual(len(item['details']['rows']), 1)
        self.assertEqual(item['payout'], 2000)

    def test_zero_is_a_value_and_missing_amount_waits_for_data(self):
        self.add('zero', budget=0, amount=0)
        self.assertEqual(self.result()['payout'], 2000)
        self.add('missing', amount=None)
        item = self.result()
        self.assertIsNone(item['components'][0]['fact'])
        self.assertEqual(item['payout'], 0)
        self.assertEqual(item['extra'][-1]['value'], 1)

    def test_selected_transport_rows_or_all_unselected_rows_are_summed(self):
        lines = [{'Флаг': True, 'СтоимостьСНДС': 150000}, {'Флаг': True, 'СтоимостьСНДС': 35000},
                 {'Флаг': True, 'СтоимостьСНДС': 5000}, {'Флаг': True, 'СтоимостьСНДС': 50000},
                 {'Флаг': True, 'СтоимостьСНДС': 14000}, {'Флаг': False, 'СтоимостьСНДС': 999999}]
        self.assertEqual(transport_amount(lines), (254000, None))
        for line in lines:
            line['Флаг'] = False
        self.assertEqual(transport_amount(lines), (1253999, None))
        self.assertIsNone(transport_amount([{'СтоимостьСНДС': None}])[0])

    def test_imported_and_legacy_document_dates_are_supported(self):
        self.add('imported')
        raw = dict(self.con.execute('SELECT * FROM logistics_deliveries').fetchone())
        raw.update({'manager': 'Логист', 'quote_date': '2026-07-29T00:00:00', 'comparison_date': '04.08.2026'})
        source = OneC({'datasets': {'logistics_deliveries': {'source': 'http', 'path': '/unused'}}})
        with patch.object(source, '_load', return_value=[raw]):
            rows = source.fetch(self.con, {'логист': 'logist'}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))['logistics_deliveries']
        self.assertEqual(rows[0]['quote_date'], '2026-07-29')
        self.assertEqual(rows[0]['comparison_date'], '2026-08-04')
        replace_rows(self.con, 'logistics_deliveries', rows)
        self.assertEqual(self.result()['payout'], 2000)
        self.con.execute("UPDATE logistics_deliveries SET quote_date='2026-07-29T00:00:00',comparison_date='04.08.2026'")
        item = self.result()
        self.assertIn('29-07-2026', item['details']['rows'][0][2])
        self.assertIn('04-08-2026', item['details']['rows'][0][5])

    def test_transport_priority_uses_older_request_and_no_fallback_for_unfilled_request(self):
        customer = {'Ref_Key': 'order', 'Number': 'ORDER', 'Date': '2026-10-08', 'Статус': 'ТТК_ВЗакуп',
                    'ДокументОснование': 'quote', 'ДокументОснование_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту'}
        transport = {'Ref_Key': 'transport', 'Number': '000001973', 'Date': '2026-08-04',
                     'ДокументОснование': 'quote', 'ДокументОснование_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту',
                     'РезультатыРасчета': [{'СтоимостьСНДС': 254000}]}
        data = {'Document_ЗаказКлиента': [customer],
                'Document_КоммерческоеПредложениеКлиенту': [{'Ref_Key': 'quote', 'Number': '00ЦБ-000604',
                     'Date': '2026-07-29', 'ТТК_СтоимостьДоставки': 208196.72}],
                'Document_ТТК_ЗаявкаВОтделСнабжения': [], 'Document_ЗаказПоставщику': [],
                'Document_ПоручениеЭкспедитору': [{'Ref_Key': 'forwarder', 'Number': 'PE', 'Date': '2026-11-10',
                     'ТТК_ЗаказКлиента_Key': 'order', 'ТТК_СуммаОстаток': 300000}],
                'Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов': [transport]}
        class Source:
            def _odata(self, entity, params):
                return data[entity]
        def fetch():
            return fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 12))
        rows = fetch()
        self.assertEqual(len(rows), 1, 'older transport requests do not change the November cohort')
        self.assertEqual(rows[0]['comparison_kind'], 'transport')
        self.assertEqual(rows[0]['comparison_amount'], 254000)
        self.assertEqual(rows[0]['delivery_novat'], 208196.72)
        transport['РезультатыРасчета'] = []
        row = fetch()[0]
        self.assertEqual(row['comparison_kind'], 'transport')
        self.assertIsNone(row['comparison_amount'])
        self.assertTrue(row['comparison_issue'])
        data['Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов'] = []
        row = fetch()[0]
        self.assertEqual(row['comparison_kind'], 'forwarder')
        self.assertEqual(row['comparison_amount'], 300000)

