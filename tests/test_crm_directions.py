import json
import unittest
from unittest.mock import Mock

from app.bitrix24 import Bitrix24, B24Error
from app.common import connect, replace_rows
from app.funnel import deal_funnel


class FunnelDirectionsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.config = {'employees': [], 'deal_funnel': {'sections': [{'key': 'all', 'all_open': True}]},
                       'lead_funnel': {'sections': [{'key': 'all', 'all_open': True}]}}
        self.samples = [
            ('1', 'Защита от БПЛА', ['KPI']),
            ('2', 'Проект', ['БПЛА']),
            ('3', 'БПЛА', ['БПЛА']),
            ('4', 'БПЛА без направления', []),
            ('5', 'Другой проект', ['KPI']),
            ('6', 'EPC', ['EPC подрядчики', 'Инвестиции']),
            ('7', 'Инвестиционный проект', ['Инвестиции']),
            ('8', 'Факторинг', ['Факторинг']),
            ('9', 'Прочее', ['Тендерплан']),
            ('10', 'БПЛА и EPC', ['EPC']),
        ]

    def load_rows(self, kind, mode):
        entity = 'lead' if kind == 'leads' else 'deal'
        rows = [{'id': entity + key, 'entity': entity, 'title': title, 'tags': json.dumps(tags),
                 'category_id': '0', 'category': 'Продажи', 'stage_id': 'NEW', 'stage': 'В работе',
                 'semantic': 'P', 'amount': int(key) * 10, 'created': '2026-01-01',
                 'snapshot': '2026-11-10T10:00:00'} for key, title, tags in self.samples]
        replace_rows(self.con, 'fact_records' if mode == 'fact' else 'crm_deals', rows)
        replace_rows(self.con, 'stage_moves', [
            {'id': row['id'] + ':1', 'rid': row['id'], 'entity': entity, 'stage_id': 'NEW',
             'stage': 'В работе', 'category_id': '0', 'category': 'Продажи', 'semantic': 'P',
             'ts': '2026-11-05T12:00:00'} for row in rows])

    def test_direction_union_all_and_kpi_bpla_exception_in_both_modes(self):
        cases = [(['bpla'], {'1', '2', '3', '4', '10'}),
                 (['kpi', 'bpla'], {'1', '2', '3', '5'}),
                 (['kpi', 'bpla', 'epc'], {'1', '2', '3', '5', '6', '10'}),
                 (['epc', 'investments'], {'6', '7', '10'}),
                 (['epc', 'bpla'], {'1', '2', '3', '4', '6', '10'}),
                 ('all', {'1', '2', '3', '5', '6', '7', '8', '10'})]
        for kind in ['leads', 'deals']:
            for mode in ['date', 'fact']:
                self.load_rows(kind, mode)
                entity = 'lead' if kind == 'leads' else 'deal'
                for selection, expected in cases:
                    with self.subTest(kind=kind, mode=mode, selection=selection):
                        report = deal_funnel(self.con, self.config, kind=kind, mode=mode, directions=selection)
                        self.assertEqual(report['total'], len(expected))
                        self.assertEqual({row['id'] for row in report['sections'][0]['deals']},
                                         {entity + key for key in expected})
                        self.assertEqual(report['total_amount'], sum(int(key) * 10 for key in expected))

    def test_unknown_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            deal_funnel(self.con, self.config, directions=['other'])

    def test_other_filter_options_remain_visible_when_directions_change(self):
        config = {**self.config, 'employees': [{'login': 'epc_owner', 'department': 'supply'}]}
        for mode in ['date', 'fact']:
            with self.subTest(mode=mode):
                self.load_rows('deals', mode)
                table = 'fact_records' if mode == 'fact' else 'crm_deals'
                self.con.execute('UPDATE ' + table + " SET emp='epc_owner',responsible='EPC owner',category_id='epc' WHERE id='deal6'")
                report = deal_funnel(self.con, config, mode=mode, directions=['bpla'], login=['epc_owner'],
                                     category='epc', departments=['supply'])
                self.assertEqual(report['total'], 0)
                for name, value in [('responsibles', 'epc_owner'), ('departments', 'supply'), ('categories', 'epc')]:
                    self.assertTrue(any(row['value'] == value for row in report['filters'][name]), name)

    def test_demo_default_direction_request_has_tagged_cards(self):
        from app.demo import generate
        from app.common import ROOT
        from pathlib import Path
        config = json.loads(Path(ROOT, 'config.example.json').read_text(encoding='utf-8'))
        generate(self.con, config)
        for kind in ['leads', 'deals']:
            report = deal_funnel(self.con, config, kind=kind, directions='all')
            self.assertEqual(report['total'], deal_funnel(self.con, config, kind=kind)['total'])
            self.assertGreater(deal_funnel(self.con, self.config, kind=kind, directions='all')['total'], 0)

    def test_fact_filters_closed_records_and_preserves_transition_date(self):
        self.load_rows('deals', 'fact')
        self.con.execute("UPDATE fact_records SET semantic='S' WHERE id='deal1'")
        self.con.execute("UPDATE stage_moves SET semantic='S',stage='Успех' WHERE rid='deal1'")
        result = deal_funnel(self.con, self.config, mode='fact', directions=['bpla'],
                             date_from='2026-11-01', date_to='2026-11-30')
        closed = next(section for section in result['sections'] if section['key'] == '_won')
        self.assertEqual([r['id'] for r in closed['deals']], ['deal1'])
        self.assertEqual(result['applied']['date_from'], '2026-11-01')


class CRMTagSourceTest(unittest.TestCase):
    def source(self):
        b = Bitrix24({'webhook': 'https://example.test/rest/1/test/'}, log=lambda *args: None)
        b.call = Mock(return_value={'result': {'UF_TAG': {'type': 'string', 'formLabel': 'Теги'}}})
        b.deal_stages = Mock(return_value={'0': {'name': 'Продажи', 'stages': {}}})
        b.lead_statuses = Mock(return_value={})
        b._user_names = Mock(return_value={})
        return b

    def test_tags_are_selected_normalized_and_stored_for_leads_and_deals(self):
        for kind in ['lead', 'deal']:
            b = self.source()
            def records(method, params, key=None):
                self.assertIn('UF_TAG', params['select'])
                return [{'ID': '7', 'TITLE': 'Проект', 'UF_TAG': ' ерс ; бпла, KPI\nИнвестиции'}]
            b.list_all = records
            b.by_ids = Mock(return_value={})
            result = b.fetch_crm_leads({}, {}) if kind == 'lead' else b.fetch_crm_deals({}, {})
            self.assertEqual(json.loads(result[0]['tags']), ['EPC', 'БПЛА', 'KPI', 'Инвестиции'])

    def test_fact_refresh_includes_retained_cards_outside_new_history_window(self):
        b = self.source()
        def records(method, params, key=None):
            if method == 'crm.stagehistory.list':
                return []
            self.assertIn('UF_TAG', params['select'])
            return [{'ID': str(i), 'TITLE': 'Закрытый проект', 'UF_TAG': 'KPI'} for i in params['filter']['ID']]
        b.list_all = records
        moves, cards = b.fetch_crm_fact({}, '2026-11-01', {}, retained=['lead1', 'deal2'])
        self.assertEqual(moves, [])
        self.assertEqual({r['id'] for r in cards}, {'lead1', 'deal2'})
        self.assertTrue(all(json.loads(r['tags']) == ['KPI'] for r in cards))

    def test_missing_tag_metadata_stops_refresh(self):
        b = self.source()
        b.call.return_value = {'result': {}}
        with self.assertRaises(B24Error):
            b.fetch_crm_leads({}, {})
