import datetime as dt
import json
import unittest
from unittest.mock import patch

from app import supply_documents
from app.common import connect, load_rules
from app.engine import Engine
from app.server import App


class LogisticsDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.config = {'holidays': [], 'employees': [
            {'login': 'logist', 'name': 'Логист', 'position': 'logistics', 'role': 'employee'}]}
        self.engine = Engine(self.con, self.rules, self.config)

    def item(self):
        with patch('app.engine.today', return_value=dt.date(2026, 11, 13)):
            return next(row for row in self.engine.compute('logist', '2026-11')['items']
                        if row['code'] == 'logistics_documents')

    def test_disabled_sixth_item_is_absent_in_reports_and_methodology(self):
        result = self.engine.compute('logist', '2026-11')
        self.assertEqual([row['n'] for row in result['items']], [1, 2, 3, 4, 5])
        self.assertEqual(sum(row['amount'] for row in result['items']), 8500)
        self.assertNotIn('logistics_discipline', [row['code'] for row in result['items']])
        app = App.__new__(App)
        app.config, app.rules, app.con = self.config, self.rules, self.con
        self.assertEqual(len(app.bootstrap(self.config['employees'][0])['positions']['logistics']['items']), 5)

    def test_fraction_target_and_proportional_payment(self):
        self.con.execute("INSERT INTO dataset_state VALUES('forwarder_contracts','2026-11-01','2026-11-30','now')")
        self.con.executemany('INSERT INTO forwarder_contracts(id,emp,dt,doc,files,has_contract) VALUES(?,?,?,?,?,?)',
                             [(str(i), 'logist', '2026-11-03', f'PE-{i}',
                               json.dumps(['Договор-заявка.pdf'] if i < 49 else [], ensure_ascii=False), int(i < 49))
                              for i in range(100)])
        item = self.item()
        self.assertEqual(item['components'][0]['fact'], .49)
        self.assertEqual(item['pct'], .5)
        self.assertEqual(item['payout'], 750)
        self.assertEqual(len(item['details']['rows']), 100)
        self.assertIn('Договор-заявка.pdf', item['details']['rows'][0])
        self.con.execute('UPDATE forwarder_contracts SET has_contract=1')
        self.assertEqual(self.item()['payout'], 1500)

    def test_unloaded_and_empty_period_never_pay(self):
        self.assertIsNone(self.item()['pct'])
        self.assertEqual(self.item()['payout'], 0)
        self.con.execute("INSERT INTO dataset_state VALUES('forwarder_contracts','2026-11-01','2026-11-30','now')")
        self.assertIsNone(self.item()['pct'])
        self.assertEqual(self.item()['payout'], 0)

    def test_only_direct_matching_forwarder_files_count(self):
        def document(key, **extra):
            return {'Ref_Key': key, 'Number': key, 'Date': '2026-11-02T10:00:00',
                    'ТТК_ОтветственныйЛогист': {'Description': 'Логист'}, **extra}

        documents = [document('one'), document('two'), document('three'), document('deleted', DeletionMark=True)]
        files = [
            {'Ref_Key': 'f1', 'ВладелецФайла': 'one', 'ВладелецФайла_Type': supply_documents.FORWARDER_TYPE,
             'Description': 'ДОГОВОР-заявка', 'Расширение': 'pdf'},
            {'Ref_Key': 'f2', 'ВладелецФайла': 'one', 'ВладелецФайла_Type': supply_documents.FORWARDER_TYPE,
             'Description': 'дог. 12', 'Расширение': 'docx'},
            {'Ref_Key': 'f3', 'ВладелецФайла': 'two', 'ВладелецФайла_Type': supply_documents.FORWARDER_TYPE,
             'Description': 'договор', 'DeletionMark': True},
            {'Ref_Key': 'f4', 'ВладелецФайла': 'three', 'ВладелецФайла_Type': supply_documents.ORDER_TYPE,
             'Description': 'договор'},
            {'Ref_Key': 'f5', 'ВладелецФайла': 'two', 'ВладелецФайла_Type': supply_documents.FORWARDER_TYPE,
             'Description': 'подготовка.pdf'},
        ]

        class Source:
            def _odata(self, entity, params):
                data = {'Document_ПоручениеЭкспедитору': documents, 'Catalog_Файлы': files}[entity]
                assert 'Base64' not in params.get('$select', '')
                return data[params.get('$skip', 0):params.get('$skip', 0) + params.get('$top', len(data))]

        rows = supply_documents.fetch_forwarder_contracts(
            Source(), {'логист': 'logist'}, dt.date(2026, 11, 1), dt.date(2026, 11, 30), file_entity='Catalog_Файлы')
        self.assertEqual([row['id'] for row in rows], ['one', 'two', 'three'])
        self.assertEqual([row['has_contract'] for row in rows], [1, 0, 0])
        self.assertEqual([row['emp'] for row in rows], ['logist'] * 3)
        self.assertEqual(len(json.loads(rows[0]['files'])), 2)

    def test_unconfigured_source_cannot_be_marked_as_loaded(self):
        with self.assertRaisesRegex(ValueError, 'Источник прикреплённых файлов'):
            supply_documents.fetch_forwarder_contracts(object(), {}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))

    def test_sync_waits_for_source_and_preserves_confirmed_snapshot_on_error(self):
        from app import sync
        config = {**self.config, 'mode': 'real', 'onec': {'base_url': 'https://example.test',
                  'datasets': {'forwarder_orders': {'source': 'forwarder_orders'}}}}
        with patch('app.sync.OneC') as source, \
                patch('app.sync.supplier_directory.refresh', return_value='ok'), \
                patch('app.sync.onec_sales_register.fetch', return_value=[]), \
                patch('app.sync.onec_money.fetch', return_value=None), \
                patch('app.sync.supply_documents.fetch_forwarder_contracts') as files:
            source.return_value.fetch.return_value = {'forwarder_orders': []}
            self.assertEqual(sync.run(self.con, config)['status'], 'ok')
            files.assert_not_called()
            self.assertIsNone(self.con.execute("SELECT * FROM dataset_state WHERE dataset='forwarder_contracts'").fetchone())
            config['onec']['datasets']['forwarder_orders']['contract_files_entity'] = 'Catalog_Файлы'
            files.return_value = [{'id': 'one', 'emp': 'logist', 'dt': dt.date.today().isoformat(),
                                   'doc': 'PE', 'files': '["договор.pdf"]', 'has_contract': 1}]
            self.assertEqual(sync.run(self.con, config)['status'], 'ok')
            marker = tuple(self.con.execute("SELECT * FROM dataset_state WHERE dataset='forwarder_contracts'").fetchone())
            files.side_effect = ValueError('Файлы недоступны')
            self.assertEqual(sync.run(self.con, config)['status'], 'error')
            self.assertEqual(tuple(self.con.execute("SELECT * FROM dataset_state WHERE dataset='forwarder_contracts'").fetchone()), marker)
            self.assertEqual(self.con.execute('SELECT has_contract FROM forwarder_contracts').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
