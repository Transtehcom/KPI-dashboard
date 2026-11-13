import datetime as dt
import unittest
from unittest.mock import patch

from app.common import connect, load_rules
from app.engine import Engine
from app.onec_logistics_delivery import fetch_customer_deliveries


class LogisticsDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), {'holidays': [], 'employees': [
            {'login': 'logist', 'position': 'logistics', 'name': 'Логист'}]})
        self.con.execute('INSERT INTO dataset_state VALUES(?,?,?,?)',
                         ('logistics_deliveries', '2026-11-01', '2026-11-30', '2026-11-12'))

    def add(self, key, planned='2026-11-10', actual='2026-11-10', source='forwarder', customer=None, day='2026-11-05'):
        self.con.execute('INSERT INTO logistics_deliveries(id,emp,dt,source_kind,source_key,source_doc,order_key,order_doc,planned_date,actual_date) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (key, 'logist', day, source, key, key, customer or key, customer or key, planned, actual))

    def result(self):
        with patch('app.engine.today', return_value=dt.date(2026, 11, 12)):
            result = self.engine.compute('logist', '2026-11')
        return result, result['items'][0]

    def test_98_percent_pays_full_and_49_percent_pays_half(self):
        for good, expected in ((98, 2000), (49, 1000), (100, 2000), (0, 0)):
            with self.subTest(good=good):
                self.con.execute('DELETE FROM logistics_deliveries')
                for index in range(100):
                    self.add(str(index), actual='2026-11-10' if index < good else '2026-11-11')
                report, item = self.result()
                self.assertFalse(report['draft'])
                self.assertEqual(item['components'][0]['fact'], good / 100)
                self.assertEqual(item['payout'], expected)
                self.assertEqual(report['payout'], expected)
                self.assertEqual(report['items'][1]['payout'], 0)
                self.assertTrue(all(report['items'][n - 1]['status'] == 'draft' and report['items'][n - 1]['payout'] == 0
                                    for n in (3, 5, 6)))
                self.assertEqual(report['items'][3]['status'], 'awaiting')

    def test_one_customer_counts_once_across_documents_and_uses_document_month(self):
        self.add('first', customer='customer')
        self.add('second', customer='customer', source='transport')
        self.add('old', day='2026-10-31')
        _, item = self.result()
        self.assertEqual(len(item['details']['rows']), 1)
        self.assertEqual(item['components'][0]['fact'], 1)
        self.assertIn('first', item['details']['rows'][0][2])
        self.assertIn('second', item['details']['rows'][0][2])

    def test_empty_actual_is_overdue_only_after_deadline(self):
        self.add('future', planned='2026-11-20', actual=None)
        self.add('today', planned='2026-11-12', actual=None)
        self.add('late', planned='2026-11-11', actual=None)
        self.add('early', actual='2026-11-09')
        _, item = self.result()
        self.assertEqual(item['components'][0]['fact'], .75)
        rows = {row[0]: row for row in item['details']['rows']}
        self.assertEqual(rows['late'][-2], '-1 дн.')
        self.assertEqual(rows['early'][-2], '+1 дн.')
        self.assertIn('Ожидает', rows['today'][-1])

    def test_missing_deadline_or_coverage_does_not_create_full_payout(self):
        self.add('missing', planned=None)
        _, item = self.result()
        self.assertIsNone(item['components'][0]['fact'])
        self.assertEqual(item['payout'], 0)
        self.con.execute('DELETE FROM logistics_deliveries')
        self.add('good')
        self.con.execute('DELETE FROM dataset_state')
        _, item = self.result()
        self.assertIsNone(item['components'][0]['fact'])
        self.assertEqual(item['payout'], 0)

    def test_source_follows_explicit_bases_and_excludes_deleted_documents(self):
        customer = {'Ref_Key': 'customer', 'Number': 'ORDER', 'Date': '2026-09-01',
                    'ДокументОснование': 'kp', 'ДокументОснование_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту',
                    'ТТК_КонечнаяДатаПоставки': '2026-11-10T00:00:00', 'ТТК_ФактическаяДатаПоставки': '2026-11-09T00:00:00'}
        data = {'Document_ЗаказКлиента': [customer], 'Document_КоммерческоеПредложениеКлиенту': [],
                'AccumulationRegister_РасчетыСКлиентами_RecordType': [],
                'Document_ТТК_ЗаявкаВОтделСнабжения': [{'Ref_Key': 'supply', 'ДокументОснования': 'kp',
                    'ДокументОснования_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту'}],
                'Document_ЗаказПоставщику': [],
                'Document_ПоручениеЭкспедитору': [
                    {'Ref_Key': 'forwarder', 'Number': 'F1', 'Date': '2026-11-03', 'ТТК_ЗаказКлиента_Key': 'customer'},
                    {'Ref_Key': 'deleted', 'Number': 'F2', 'Date': '2026-11-03', 'DeletionMark': True, 'ТТК_ЗаказКлиента_Key': 'customer'}],
                'Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов': [
                    {'Ref_Key': 'transport', 'Number': 'T1', 'Date': '2026-11-02', 'ДокументОснование2': 'supply',
                     'ДокументОснование2_Type': 'StandardODATA.Document_ТТК_ЗаявкаВОтделСнабжения'}]}
        class Source:
            def _odata(self, entity, params):
                return data[entity]
        rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['order_key'] for row in rows}, {'customer'})
        self.assertEqual(rows[0]['planned_date'], '2026-11-10')
        self.assertEqual(rows[0]['actual_date'], '2026-11-09')

    def test_explicit_customer_or_supply_basis_does_not_add_sibling_orders(self):
        customers = [{'Ref_Key': key, 'Number': key, 'Date': '2026-10-01', 'ДокументОснование': 'shared-kp',
                      'ДокументОснование_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту'} for key in ('A', 'B')]
        data = {'Document_ЗаказКлиента': customers, 'Document_КоммерческоеПредложениеКлиенту': [],
                'AccumulationRegister_РасчетыСКлиентами_RecordType': [],
                'Document_ТТК_ЗаявкаВОтделСнабжения': [{'Ref_Key': 'supply', 'ДокументОснования': 'shared-kp',
                    'ДокументОснования_Type': 'StandardODATA.Document_КоммерческоеПредложениеКлиенту'}],
                'Document_ЗаказПоставщику': [{'Ref_Key': 'supplier', 'ТТК_ЗаказКлиента_Key': 'A', 'ДокументОснование': 'supply'}],
                'Document_ПоручениеЭкспедитору': []}
        class Source:
            def _odata(self, entity, params):
                return data[entity]
        for ref, kind in (('A', 'ЗаказКлиента'), ('supply', 'ТТК_ЗаявкаВОтделСнабжения')):
            with self.subTest(kind=kind):
                data['Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов'] = [
                    {'Ref_Key': 'transport', 'Number': 'T1', 'Date': '2026-11-02',
                     'ДокументОснование': ref, 'ДокументОснование_Type': 'StandardODATA.Document_' + kind}]
                rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
                self.assertEqual([row['order_key'] for row in rows], ['A'])
        data['Document_ЗаказПоставщику'] = []
        data['Document_ТТК_ЗаявкаВОтделСнабжения'][0].update({
            'ДокументОснования': 'A', 'ДокументОснования_Type': 'StandardODATA.Document_ЗаказКлиента'})
        rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        self.assertEqual([row['order_key'] for row in rows], ['A'], 'traversal stops once the specific customer is resolved')
