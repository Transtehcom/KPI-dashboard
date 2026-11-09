import datetime as dt
import unittest

from app.common import connect, db_lock, load_rules
from app import operations
from app.server import App, Handler


class OperationsAPITest(unittest.TestCase):
    def setUp(self):
        self.con=connect(':memory:')
        self.addCleanup(self.con.close)
        self.config={'employees':[{'login':'boss','name':'Boss','role':'manager'},
                                  {'login':'worker','name':'Worker','position':'supply','role':'employee'}]}
        self.app=App.__new__(App)
        self.app.con,self.app.config,self.app.rules=self.con,self.config,load_rules()
        self.app.lock=db_lock()
        self.user=self.config['employees'][0]
        self.app.session_user=lambda token:self.user
        self.handler=Handler.__new__(Handler)
        self.handler.app=self.app
        self.handler.token=lambda:'fixture'
        self.handler.body=lambda:{}
        self.today=dt.date.today().isoformat()
        self.entity={'id':'onec:b:supply:r','kind':'supply','owner':'worker','status':'ВРаботе',
                     'date':self.today,'due':self.today+'T18:00:00','items':[{'id':'row1','name':'Item','offers':[]}]}
        operations.capture(self.con,'onec',[self.entity],[],self.today+'T08:00:00')
        self.con.commit()

    def request(self,path='/api/operations',query=None,body=None):
        self.handler.body=lambda:body or {}
        return self.handler.api_response('POST' if body is not None else 'GET',path,query or {'kind':'supply','date_from':self.today,'date_to':self.today})

    def test_report_is_available_to_manager_only(self):
        response=self.request()
        self.assertEqual(response.code,200)
        self.assertEqual(response.data['kind'],'supply')
        self.user=self.config['employees'][1]
        self.assertEqual(self.request().code,403)
        self.assertEqual(self.request('/api/operations/document',{'id':self.entity['id']}).code,403)

    def test_invalid_date_kind_and_employee_are_rejected(self):
        for query in ({'kind':'bad'},{'kind':'supply','date_from':'bad'},
                      {'kind':'supply','date_from':'2026-12-01','date_to':'2026-01-01'},
                      {'kind':'supply','emp':'unknown'}):
            with self.subTest(query=query):
                self.assertEqual(self.request(query=query).code,400)

    def test_scope_is_audited_and_reason_is_required(self):
        body={'request_id':self.entity['id'],'item_id':'row1','included':False,'reason':''}
        self.assertEqual(self.request('/api/operations/item-scope',body=body).code,400)
        body['reason']='Client cancelled'
        self.assertEqual(self.request('/api/operations/item-scope',body=body).code,200)
        row=self.con.execute('SELECT * FROM operations_item_scope').fetchone()
        self.assertEqual(row['author'],'boss')
        self.assertEqual(row['included'],0)
        self.user=self.config['employees'][1]
        self.assertEqual(self.request('/api/operations/item-scope',body=body).code,403)

    def test_unknown_source_row_cannot_be_modified(self):
        response=self.request('/api/operations/item-scope',body={'request_id':self.entity['id'],'item_id':'other','included':False,'reason':'x'})
        self.assertEqual(response.code,404)
        self.assertEqual(self.con.execute('SELECT count(*) FROM operations_item_scope').fetchone()[0],0)

    def test_document_historical_cutoff_does_not_return_current_version(self):
        operations.capture(self.con,'onec',[{**self.entity,'title':'Future version'}],[],'2099-01-01T10:00:00')
        response=self.request('/api/operations/document',{'id':self.entity['id'],'date_to':self.today})
        self.assertEqual(response.code,200)
        self.assertNotEqual(response.data['document'].get('title'),'Future version')


if __name__=='__main__':
    unittest.main()
