import unittest
from unittest import mock

from app.common import connect
from app import sync,operations


class OperationsSyncTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(':memory:')
        self.addCleanup(self.con.close)
        self.config={'mode':'real','employees':[],'holidays':[],'operations':{'enabled':True},
                     'onec':{'base_url':'https://example.test','datasets':{}}}
        self.entity={'id':'r','kind':'supply','status':'ВРаботе'}

    def run_sync(self,legacy_error=False,operations_error=False):
        with mock.patch('app.sync.OneC') as client, \
             mock.patch('app.sync.onec_sales_register.fetch',return_value=[]), \
             mock.patch('app.sync.onec_money.fetch',return_value=None), \
             mock.patch('app.sync.onec_operations.fetch') as operational:
            client.return_value.fetch.side_effect=RuntimeError('Legacy failure') if legacy_error else None
            client.return_value.fetch.return_value={}
            operational.return_value={'entities':[self.entity],'events':[],'errors':{'source':'unavailable'} if operations_error else {},'warnings':[]}
            sync.run(self.con,self.config)
            operational.assert_called_once()

    def test_new_source_is_independent_of_legacy_kpi_fetch(self):
        self.run_sync(legacy_error=True)
        self.assertEqual(operations.get_entity(self.con,'onec','r')['status'],'ВРаботе')

    def test_source_captures_when_no_legacy_datasets_are_written(self):
        self.run_sync()
        self.assertEqual(operations.get_entity(self.con,'onec','r')['id'],'r')

    def test_failed_fetch_preserves_previous_source_and_marks_error(self):
        operations.capture(self.con,'onec',[self.entity],[],'2026-11-09T10:00:00')
        self.run_sync(operations_error=True)
        self.assertEqual(operations.get_entity(self.con,'onec','r')['status'],'ВРаботе')
        state=self.con.execute("SELECT * FROM operations_sources WHERE source='onec'").fetchone()
        self.assertEqual(state['updated_at'],'2026-11-09T10:00:00')
        self.assertTrue(state['error'])


if __name__=='__main__':
    unittest.main()
