import datetime as dt
import importlib
import sqlite3
import unittest
from unittest import mock


class OperationsViewsTest(unittest.TestCase):
    def test_missing_submission_history_is_unknown_instead_of_zero(self):
        self.capture([{'id':'meta','kind':'source_meta','capabilities':{'submission_history':False}}],at='2026-11-09T08:00:00')
        report=self.report('sales')
        tile=next(tile for tile in report['tiles'] if tile['key']=='submitted')
        self.assertIsNone(tile['value'])

    def setUp(self):
        try:
            self.views = importlib.import_module('app.operations_views')
        except ImportError:
            self.views = None
        self.assertIsNotNone(self.views, 'Computed operations reports must be implemented')
        from app import operations
        self.core = operations
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        operations.ensure_schema(self.con)
        self.config = {'employees': [
            {'login': 'sales', 'name': 'Sales', 'department': 'sales', 'onec_manager': 'Sales'},
            {'login': 'supply', 'name': 'Supply', 'department': 'supply', 'onec_manager': 'Supply'}]}

    def capture(self, entities, events=(), at='2026-11-09T09:00:00', source='onec'):
        self.core.capture(self.con, source, entities, list(events), observed_at=at)

    def report(self, kind='supply', **kwargs):
        return self.views.build(self.con, self.config, kind, '2026-11-09', '2026-11-09',
                                now='2026-11-09T20:00:00', **kwargs)

    def test_sales_groups_only_explicit_links_and_attributes_events_to_author(self):
        self.capture([
            {'id': 'onec:tender:a', 'kind': 'tender', 'owner': 'sales', 'client': 'Same'},
            {'id': 'onec:kp:b', 'kind': 'kp', 'owner': 'sales', 'client': 'Same', 'links': ['onec:tender:a']},
            {'id': 'onec:tender:c', 'kind': 'tender', 'owner': 'sales', 'client': 'Same'},
            {'id': 'onec:supply:s', 'kind': 'supply', 'owner': 'supply', 'links': ['onec:tender:a']}],
            [{'id': 'e1', 'entity_id': 'onec:supply:s', 'kind': 'closed', 'at': '2026-11-09T11:00:00', 'author': 'supply'}])
        all_report = self.report('sales')
        self.assertEqual(len(all_report['records']), 2)
        own_report = self.report('sales', emp='sales')
        self.assertEqual(own_report['events'], [])
        self.assertEqual(all_report['events'][0]['author'], 'supply')

    def test_lost_and_rejected_tenders_do_not_leave_submission_reminders(self):
        for status in ('Проиграно', 'Отклонён', 'Отменено'):
            with self.subTest(status=status):
                self.capture([{'id': 't', 'kind': 'tender', 'status': status, 'due': '2026-11-08T18:00:00'}])
                report = self.report('sales')
                self.assertEqual(report['queue'], [])
                detail = self.views.document_detail(self.con, self.config, 't', '2026-11-09T20:00:00')
                self.assertTrue(detail['is_cancelled'])

    def test_won_tender_stays_open_without_the_old_submission_deadline(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Выиграно', 'due': '2026-11-08T18:00:00'}])
        row = self.report('sales')['queue'][0]
        self.assertNotEqual(row['timeliness'], 'overdue')
        self.assertIsNone(row.get('due'))
        self.assertTrue(row['submission_obligation_finished'])
        self.assertEqual(row['documents'][0]['due'], '2026-11-08T18:00:00')

    def test_explicit_order_successor_supersedes_the_ancestor_submission_deadlines(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-07T18:00:00'},
                      {'id': 'kp', 'kind': 'kp', 'status': 'Действует', 'due': '2026-11-08T18:00:00', 'links': ['t']},
                      {'id': 'o', 'kind': 'order', 'status': 'ВРаботе', 'due': '2026-11-10T18:00:00', 'links': ['kp']}])
        row = self.report('sales')['queue'][0]
        self.assertEqual((row['due'], row['status'], row['timeliness']), ('2026-11-10T18:00:00', 'ВРаботе', 'open'))
        self.assertEqual(row['active_obligation_id'], 'o')
        source_tender = next(doc for doc in row['documents'] if doc['id'] == 't')
        self.assertEqual((source_tender['status'], source_tender['due']), ('Действует', '2026-11-07T18:00:00'))

    def test_cancelled_successor_does_not_finish_the_active_parent_submission(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-08T18:00:00'},
                      {'id': 'kp', 'kind': 'kp', 'status': 'Отменено', 'links': ['t']}])
        row = self.report('sales')['queue'][0]
        self.assertEqual(row['timeliness'], 'overdue')
        self.assertEqual(row['active_obligation_id'], 't')

    def test_sibling_orders_use_the_next_actual_open_deadline(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-07T18:00:00'},
                      {'id': 'future', 'kind': 'order', 'status': 'ВРаботе', 'due': '2026-11-11T18:00:00', 'links': ['t']},
                      {'id': 'next', 'kind': 'order', 'status': 'ВРаботе', 'due': '2026-11-09T10:00:00', 'links': ['t']}],
                     at='2026-11-09T12:00:00')
        row = self.report('sales')['queue'][0]
        self.assertEqual(row['active_obligation_id'], 'next')
        self.assertEqual((row['due'], row['timeliness']), ('2026-11-09T10:00:00', 'overdue'))

    def test_closed_order_finishes_ancestors_without_leaving_a_false_open_queue(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-07T18:00:00'},
                      {'id': 'o', 'kind': 'order', 'status': 'Закрыт', 'closed_at': '2026-11-08T18:00:00', 'links': ['t']}])
        self.assertEqual(self.report('sales')['queue'], [])

    def test_long_order_chains_do_not_repeat_each_ancestors_links(self):
        reads = [0]
        class TrackedLinks(list):
            def __iter__(self):
                reads[0] += len(self)
                return super().__iter__()
        raw = [{'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-08'}]
        for index in range(150):
            raw.append({'id': 'o' + str(index), 'kind': 'order', 'status': 'ВРаботе',
                        'links': ['o' + str(index - 1) if index else 't'], 'due': '2026-11-11'})
        documents = [self.views._record(entity, [], dt.datetime(2026, 11, 9, 12), {}, {}) for entity in raw]
        for document in documents:
            document['links'] = TrackedLinks(document['links'])
        rows = self.views._sales_records(documents)
        self.assertEqual(len(rows), 1)
        self.assertLess(reads[0], 25 * len(documents))

    def test_completed_bitrix_activity_and_recorded_call_do_not_reopen_a_closed_order(self):
        from app.common import connect
        con = connect(':memory:')
        self.addCleanup(con.close)
        self.core.ensure_schema(con)
        con.execute('INSERT INTO crm_deals(id,emp,semantic,stage,title) VALUES(?,?,?,?,?)',
                    ('deal12', 'sales', 'S', 'WON', 'Deal'))
        con.execute('INSERT INTO call_records(id,emp,dt,direction,bindings) VALUES(?,?,?,?,?)',
                    ('call', 'sales', '2026-11-09T09:00:00', 'out', 'DEAL_12'))
        self.core.capture(con, 'onec', [{'id': 'o', 'kind': 'order', 'status': 'Закрыт',
                                       'closed_at': '2026-11-09T10:00:00'}], [], '2026-11-09T10:00:00')
        self.core.capture_bitrix(con, self.config, observed_at='2026-11-09T11:00:00', source_data={
            'deal_links': {'12': ['o']}, 'activities': [{'ID': 'a', 'COMPLETED': 'Y',
            'CREATED': '2026-11-09T09:00:00', 'LAST_UPDATED': '2026-11-09T10:00:00',
            'BINDINGS': [{'OWNER_TYPE_ID': 2, 'OWNER_ID': 12}]}]})
        report = self.views.build(con, self.config, 'sales', '2026-11-09', '2026-11-09', now='2026-11-09T20:00:00')
        self.assertEqual(report['queue'], [])
        activity = self.views.document_detail(con, self.config, 'bitrix:activity:a', '2026-11-09T20:00:00')
        self.assertTrue(activity['is_closed'])
        self.assertEqual(activity['timeliness'], 'review')

    def test_lost_tender_keeps_only_a_linked_active_department_obligation(self):
        self.capture([{'id': 't', 'kind': 'tender', 'status': 'Проиграно', 'due': '2026-11-08T18:00:00'},
                      {'id': 's', 'kind': 'supply', 'status': 'ВРаботе', 'due': '2026-11-10T18:00:00', 'links': ['t']}])
        row = self.report('sales')['queue'][0]
        self.assertEqual(row['active_obligation_id'], 's')
        self.assertEqual(row['timeliness'], 'open')
        self.assertEqual(next(doc for doc in row['documents'] if doc['id'] == 't')['timeliness'], 'cancelled')

    def test_current_successor_cannot_remove_a_historical_submission_obligation(self):
        tender = {'id': 't', 'kind': 'tender', 'status': 'Действует', 'due': '2026-11-09T10:00:00'}
        self.capture([tender], at='2026-11-09T11:00:00')
        self.capture([tender, {'id': 'o', 'kind': 'order', 'status': 'ВРаботе', 'links': ['t'],
                               'due': '2026-11-11T18:00:00'}], at='2026-11-10T12:00:00')
        self.assertEqual(self.report('sales')['queue'][0]['timeliness'], 'overdue')

    def test_supply_due_today_late_and_missing_plan_fact_have_separate_memberships(self):
        self.capture([], at='2026-11-09T08:00:00')
        self.capture([{'id': 'today', 'kind': 'supply', 'owner': 'supply', 'status': 'ВРаботе', 'due': '2026-11-09T18:00:00'},
                      {'id': 'late', 'kind': 'supply', 'owner': 'supply', 'status': 'Отработана',
                       'due': '2026-11-09T10:00:00', 'closed_at': '2026-11-09T11:00:00'},
                      {'id': 'no-plan', 'kind': 'supply', 'owner': 'supply', 'status': 'Отработана', 'closed_at': '2026-11-09T11:00:00'},
                      {'id': 'no-fact', 'kind': 'supply', 'owner': 'supply', 'status': 'Отработана', 'due': '2026-11-09T10:00:00'}],
                     at='2026-11-09T12:00:00')
        report = self.report()
        self.assertEqual(report['results']['due_today'], 1)
        self.assertEqual(report['results']['closed_late'], 1)
        self.assertEqual(report['results']['closed_without_plan'], 1)
        self.assertEqual(report['results']['closed_without_fact'], 1)
        self.assertEqual(report['results']['known_timeliness'], 1)
        self.assertEqual(next(tile for tile in report['tiles'] if tile['key'] == 'due_today')['record_ids'], ['today'])
        person = next(person for person in report['people'] if person['login'] == 'supply')
        self.assertEqual((person['due_today'], person['closed_late']), (1, 1))

    def test_selected_authors_actions_keep_colleague_documents_without_own_queue(self):
        self.capture([], at='2026-11-09T08:00:00')
        self.capture([{'id': 'a', 'kind': 'tender', 'owner': 'supply', 'status': 'open'}],
                     [{'id': 'submit', 'entity_id': 'a', 'kind': 'submitted',
                       'author': 'sales', 'at': '2026-11-09T10:00:00'}], at='2026-11-09T11:00:00')
        report = self.report('sales', emp='sales')
        self.assertEqual([event['id'] for event in report['events']], ['submit'])
        self.assertTrue(report['records'][0]['action_context'])
        self.assertEqual(report['queue'], [])

    def test_old_closed_requests_are_only_returned_as_linked_context(self):
        self.capture([{'id': 'old', 'kind': 'supply', 'status': 'closed', 'closed_at': '2026-10-01'},
                      {'id': 'open', 'kind': 'supply', 'status': 'open', 'links': ['old']}])
        report = self.report()
        self.assertEqual([row['id'] for row in report['records']], ['open'])
        self.assertIn('old', {row['id'] for row in report['records'][0]['documents']})

    def test_supply_uses_last_closure_unique_counts_and_known_denominator(self):
        self.capture([], at='2026-11-09T08:00:00')
        self.capture([
            {'id': 's1', 'kind': 'supply', 'owner': 'supply', 'status': 'closed',
             'due': '2026-11-09T12:00:00', 'closed_at': '2026-11-09T14:00:08'},
            {'id': 's2', 'kind': 'supply', 'owner': 'supply', 'status': 'closed', 'closed_at': '2026-11-09T14:00:00'},
            {'id': 's3', 'kind': 'supply', 'owner': 'supply', 'status': 'open', 'due': '2026-11-09T12:00:00'}],
            [{'id': 'c1', 'entity_id': 's1', 'kind': 'closed', 'at': '2026-11-09T10:00:00'},
             {'id': 'r1', 'entity_id': 's1', 'kind': 'reopened', 'at': '2026-11-09T11:00:00'},
             {'id': 'c2', 'entity_id': 's1', 'kind': 'closed', 'at': '2026-11-09T14:00:08'},
             {'id': 'c3', 'entity_id': 's2', 'kind': 'closed', 'at': '2026-11-09T14:00:00'}], at='2026-11-09T15:00:00')
        report = self.report()
        first = next(r for r in report['records'] if r['id'] == 's1')
        self.assertEqual(first['late_seconds'], 7208)
        self.assertEqual(first['timeliness'], 'late')
        self.assertEqual(report['results']['closed'], 2)
        self.assertEqual(report['results']['known_timeliness'], 1)
        self.assertEqual(report['results']['on_time_rate'], 0)
        self.assertEqual({r['id'] for r in report['queue']}, {'s3'})

    def test_conflicting_closure_dates_are_flagged_for_review(self):
        self.capture([], at='2026-11-09T08:00:00')
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'closed',
                       'due': '2026-11-09T12:00:00', 'closed_at': '2026-11-09T11:00:00'}],
                     [{'id': 'c', 'entity_id': 's', 'kind': 'closed', 'at': '2026-11-09T13:00:00'}],
                     at='2026-11-09T14:00:00')
        report = self.report()
        self.assertEqual(report['records'][0]['timeliness'], 'review')
        self.assertEqual(report['records'][0]['closed_at'], '2026-11-09T13:00:00')
        self.assertEqual(report['results']['known_timeliness'], 0)

    def test_history_boundary_and_cutoff_do_not_leak_current_state(self):
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'open'}])
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'closed', 'closed_at': '2026-11-10T10:00:00'}], at='2026-11-10T11:00:00')
        report = self.report()
        self.assertEqual(report['records'][0]['status'], 'open')
        before = self.views.build(self.con, self.config, 'supply', '2026-10-01', '2026-10-01', now='2026-11-10')
        self.assertEqual(before['records'], [])
        self.assertFalse(before['coverage']['available'])
        self.assertIsNone(before['results']['on_time_rate'])

    def test_queue_deadline_stops_at_last_successful_source_observation(self):
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'open',
                       'due': '2026-11-09T11:00:00'}], at='2026-11-09T10:00:00')
        report = self.report()
        self.assertEqual(report['queue'][0]['timeliness'], 'open')
        self.assertEqual(report['queue'][0]['asof'], '2026-11-09T10:00:00')

    def test_shared_transport_does_not_merge_two_sales_opportunities(self):
        self.capture([{'id': 'a', 'kind': 'tender'}, {'id': 'b', 'kind': 'tender'},
                      {'id': 'l', 'kind': 'transport', 'links': ['a', 'b']}])
        report = self.report('sales')
        self.assertEqual(len(report['records']), 2)
        self.assertTrue(all('l' in {d['id'] for d in row['documents']} for row in report['records']))

    def test_unlinked_calls_are_counted_once_by_author_after_source_baseline(self):
        self.capture([], at='2026-11-09T08:00:00', source='bitrix')
        self.capture([{'id': 'call1', 'kind': 'call', 'owner': 'sales', 'links': []}],
                     [{'id': 'call1', 'entity_id': 'call1', 'kind': 'call',
                       'author': 'sales', 'at': '2026-11-09T09:00:00'}],
                     source='bitrix', at='2026-11-09T10:00:00')
        report = self.report('sales')
        self.assertEqual([event['id'] for event in report['events']], ['call1'])
        self.assertEqual(next(tile for tile in report['tiles'] if tile['key'] == 'calls')['value'], 1)
        self.assertEqual(report['people'][0]['events'], 1)

    def test_planned_meetings_are_counted_separately_from_completed_meetings(self):
        self.capture([], at='2026-11-09T08:00:00', source='bitrix')
        self.capture([{'id': 'activity', 'kind': 'activity', 'owner': 'sales'}],
                     [{'id': 'planned', 'entity_id': 'activity', 'kind': 'meeting_planned',
                       'author': 'sales', 'at': '2026-11-09T09:00:00'},
                      {'id': 'held', 'entity_id': 'activity', 'kind': 'meeting',
                       'author': 'sales', 'at': '2026-11-09T10:00:00'}],
                     source='bitrix', at='2026-11-09T11:00:00')
        report = self.report('sales')
        planned = next(tile for tile in report['tiles'] if tile['key'] == 'meetings')
        self.assertEqual(planned['title'], 'Назначены встречи')
        self.assertEqual(planned['event_ids'], ['planned'])
        held = next(tile for tile in report['tiles'] if tile['key'] == 'completed_meetings')
        self.assertEqual(held['event_ids'], ['held'])

    def test_source_baselines_do_not_backfill_preimport_calls(self):
        self.capture([], at='2026-11-09T08:00:00')
        self.capture([{'id': 'call1', 'kind': 'call', 'owner': 'sales', 'links': []}],
                     [{'id': 'call1', 'entity_id': 'call1', 'kind': 'call',
                       'author': 'sales', 'at': '2026-11-09T09:00:00'}],
                     source='bitrix', at='2026-11-09T10:00:00')
        self.assertEqual(self.report('sales')['events'], [])

    def test_source_deadline_changes_require_audited_approval(self):
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'open', 'due': '2026-11-09T10:00:00'}], at='2026-11-09T08:00:00')
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'open', 'due': '2026-11-09T18:00:00'}], at='2026-11-09T12:00:00')
        row = self.report()['records'][0]
        self.assertEqual(row['due'], '2026-11-09T10:00:00')
        self.assertTrue(row['due_change_pending'])
        self.views.approve_deadline(self.con, 's', '2026-11-09T18:00:00', 'Agreed by manager', 'admin')
        row = self.views.build(self.con, self.config, 'supply', '2026-11-09', '2030-01-01', now='2030-01-01')['records'][0]
        self.assertEqual(row['due'], '2026-11-09T18:00:00')
        self.assertFalse(row['due_change_pending'])
        self.assertEqual(row['deadline_approval']['author'], 'admin')

    def test_unverified_transport_due_and_closure_are_not_confirmed_timeliness(self):
        self.capture([{'id': 'a', 'kind': 'tender', 'status': 'open'},
                      {'id': 'l', 'kind': 'transport', 'status': 'closed', 'links': ['a'],
                       'due': '2026-11-09T10:00:00', 'due_verified': False,
                       'closed_at': '2026-11-09T11:00:00'}], at='2026-11-09T12:00:00')
        transport = next(doc for doc in self.report('sales')['records'][0]['documents'] if doc['id'] == 'l')
        self.assertEqual(transport['timeliness'], 'unknown')

    def test_safe_onec_document_urls_are_retained(self):
        self.capture([{'id': 's', 'kind': 'supply', 'url': 'e1cib/data/Документ.Заявка?ref=123abc'}])
        detail = self.views.document_detail(self.con, self.config, 's', '2026-11-09T20:00:00')
        self.assertTrue(detail['url'].startswith('e1cib/data/'))

    def test_source_limitations_are_visible_and_metadata_is_not_a_document(self):
        self.capture([{'id': 'operations:source:onec', 'kind': 'source_meta',
                       'warnings': ['Журнал состояний не опубликован']}, {'id': 's', 'kind': 'supply'}])
        report = self.report()
        self.assertIn('Журнал состояний не опубликован', report['warnings'])
        self.assertEqual([row['id'] for row in report['records']], ['s'])

    def test_price_median_latest_supplier_and_quantity_conditions(self):
        item = {'id': 'line', 'nomenclature': 'n1', 'quantity': 10, 'unit': 'pcs',
                'currency': 'RUB', 'vat_included': True, 'delivery': 'included'}
        def quote(supplier, price, **kw):
            return dict(item, id='q-' + supplier + str(price), supplier_id=supplier,
                        price=price, quote_date='2026-11-09T09:00:00', **kw)
        offers = [quote('a', 10), quote('b', 30), quote('c', 50, quantity_min=20, quantity_max=30)]
        self.capture([{'id': 'r', 'kind': 'supply', 'items': [dict(item, offers=offers)]}])
        history = self.views.price_history(self.con, item, '2026-11-09T20:00:00')
        self.assertEqual(history['median'], 20)
        self.assertEqual(history['supplier_count'], 2)
        self.assertEqual(len(history['offers']), 3)
        self.assertFalse(next(q for q in history['offers'] if q['supplier_id'] == 'c')['eligible'])

    def test_price_history_retains_versions_without_reusing_own_or_unknown_quotes(self):
        item = {'id': 'line', 'nomenclature': 'n1', 'quantity': 10, 'unit': 'pcs',
                'currency': 'RUB', 'vat_included': True, 'delivery': 'included'}
        def request(price, offered_at):
            return {'id': 'old', 'kind': 'supply', 'items': [dict(item, offers=[
                dict(item, id='a', supplier_id='a', price=price, offered_at=offered_at)])]}
        self.capture([request(10, '2026-11-08T12:00:00')])
        self.capture([request(20, '2026-11-09T12:00:00'),
                      {'id': 'own', 'kind': 'supply', 'items': [dict(item, offers=[
                          dict(item, id='own', supplier_id='b', price=100, offered_at='2026-11-09T12:00:00')])]}],
                     at='2026-11-09T13:00:00')
        history = self.views.price_history(self.con, dict(item, request_id='own'), '2026-11-09T20:00:00')
        self.assertEqual(history['median'], 20)
        self.assertEqual(history['supplier_count'], 1)
        self.assertEqual(len(history['offers']), 3)
        own = next(offer for offer in history['offers'] if offer['supplier_id'] == 'b')
        self.assertIn('own_quote', own['exclusion_reasons'])

    def test_quantity_approval_expires_when_the_target_quantity_changes(self):
        common = {'nomenclature': 'n1', 'unit': 'pcs', 'currency': 'RUB',
                  'vat_included': True, 'delivery': 'included'}
        quote_item = dict(common, id='source-line', quantity=10, offers=[
            dict(common, id='q', supplier_id='supplier', quantity=10,
                 price=5, offered_at='2026-11-09T09:00:00')])
        target_item = dict(common, id='target-line', quantity=20, offers=[])
        source = {'id': 'source', 'kind': 'supply', 'items': [quote_item]}
        self.capture([source, {'id': 'target', 'kind': 'supply', 'items': [target_item]}])
        with mock.patch.object(self.views, 'dt', wraps=dt) as clock:
            clock.datetime.now.return_value = dt.datetime(2026, 11, 9, 12)
            self.views.approve_price(self.con, 'target', 'target-line', 'supplier', 'admin')
        approved = self.views.price_history(self.con, dict(target_item, request_id='target'), '2026-11-09T20:00:00')
        self.assertEqual(approved['median'], 5)
        changed_item = dict(target_item, quantity=30)
        self.capture([source, {'id': 'target', 'kind': 'supply', 'items': [changed_item]}], at='2026-11-09T13:00:00')
        changed = self.views.price_history(self.con, dict(changed_item, request_id='target'), '2026-11-09T20:00:00')
        self.assertIsNone(changed['median'])
        self.assertFalse(changed['offers'][0]['quantity_approved'])
        self.assertIn('quantity_mismatch', changed['offers'][0]['exclusion_reasons'])

    def test_quantity_approval_rejects_missing_current_target_item(self):
        self.capture([{'id': 'target', 'kind': 'supply', 'items': []}])
        with self.assertRaises(ValueError):
            self.views.approve_price(self.con, 'target', 'missing', 'supplier', 'admin')

    def test_quantity_approval_is_bound_to_all_target_conditions(self):
        item = {'id': 'line', 'nomenclature': 'n1', 'quantity': 20, 'unit': 'pcs', 'unit_id': 'u1',
                'characteristic': 'c1', 'drawing': 'd1', 'revision': 'r1', 'steel': 's1', 'coating': 'coat1',
                'currency': 'RUB', 'vat_included': True, 'delivery': 'included', 'lot': 'lot1'}
        quote_item = dict(item, id='source-line', quantity=10, offers=[
            dict(item, id='q', supplier_id='supplier', quantity=10, price=5, offered_at='2026-11-09T09:00:00')])
        self.capture([{'id': 'source', 'kind': 'supply', 'items': [quote_item]},
                      {'id': 'target', 'kind': 'supply', 'items': [item]}])
        with mock.patch.object(self.views, 'dt', wraps=dt) as clock:
            clock.datetime.now.return_value = dt.datetime(2026, 11, 9, 12)
            self.views.approve_price(self.con, 'target', 'line', 'supplier', 'admin')
        unchanged = self.views.price_history(self.con, dict(item, request_id='target', quantity=20.0), '2026-11-09T20:00:00')
        self.assertTrue(unchanged['offers'][0]['quantity_approved'])
        for field in ('unit', 'unit_id', 'drawing', 'revision', 'steel', 'coating', 'currency', 'vat_included', 'delivery'):
            with self.subTest(field=field):
                changed = dict(item, request_id='target', **{field: None})
                history = self.views.price_history(self.con, changed, '2026-11-09T20:00:00')
                self.assertFalse(history['offers'][0]['quantity_approved'])
                self.assertIsNone(history['median'])
        overridden = self.views.price_history(self.con, dict(item, request_id='target'), '2026-11-09T20:00:00', quantity=30)
        self.assertFalse(overridden['offers'][0]['quantity_approved'])

    def test_quantity_approval_never_authorizes_unknown_vat(self):
        item = {'id': 'line', 'nomenclature': 'n1', 'quantity': 20, 'unit': 'pcs',
                'currency': 'RUB', 'vat_included': None, 'delivery': 'included'}
        quote_item = dict(item, id='source-line', quantity=10, offers=[
            dict(item, id='q', supplier_id='supplier', quantity=10, price=5, offered_at='2026-11-09T09:00:00')])
        self.capture([{'id': 'source', 'kind': 'supply', 'items': [quote_item]},
                      {'id': 'target', 'kind': 'supply', 'items': [item]}])
        with mock.patch.object(self.views, 'dt', wraps=dt) as clock:
            clock.datetime.now.return_value = dt.datetime(2026, 11, 9, 12)
            self.views.approve_price(self.con, 'target', 'line', 'supplier', 'admin')
        history = self.views.price_history(self.con, dict(item, request_id='target'), '2026-11-09T20:00:00')
        self.assertTrue(history['offers'][0]['quantity_approved'])
        self.assertFalse(history['offers'][0]['eligible'])
        self.assertIn('vat_included_unknown', history['offers'][0]['exclusion_reasons'])

    def test_direct_sales_channel_accepts_the_api_label(self):
        self.capture([{'id': 'a', 'kind': 'tender'}, {'id': 'b', 'kind': 'kp'}])
        report = self.report('sales', channel='Прямые')
        self.assertEqual([row['id'] for row in report['records']], ['b'])

    def test_control_example_retains_seconds_and_excludes_prebaseline_results(self):
        self.capture([{'id': 's', 'kind': 'supply', 'status': 'Отработана',
                       'due': '2026-10-16T18:00:00', 'closed_at': '2026-10-20T09:32:08'}])
        report = self.report()
        detail = self.views.document_detail(self.con, self.config, 's', '2026-11-09T20:00:00')
        self.assertEqual(detail['late_seconds'], 315128)
        self.assertEqual(report['results']['closed'], 0)

    def test_actual_quote_fills_required_line_and_scope_is_audited(self):
        self.capture([{'id': 'r', 'kind': 'supply', 'items': [
            {'id': 'a', 'quantity': 2, 'offers': [{'supplier_id': 's', 'price': 5}]},
            {'id': 'b', 'quantity': 1, 'offers': []}]}])
        first = self.report()['records'][0]
        self.assertEqual((first['required_count'], first['filled_count']), (2, 1))
        with self.assertRaises(ValueError):
            self.views.set_item_scope(self.con, 'r', 'b', False, '', 'admin')
        self.views.set_item_scope(self.con, 'r', 'b', False, 'Outside scope', 'admin')
        self.views.approve_price(self.con, 'r', 'a', 's', 'admin')
        future = self.views.build(self.con, self.config, 'supply', '2026-11-09', '2030-01-01', now='2030-01-01')
        row = future['records'][0]
        self.assertEqual((row['required_count'], row['filled_count']), (1, 1))
        detail = self.views.document_detail(self.con, self.config, 'r', '2030-01-01')
        self.assertEqual(detail['items'][1]['scope']['author'], 'admin')

    def test_report_summaries_do_not_calculate_each_item_price_history(self):
        self.capture([{'id': 'r', 'kind': 'supply', 'items': [
            {'id': 'a', 'quantity': 2, 'offers': [{'supplier_id': 's', 'price': 5}]}]}])
        with mock.patch.object(self.views, 'price_history', side_effect=AssertionError('History must load on demand')):
            report = self.report()
        self.assertEqual(report['records'][0]['items'], [])
        self.assertEqual(report['records'][0]['item_count'], 1)

    def test_report_contains_small_document_descriptors_and_no_source_payload(self):
        self.capture([{'id': 'a', 'kind': 'tender', 'raw_payload': 'private long source text'},
                      {'id': 'r', 'kind': 'supply', 'links': ['a'], 'raw_payload': 'another source payload',
                       'delivery_options': [{'carrier': 'Long description'}],
                       'items': [{'id': 'line', 'quantity': 1, 'offers': [{'supplier_id': 's', 'price': 5}]}]}])
        report = self.report()
        row = report['records'][0]
        self.assertNotIn('raw_payload', row)
        self.assertNotIn('delivery_options', row)
        self.assertTrue(all('raw_payload' not in doc and 'items' not in doc and 'events' not in doc for doc in row['documents']))
        self.assertEqual({doc['id'] for doc in row['documents']}, {'a', 'r'})
        self.assertEqual(row['filled_count'], 1)

    def test_legacy_snapshot_without_summary_still_counts_positive_offers(self):
        self.capture([{'id': 'r', 'kind': 'supply', 'items': [
            {'id': 'line', 'quantity': 2, 'currency': 'RUB', 'vat_included': True,
             'selected_supplier_id': 's', 'offers': [{'supplier_id': 's', 'price': 5}]}]}])
        self.con.execute('UPDATE operations_snapshots SET summary=NULL')
        row = self.report()['records'][0]
        self.assertEqual(row['filled_count'], 1)
        self.assertEqual(row['selected_total'], 10)


if __name__ == '__main__':
    unittest.main()
