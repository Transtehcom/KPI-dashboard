import io
import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app.common import connect, load_rules
from app.gpt_assistant import GPTAssistant
from app import supplier_directory, supplier_export
from app.supplier_format import company_name, region


class SupplierExportTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.service = GPTAssistant(self.con, {'employees': [
            {'login': 'boss', 'name': 'Администратор', 'role': 'manager'},
            {'login': 'employee', 'name': 'Сотрудник', 'role': 'employee', 'position': 'supply'}]}, load_rules())

    def test_names_and_regions(self):
        for raw, expected in [
            ('ООО ГК «Ромашка»', 'Ромашка, ГК ООО'),
            ('ГК Ромашка ООО', 'Ромашка, ГК ООО'),
            ('Ромашка, ГК ООО', 'Ромашка, ГК ООО'),
            ('Общество с ограниченной ответственностью «Ромашка»', 'Ромашка, ООО'),
            ('ИП Иванов Иван Иванович', 'Иванов Иван Иванович, ИП'),
            ('Завод "Сибмаш" ООО', 'Завод "Сибмаш", ООО'),
            ('СК-Силовой кабель', 'Силовой кабель, СК'),
            ('АБСОЛЮТ БАНК (ПАО) АКБ', 'АБСОЛЮТ БАНК, АКБ ПАО'),
            ('1С-Битрикс', '1С-Битрикс'), ('ОООмега', 'ОООмега'),
        ]:
            self.assertEqual(company_name(raw), expected)
            self.assertEqual(company_name(expected), expected)
        for raw in ('Пермский край', 'край Пермский', '59-Пермский край'):
            self.assertEqual(region(raw), '59-Пермский край')
        self.assertEqual(region('Москва г'), '77-г. Москва')
        self.assertEqual(region('обл. Свердловская'), '66-Свердловская область')
        self.assertEqual(region('Московская обл'), '50-Московская область')
        self.assertEqual(region(''), '')
        self.assertEqual(region('Пермский край; Челябинская обл'), '59-Пермский край; 74-Челябинская область')
        self.assertEqual(region('неизвестный регион'), '')

    def test_export_is_owned_and_returns_result_file_with_confirmed_region(self):
        rows = [{'id': 'one', 'fields': {'Название': 'ООО ГК Ромашка', 'Реквизит: ИНН': '5900000000'}},
                {'id': 'two', 'fields': {'Название': 'Завод АО', 'Регион': 'Москва г'}}]
        created = supplier_export.start(self.service, 'boss', rows, launch=False)
        self.assertRaises(ValueError, self.service.chat, 'employee', created['chat_id'])
        def lookup(requests, config, model, effort, fast):
            self.assertEqual(len(requests), 1)
            return {requests[0]['key']: {'region': '59-Пермский край', 'url': 'https://example.test/company',
                                         'title': 'Карточка компании', 'evidence': 'ИНН 5900000000, Пермский край'}}
        with patch('app.supplier_export.lookup_regions', side_effect=lookup):
            supplier_export.run(self.service, created['chat_id'], created['message_id'], rows)
        chat = self.service.chat('boss', created['chat_id'])
        self.assertEqual(chat['messages'][-1]['status'], 'completed')
        self.assertEqual(len(chat['documents']), 2)
        output = next(doc for doc in chat['documents'] if 'результат' in doc['name'])
        _, raw = self.service.documents.download(created['chat_id'], output['id'])
        workbook = load_workbook(io.BytesIO(raw))
        self.assertEqual(workbook.active['C2'].value, 'Ромашка, ГК ООО')
        self.assertEqual(workbook.active['BI2'].value, '59-Пермский край')
        self.assertEqual(workbook.active.max_column, 81)
        with self.con:
            supplier_directory.store(self.con, rows)
        row = next(row for row in supplier_directory.rows(self.con) if row['id'] == 'one')
        self.assertEqual(row['fields']['Регион'], '59-Пермский край')

    def test_employee_cannot_start_supplier_export(self):
        with self.assertRaises(ValueError):
            supplier_export.start(self.service, 'employee', [], launch=False)
        self.assertFalse(self.con.execute('SELECT * FROM gpt_chats').fetchone())

    def test_missing_search_evidence_cannot_fill_region(self):
        result = {'regions': [{'key': 'one', 'region': '59-Пермский край',
                               'url': 'https://example.test/company', 'title': 'Карточка',
                               'evidence': 'ИНН 5900000000 Пермский край'}]}
        with patch('app.supplier_export.call_analysis.run_codex', return_value={'result': result}):
            self.assertEqual(supplier_export.lookup_regions(
                [{'key': 'one', 'inn': '5900000000', 'name': 'Ромашка'}], {}, 'gpt-6-luna', 'medium', False), {})

    def test_search_requires_visited_url_and_consistent_address(self):
        request = [{'key': 'one', 'inn': '5900000000', 'name': 'Ромашка'}]
        for code, url, expected in [('77-г. Москва', 'https://example.test/company', False),
                                    ('59-Пермский край', 'https://example.test/invented', False),
                                    ('59-Пермский край', 'https://example.test/company', True)]:
            def search(prompt, config, **kwargs):
                config['codex_web_used'] = True
                config['codex_web_sources'] = [{'url': 'https://example.test/company'}]
                self.assertNotIn('password', json.dumps(config))
                return {'result': {'regions': [{'key': 'one', 'region': code, 'url': url,
                    'title': 'Ромашка', 'evidence': 'ИНН 5900000000. Юридический адрес: Пермский край, г. Пермь'}]}}
            with patch('app.supplier_export.call_analysis.run_codex', side_effect=search):
                self.assertEqual(bool(supplier_export.lookup_regions(request, {}, 'gpt-6-luna', 'medium', False)), expected)

    def test_full_size_directory_can_be_attached_and_processed(self):
        rows = [{'id': str(i), 'fields': {'Название': f'Поставщик {i} ООО', 'Реквизит: ИНН': f'590000{i:04}',
                                        'Регион': 'Пермский край', 'Рабочий телефон': '+70000000000',
                                        'Рабочий e-mail': 'test@example.test'}} for i in range(3000)]
        created = supplier_export.start(self.service, 'boss', rows, launch=False)
        supplier_export.run(self.service, created['chat_id'], created['message_id'], rows)
        chat = self.service.chat('boss', created['chat_id'])
        self.assertEqual(chat['messages'][-1]['status'], 'completed')
        self.assertEqual(len(chat['documents']), 2)

    def test_cancelled_job_does_not_write_regions_or_result(self):
        created = supplier_export.start(self.service, 'boss', [
            {'id': 'one', 'fields': {'Название': 'Ромашка ООО', 'Реквизит: ИНН': '5900000000'}}], launch=False)
        self.service.cancel('boss', created['chat_id'])
        with patch('app.supplier_export.lookup_regions') as lookup:
            supplier_export.run(self.service, created['chat_id'], created['message_id'], [])
        lookup.assert_not_called()
        chat = self.service.chat('boss', created['chat_id'])
        self.assertEqual(chat['messages'][-1]['status'], 'cancelled')
        self.assertEqual(len(chat['documents']), 1)


if __name__ == '__main__':
    unittest.main()
