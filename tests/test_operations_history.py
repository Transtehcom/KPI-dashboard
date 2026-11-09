import datetime as dt
import unittest

from app.common import connect
from app import operations


class OperationsHistoryTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        operations.ensure_schema(self.con)

    def capture(self, at, entities, events=()):
        operations.capture(self.con, 'onec', entities, list(events), at)

    def test_midnight_means_end_of_day_and_timezones_keep_seconds(self):
        self.assertEqual(operations.deadline('2026-10-29T00:00:00'), dt.datetime(2026, 10, 29, 23, 59, 59, 999999))
        self.assertEqual(operations.deadline('2026-10-29T16:00:08'), dt.datetime(2026, 10, 29, 16, 0, 8))
        self.assertEqual(operations.local_datetime('2026-10-29T11:00:08Z'), dt.datetime(2026, 10, 29, 16, 0, 8))
        self.assertIsNone(operations.deadline('0001-01-01T00:00:00'))

    def test_capture_preserves_prior_snapshot_and_avoids_unchanged_versions(self):
        row = {'id':'onec:b:supply:1','kind':'supply','status':'ВРаботе','due':'2026-11-10T18:00:00'}
        self.capture('2026-11-09T10:00:00', [row])
        self.capture('2026-11-09T11:00:00', [row])
        self.assertEqual(len(operations.versions(self.con,'onec')), 1)
        self.capture('2026-11-09T12:00:00', [{**row,'status':'Отработана'}])
        self.assertEqual(operations.snapshot(self.con,'onec','2026-11-09T11:30:00')[0]['status'],'ВРаботе')
        self.assertEqual(operations.snapshot(self.con,'onec')[0]['status'],'Отработана')
        self.assertEqual(operations.snapshot(self.con,'onec','2026-11-09T09:00:00'), [])

    def test_successful_removal_is_historical_and_reappearance_is_preserved(self):
        row = {'id':'1','kind':'supply'}
        self.capture('2026-11-09T10:00:00',[row])
        self.capture('2026-11-09T11:00:00',[])
        self.assertEqual(operations.snapshot(self.con,'onec'), [])
        self.assertEqual(len(operations.snapshot(self.con,'onec','2026-11-09T10:30:00')),1)
        self.capture('2026-11-09T12:00:00',[row])
        self.assertEqual(len(operations.snapshot(self.con,'onec')),1)

    def test_failure_preserves_data_and_last_success_with_independent_sources(self):
        self.capture('2026-11-09T10:00:00',[{'id':'1','kind':'supply'}])
        operations.mark_failure(self.con,'onec','source unavailable','2026-11-09T11:00:00')
        state=operations.freshness(self.con,'2026-11-09T11:31:00')
        self.assertTrue(state['onec']['stale'])
        self.assertEqual(state['onec']['updated_at'],'2026-11-09T10:00:00')
        self.assertTrue(state['onec']['error'])
        self.assertIsNone(state['bitrix']['updated_at'])
        self.assertEqual(len(operations.snapshot(self.con,'onec')),1)

    def test_event_edits_cannot_leak_into_old_cutoff(self):
        event={'id':'state1','entity_id':'1','at':'2026-11-09T10:05:00','author':'anna','status':'Действует'}
        self.capture('2026-11-09T11:00:00',[{'id':'1'}],[event])
        self.capture('2026-11-09T12:00:00',[{'id':'1'}],[{**event,'author':'maria'}])
        self.assertEqual(operations.events(self.con,'onec','2026-11-09T11:30:00')[0]['author'],'anna')
        self.assertEqual(operations.events(self.con,'onec')[0]['author'],'maria')

    def test_repeat_import_does_not_rejuvenate_quote(self):
        row={'id':'1','kind':'supply','items':[{'id':'row1','offers':[{'supplier_id':'a','price':10}]}]}
        self.capture('2026-11-09T10:00:00',[row])
        self.capture('2026-11-09T11:00:00',[row])
        first=operations.snapshot(self.con,'onec')[0]['items'][0]['offers'][0]
        self.assertEqual(first['first_seen_at'],'2026-11-09T10:00:00')
        self.assertTrue(first['is_initial'])
        row['items'][0]['offers'][0]['price']=12
        self.capture('2026-11-09T12:00:00',[row])
        changed=operations.snapshot(self.con,'onec')[0]['items'][0]['offers'][0]
        self.assertEqual(changed['first_seen_at'],'2026-11-09T12:00:00')
        self.assertFalse(changed['is_initial'])

    def test_manual_exclusion_history_is_append_only(self):
        columns={r['name'] for r in self.con.execute('PRAGMA table_info(operations_item_scope)')}
        self.assertTrue({'id','request_id','item_id','included','reason','author','created_at'} <= columns)

    def test_bitrix_activity_camelcase_bindings_and_quote_summary(self):
        config={'employees':[{'login':'anna','name':'Anna','b24_user_id':15}]}
        activity={'ID':'7','RESPONSIBLE_ID':'15','AUTHOR_ID':'15','SUBJECT':'Plan','CREATED':'2026-11-09T10:00:00',
                  'OWNER_TYPE_ID':'2','OWNER_ID':'9','BINDINGS':[{'entityTypeId':1,'entityId':8}]}
        operations.capture_bitrix(self.con,config,'2026-11-09T11:00:00',{'activities':[activity]})
        row=operations.get_entity(self.con,'bitrix','bitrix:activity:7')
        self.assertEqual(set(row['links']),{'bitrix:crm_deal:9','bitrix:crm_lead:8'})
        row={'id':'r','kind':'supply','items':[{'id':'i','offers':[{'supplier_id':'s','price':5,'selected':True}],'quantity':2}]}
        self.capture('2026-11-09T11:00:00',[row])
        lean=operations.snapshot(self.con,'onec',lean=True)[0]
        self.assertTrue(lean['items'][0]['has_price'])
        self.assertNotIn('offers',lean['items'][0])
        self.assertNotIn('items',operations.versions(self.con,'onec',lean=True)[0])


if __name__=='__main__':
    unittest.main()
