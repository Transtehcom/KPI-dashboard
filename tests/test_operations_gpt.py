import datetime as dt
import unittest
from unittest import mock

from app.common import connect,load_rules
from app.gpt_access import Access
from app.gpt_privacy import PrivacyContext
from app.gpt_tools import DataTools
from app import operations


class OperationsGPTTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(':memory:')
        self.addCleanup(self.con.close)
        self.config={'employees':[{'login':'boss','name':'Boss','role':'manager'},
                                  {'login':'worker','name':'Worker','position':'supply','role':'employee'}]}
        self.privacy=PrivacyContext()

    def test_staff_is_denied_before_operational_report_read(self):
        tools=DataTools(self.con,self.config,load_rules(),Access(self.config,'worker'))
        with mock.patch('app.operations_views.build') as read:
            with self.assertRaises(ValueError):
                tools.execute('operations_report','{"kind":"sales"}','',None,self.privacy)
            read.assert_not_called()

    def test_admin_gets_deterministic_summary_with_sources(self):
        today=dt.date.today().isoformat()
        operations.capture(self.con,'onec',[{'id':'r','kind':'supply','owner':'worker','status':'ВРаботе','date':today}],[],today+'T08:00:00')
        tools=DataTools(self.con,self.config,load_rules(),Access(self.config,'boss'))
        result=tools.execute('operations_report','{"kind":"supply"}','',None,self.privacy)
        self.assertEqual(result['kind'],'supply')
        self.assertTrue(result['source'])
        self.assertEqual(result['total_records'],1)
        self.assertIn('coverage',result)


if __name__=='__main__':
    unittest.main()
