import io
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app import supplier_directory as directory
from app.common import connect


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')

    def tearDown(self):
        self.con.close()

    def test_template_and_safe_excel(self):
        self.assertEqual(len(directory.HEADERS), 81)
        self.assertEqual(directory.HEADERS[2], 'Название')
        self.assertEqual(directory.HEADERS[-2:], ['Теги отдела снабжения', 'Реквизит: ИНН'])
        fields = {'Название': '=1+1', 'Реквизит: ИНН': '0012345678', 'Тип компании': 'Поставщик'}
        body = directory.xlsx([{'id': 'a', 'fields': fields}])
        wb = load_workbook(io.BytesIO(body))
        sheet = wb.active
        self.assertEqual([c.value or '' for c in sheet[1]], directory.HEADERS)
        self.assertEqual(sheet['C2'].value, '=1+1')
        self.assertEqual(sheet['C2'].data_type, 's')
        self.assertEqual(sheet['CC2'].value, '0012345678')
        self.assertIsNone(sheet['A2'].value)
        self.assertIsNone(sheet['BA2'].value)
        self.assertIsNone(sheet['CA2'].value)
        self.assertEqual(sheet.max_row, 2)

    def test_build_contacts_legal_entities_and_supplier_scope(self):
        partners = [dict(Ref_Key='p', Description='Поставщик', Поставщик=True,
                         КонтактнаяИнформация=[{'Тип': 'Телефон', 'Представление': '+7 111'}]),
                    dict(Ref_Key='client', Description='Клиент', Поставщик=False),
                    dict(Ref_Key='carrier', Description='Перевозчик', Перевозчик=True),
                    dict(Ref_Key='deleted', Description='Удалён', Поставщик=True, DeletionMark=True)]
        counterparties = [dict(Ref_Key='a', Партнер_Key='p', Description='Компания А', ИНН='0012345678',
                               КонтактнаяИнформация=[{'Тип': 'АдресЭлектроннойПочты', 'АдресЭП': 'a@example.test'},
                                                    {'Тип': 'Адрес', 'Регион': 'Пермский край'}]),
                          dict(Ref_Key='b', Партнер_Key='p', Description='Компания Б', ИНН='9876543210')]
        data = directory.build(partners, counterparties, {'p': ['Проволока'], 'client': ['Кабель']}, {})
        self.assertEqual(len(data), 3)
        a = next(row['fields'] for row in data if row['id'] == 'a')
        b = next(row['fields'] for row in data if row['id'] == 'b')
        self.assertEqual(a['Название'], 'Компания А')
        self.assertEqual(a['Рабочий телефон'], '+7 111')
        self.assertEqual(a['Рабочий e-mail'], 'a@example.test')
        self.assertEqual(a['Регион'], 'Пермский край')
        self.assertFalse(b.get('Рабочий e-mail'))
        self.assertEqual(a['Теги отдела снабжения'], 'Проволока')
        self.assertFalse(a.get('Стратегия'))

    def test_store_search_and_failed_refresh_preserve_snapshot(self):
        rows = [{'id': 'a', 'fields': {'Название': 'Ёмкости А', 'Реквизит: ИНН': '123',
                                     'Теги отдела снабжения': 'Резервуары'}},
                {'id': 'b', 'fields': {'Название': 'Другой', 'Реквизит: ИНН': '456'}}]
        with self.con:
            directory.store(self.con, rows, '2026-11-13T12:00:00')
        self.assertEqual(len(directory.rows(self.con, 'емкости 123')), 1)
        self.assertEqual(len(directory.rows(self.con, 'резервуары')), 1)
        self.assertFalse(directory.rows(self.con, '%'))
        with patch.object(directory, 'fetch', side_effect=RuntimeError('нет связи')):
            self.assertEqual(directory.refresh(self.con, object()), 'error')
        self.assertEqual(len(directory.rows(self.con)), 2)
        self.assertEqual(directory.meta(self.con)['synced_at'], '2026-11-13T12:00:00')
        self.assertIn('нет связи', directory.meta(self.con)['error'])

    def test_categories_keep_materials_separate(self):
        self.assertIn('Опоры ЛЭП (ЖБИ)', directory.item_categories('Стойка СВ 110-5'))
        self.assertNotIn('Опоры ЛЭП (мет.)', directory.item_categories('Стойка СВ 110-5'))
        self.assertIn('Опоры ЛЭП (мет.)', directory.item_categories('Опора ЛЭП металлическая У110'))
        self.assertNotIn('Опоры ЛЭП (мет.)', directory.item_categories('Хомут для опоры ЛЭП'))
        self.assertEqual(directory.item_categories('Неизвестная деталь'), [])

    def test_read_all_paginates_and_detects_repeated_page(self):
        class Source:
            def _odata(self, entity, params):
                return [{'Ref_Key': str(i)} for i in range(params['$skip'], min(503, params['$skip'] + params['$top']))]
        self.assertEqual(len(directory.read_all(Source(), 'Catalog_X', 'Ref_Key')), 503)
        with patch.object(Source, '_odata', return_value=[{'Ref_Key': 'same'}]):
            with self.assertRaisesRegex(ValueError, 'Повтор'):
                directory.read_all(Source(), 'Catalog_X', 'Ref_Key')

    def test_fetch_uses_explicit_links_and_catalog_flags(self):
        datasets = {
            'Catalog_Партнеры': [dict(Ref_Key='supplier', Description='Поставщик', Поставщик=True),
                                 dict(Ref_Key='customer', Description='Клиент'),
                                 dict(Ref_Key='from-doc', Description='Поставщик из документа')],
            'Catalog_Контрагенты': [dict(Ref_Key='legal', Партнер_Key='supplier', Description='ООО', ИНН='123')],
            'Catalog_ВидыКонтактнойИнформации': [],
            'Catalog_Номенклатура': [dict(Ref_Key='item', Description='Проволока стальная')],
            'Catalog_НоменклатураКонтрагентов': [dict(Ref_Key='mapping', Owner_Key='customer', Номенклатура_Key='item')],
            'Document_ЗаказПоставщику': [dict(Ref_Key='order', Партнер_Key='from-doc',
                                             Товары=[dict(Номенклатура_Key='item', Количество=1)])],
            'Document_ПриобретениеТоваровУслуг': [],
            'Document_ТТК_ЗаявкаВОтделСнабжения': [],
        }

        class Source:
            def _odata(self, entity, params):
                return datasets[entity][params['$skip']:params['$skip'] + params['$top']]

        data = directory.fetch(Source())
        self.assertEqual({row['id'] for row in data}, {'legal', 'from-doc'})
        linked = next(row for row in data if row['id'] == 'from-doc')
        self.assertEqual(linked['fields']['Теги отдела снабжения'], 'Проволока')

    def test_failed_store_rolls_back_and_empty_snapshot_is_exportable(self):
        original = [{'id': 'old', 'fields': {'Название': 'Старый поставщик'}}]
        with self.con:
            directory.store(self.con, original)
        duplicate = [{'id': 'duplicate', 'fields': {'Название': 'Новый'}}] * 2
        with patch.object(directory, 'fetch', return_value=duplicate):
            self.assertEqual(directory.refresh(self.con, object()), 'error')
        self.assertEqual(directory.rows(self.con), original)
        self.assertFalse(directory.is_stale(self.con))
        workbook = load_workbook(io.BytesIO(directory.xlsx([])))
        self.assertEqual(workbook.active.max_row, 1)


if __name__ == '__main__':
    unittest.main()
