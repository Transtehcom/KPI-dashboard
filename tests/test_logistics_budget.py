import datetime as dt
import json
import unittest
from unittest.mock import patch

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.onec import OneC
from app.onec_logistics_delivery import delivery_costs, fetch_customer_deliveries


class LogisticsBudgetTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), {'holidays': [], 'employees': [
            {'login': 'logist', 'position': 'logistics', 'name': 'Логист'}]})
        self.con.execute('INSERT INTO dataset_state VALUES(?,?,?,?)',
                         ('logistics_deliveries', '2026-11-01', '2026-11-30', '2026-11-12'))

    def add(self, identity, customer=None, expected=254000, actual=60174.94, status='ТТК_ВЗакуп'):
        self.con.execute('INSERT INTO logistics_deliveries '
            '(id,emp,dt,source_kind,source_key,source_doc,order_key,order_doc,order_status,'
            'expected_delivery_vat,actual_delivery_vat) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identity, 'logist', '2026-11-10', 'forwarder', identity, identity, customer or identity,
             customer or identity, status, expected, actual))

    def result(self):
        with patch('app.engine.today', return_value=dt.date(2026, 11, 12)):
            return self.engine.compute('logist', '2026-11')['items'][1]

    def test_customer_order_152_actual_mp_is_within_expected_mp(self):
        self.add('00ЦБ-000152')
        item = self.result()
        self.assertEqual(item['payout'], 2000)
        self.assertEqual(item['details']['rows'][0][2:5], [254000, 60174.94, 193825.06])
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
        raw.update({'manager': 'Логист', 'expected_delivery_vat': '254 000,00',
                    'actual_delivery_vat': '60 174,94'})
        source = OneC({'datasets': {'logistics_deliveries': {'source': 'http', 'path': '/unused'}}})
        with patch.object(source, '_load', return_value=[raw]):
            rows = source.fetch(self.con, {'логист': 'logist'}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))['logistics_deliveries']
        replace_rows(self.con, 'logistics_deliveries', rows)
        self.assertEqual(self.result()['payout'], 2000)

    def test_malformed_imported_audit_does_not_break_employee_report(self):
        self.add('imported')
        for audit in ('not-json', '{}', '[null]', '[{"included": true, "amount": "bad"}]'):
            with self.subTest(audit=audit):
                self.con.execute('UPDATE logistics_deliveries SET delivery_cost_sources=?', (audit,))
                item = self.result()
                self.assertEqual(item['payout'], 2000)

    def test_odata_reconstructs_actual_mp_from_supplier_settlement_objects(self):
        customer = {'Ref_Key': 'order', 'Number': '00ЦБ-000152', 'Date': '2026-10-08',
                    'Статус': 'ТТК_ВЗакуп', 'ТТК_СуммаДоставки': 254000}
        data = {'Document_ЗаказКлиента': [customer],
                'Document_КоммерческоеПредложениеКлиенту': [],
                'Document_ТТК_ЗаявкаВОтделСнабжения': [], 'Document_ЗаказПоставщику': [],
                'Document_ПоручениеЭкспедитору': [{'Ref_Key': 'forwarder', 'Number': 'PE', 'Date': '2026-11-10',
                     'ТТК_ЗаказКлиента_Key': 'order', 'ТТК_СуммаОстаток': 300000}],
                'Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов': [], 'Catalog_ОбъектыРасчетов': [],
                'Catalog_Номенклатура': [{'Ref_Key': 'transport'}]}

        for number, amount in (('749', 46632), ('750', 6974), ('757', 6568.94), ('751', 30000), ('goods', 1598822.85)):
            data['Document_ЗаказПоставщику'].append({
                'Ref_Key': number, 'Number': number, 'ТТК_ЗаказКлиента_Key': 'order',
                'ДокументОснование_Type': 'StandardODATA.Document_' + ('ЗаказКлиента' if number == 'goods' else 'ПоручениеЭкспедитору'),
                'ДокументОснование': 'forwarder', 'ОбъектРасчетов_Key': 'object-' + number,
                'СуммаДокумента': amount,
                'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'goods' if number == 'goods' else 'transport'}]})
            data['Catalog_ОбъектыРасчетов'].append({
                'Ref_Key': 'object-' + number, 'Объект': 'contract' if number == '751' else number,
                'Объект_Type': 'StandardODATA.Catalog_ДоговорыКонтрагентов' if number == '751' else 'StandardODATA.Document_ЗаказПоставщику',
                'Сумма': 900000 if number == '751' else amount})
        data['Document_ПоручениеЭкспедитору'].append({
            'Ref_Key': 'second-forwarder', 'Number': 'PE2', 'Date': '2026-11-11', 'ТТК_ЗаказКлиента_Key': 'order'})

        class Source:
            def _odata(self, entity, params):
                if entity == 'Document_ЗаказКлиента':
                    assert 'ТТК_СуммаДоставки' in params['$select']
                return data[entity]

        rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 12))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['expected_delivery_vat'], 254000)
        self.assertEqual(rows[0]['actual_delivery_vat'], 60174.94)
        self.assertEqual(rows[1]['actual_delivery_vat'], 60174.94)
        sources = json.loads(rows[0]['delivery_cost_sources'])
        self.assertEqual(len(sources), 4)
        self.assertEqual(next(r for r in sources if r['doc'] == '751')['included'], False)

        data['Catalog_ОбъектыРасчетов'] = []
        rows = fetch_customer_deliveries(Source(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 12))
        self.assertIsNone(rows[0]['actual_delivery_vat'])
        self.assertTrue(rows[0]['delivery_cost_issue'])

    def test_contract_costs_and_deleted_transport_orders_match_zero_in_form(self):
        suppliers = [
            {'Ref_Key': 'contract-order', 'Number': '753', 'ТТК_ЗаказКлиента_Key': 'customer',
             'ДокументОснование_Type': 'StandardODATA.Document_ПоручениеЭкспедитору', 'ОбъектРасчетов_Key': 'contract',
             'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'transport'}]},
            {'Ref_Key': 'deleted-order', 'DeletionMark': True, 'ТТК_ЗаказКлиента_Key': 'customer',
             'ДокументОснование_Type': 'StandardODATA.Document_ПоручениеЭкспедитору'}]

        class Source:
            def _odata(self, entity, params):
                if entity == 'Catalog_Номенклатура':
                    return [{'Ref_Key': 'transport'}]
                return [{'Ref_Key': 'contract', 'Объект_Type': 'StandardODATA.Catalog_ДоговорыКонтрагентов',
                         'Объект': 'contract-ref', 'Сумма': 2097.18}]

        cost = delivery_costs(Source(), suppliers, ['customer'])['customer']
        self.assertEqual(cost['amount'], 0)
        self.assertIsNone(cost['issue'])
        self.assertEqual(len(cost['sources']), 1)
        self.assertIn('договору', cost['sources'][0]['reason'])

    def test_customer_order_133_includes_carrier_order_created_without_forwarder(self):
        suppliers = [
            {'Ref_Key': number, 'Number': number, 'ТТК_ЗаказКлиента_Key': 'customer',
             'ДокументОснование_Type': 'StandardODATA.Document_' + basis,
             'Партнер': {'Перевозчик': carrier}, 'ОбъектРасчетов_Key': 'obj-' + number,
             'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'goods' if number == '758' else 'transport'}]}
            for number, basis, carrier in [('688', 'ЗаказКлиента', True),
                                           ('713', 'ПоручениеЭкспедитору', True),
                                           ('731', 'ПоручениеЭкспедитору', True),
                                           ('758', 'ЗаказКлиента', False)]]

        class Source:
            def _odata(self, entity, params):
                if entity == 'Catalog_Номенклатура':
                    return [{'Ref_Key': 'transport'}]
                return [{'Ref_Key': 'obj-' + number, 'Объект': number,
                         'Объект_Type': 'StandardODATA.Document_ЗаказПоставщику', 'Сумма': amount}
                        for number, amount in [('688', 4111), ('713', 8621), ('731', 50671), ('758', 54087.48)]]

        cost = delivery_costs(Source(), suppliers, ['customer'])['customer']
        self.assertEqual(cost['amount'], 63403)
        self.assertEqual({row['doc'] for row in cost['sources']}, {'688', '713', '731'})
        self.assertIsNone(cost['issue'])
        self.add('00ЦБ-000133', expected=60000, actual=float(cost['amount']))
        self.assertEqual(self.result()['payout'], 0)
        self.assertEqual(self.result()['details']['rows'][0][-1], 'Не засчитан')

    def test_customer_order_151_uses_transport_first_line_and_preserves_mixed_goods_order(self):
        amounts = {'715': 140000, '718': 9150, '723': 140000, '727': 20000, '734': 9150,
                   '745': 15000, '711': 333999.76}
        suppliers = [{'Ref_Key': number, 'Number': number, 'ТТК_ЗаказКлиента_Key': 'customer',
                      'ДокументОснование_Type': 'StandardODATA.Document_ЗаказКлиента',
                      'ОбъектРасчетов_Key': 'obj-' + number,
                      'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'goods' if number == '711' else 'transport'}]}
                     for number in amounts]
        suppliers[-1]['Товары'].append({'LineNumber': '2', 'Номенклатура_Key': 'transport'})
        suppliers[-1]['Товары'].reverse()
        suppliers[-1]['Партнер'] = {'Перевозчик': True}

        class Source:
            def _odata(self, entity, params):
                if entity == 'Catalog_Номенклатура':
                    return [{'Ref_Key': 'transport', 'Description': 'Транспортные расходы'}]
                return [{'Ref_Key': 'obj-' + number, 'Объект': number,
                         'Объект_Type': 'StandardODATA.Document_ЗаказПоставщику', 'Сумма': amount}
                        for number, amount in amounts.items()]

        cost = delivery_costs(Source(), suppliers, ['customer'])['customer']
        self.assertEqual(cost['amount'], 333300)
        self.assertEqual({row['doc'] for row in cost['sources']}, set(amounts) - {'711'})
        self.assertIsNone(cost['issue'])

    def test_unavailable_mismatched_deleted_or_unfilled_settlement_cannot_become_zero(self):
        suppliers = [{'Ref_Key': 'supplier', 'Number': 'supplier', 'ТТК_ЗаказКлиента_Key': 'customer',
                      'ДокументОснование_Type': 'StandardODATA.Document_ПоручениеЭкспедитору', 'ОбъектРасчетов_Key': 'obj',
                      'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'transport'}]}]
        for problem in ('unavailable', 'wrong-order', 'deleted', 'missing-sum', 'missing-item', 'item-unavailable'):
            with self.subTest(problem=problem):
                class Source:
                    def _odata(self, entity, params):
                        if entity == 'Catalog_Номенклатура':
                            if problem == 'item-unavailable':
                                raise OSError('source unavailable')
                            if problem == 'missing-item':
                                return []
                            return [{'Ref_Key': 'transport'}]
                        if problem == 'unavailable':
                            raise OSError('source unavailable')
                        return [{'Ref_Key': 'obj', 'Объект_Type': 'StandardODATA.Document_ЗаказПоставщику',
                                 'Объект': 'other' if problem == 'wrong-order' else 'supplier',
                                 'DeletionMark': problem == 'deleted', 'Сумма': None if problem == 'missing-sum' else 1}]
                cost = delivery_costs(Source(), suppliers, ['customer'])['customer']
                self.assertTrue(cost['issue'])

    def test_ambiguous_line_order_is_unknown_only_for_affected_customer(self):
        for bad in (None, 'bad', 0, 1, 'missing'):
            with self.subTest(number=bad):
                line = {'Номенклатура_Key': 'transport'}
                if bad != 'missing':
                    line['LineNumber'] = bad
                suppliers = [
                    {'Ref_Key': 'bad', 'ТТК_ЗаказКлиента_Key': 'A', 'ОбъектРасчетов_Key': 'bad',
                     'Товары': [line, {'LineNumber': '1', 'Номенклатура_Key': 'goods'}]},
                    {'Ref_Key': 'good', 'ТТК_ЗаказКлиента_Key': 'B', 'ОбъектРасчетов_Key': 'good',
                     'Товары': [{'LineNumber': '1', 'Номенклатура_Key': 'transport'}]}]

                class Source:
                    def _odata(self, entity, params):
                        if entity == 'Catalog_Номенклатура':
                            return [{'Ref_Key': 'transport'}]
                        return [{'Ref_Key': 'good', 'Объект': 'good', 'Сумма': 10,
                                 'Объект_Type': 'StandardODATA.Document_ЗаказПоставщику'}]

                costs = delivery_costs(Source(), suppliers, ['A', 'B'])
                self.assertTrue(costs['A']['issue'])
                self.assertIsNone(costs['B']['issue'])
                self.assertEqual(costs['B']['amount'], 10)
