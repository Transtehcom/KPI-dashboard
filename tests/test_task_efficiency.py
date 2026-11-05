import datetime as dt
import json
import unittest


def task(i, **kw):
    result={'id':str(i),'responsible_id':15,'creator_id':80,'created':'2026-10-01T10:00:00',
        'deadline':'2026-11-10T17:00:00','closed':None,'status':'2','accomplices':'[]','history':'[]','title':str(i)}
    result.update(kw);return result


class TaskEfficiencyTest(unittest.TestCase):
    def calculate(self,rows,month='2026-11'):
        from app.task_efficiency import calculate
        return calculate(rows,15,month,dt.datetime(2026,11,3,16,30))

    def test_formula_10_tasks_two_remarks_is_80_percent(self):
        rows=[task(i,deadline='2026-11-01T10:00:00' if i<2 else '2026-11-10T17:00:00') for i in range(10)]
        result=self.calculate(rows)
        self.assertEqual((result['total'],result['violations'],result['percent']),(10,2,80))

    def test_zero_tasks_defaults_to_100(self):
        self.assertEqual(self.calculate([])['percent'],100)

    def test_accomplice_included_self_assigned_excluded_and_deduplicated(self):
        rows=[task(1,responsible_id=18,creator_id=18,accomplices='[15]'),task(2,creator_id=15),
              task(3,accomplices='[15]')]
        self.assertEqual(self.calculate(rows)['total'],2)

    def test_old_open_task_and_old_overdue_continue_in_next_month(self):
        r=self.calculate([task(1,deadline='2026-10-29T17:00:00',closed='2026-11-02T12:00:00',status='5')])
        self.assertEqual((r['total'],r['violations'],r['completed']),(1,1,1))

    def test_multiple_missed_deadlines_count_as_multiple_remarks(self):
        h=[['2026-10-15T12:00:00','DEADLINE','2026-10-10T17:00:00','2026-10-20T17:00:00'],
           ['2026-10-25T12:00:00','DEADLINE','2026-10-20T17:00:00','2026-11-10T17:00:00']]
        r=self.calculate([task(1,history=json.dumps(h))],'2026-10')
        self.assertEqual((r['total'],r['violations'],r['percent']),(1,2,0))

    def test_deadline_moved_before_expiry_not_a_remark(self):
        h=[['2026-10-09T12:00:00','DEADLINE','2026-10-10T17:00:00','2026-11-10T17:00:00']]
        self.assertEqual(self.calculate([task(1,history=json.dumps(h))],'2026-10')['violations'],0)

    def test_finished_on_deadline_and_created_after_deadline_are_not_late(self):
        a=task(1,deadline='2026-11-02T12:00:00',closed='2026-11-02T12:00:00',status='5')
        b=task(2,created='2026-11-02T14:00:00',deadline='2026-11-02T12:00:00')
        self.assertEqual(self.calculate([a,b])['violations'],0)

    def test_removed_participant_is_excluded_as_in_b24_widget(self):
        h=[['2026-11-01T00:00:00','RESPONSIBLE_ID','15','18']]
        a=task(1,responsible_id=18,history=json.dumps(h))
        self.assertEqual(self.calculate([a],'2026-10')['total'],0)
        self.assertEqual(self.calculate([a],'2026-11')['total'],0)

    def test_status_changes_do_not_duplicate_same_deadline(self):
        h=[['2026-11-02T12:00:00','STATUS','2','3']]
        r=self.calculate([task(1,deadline='2026-11-01T10:00:00',status='3',history=json.dumps(h))])
        self.assertEqual(r['violations'],1)

    def test_missing_source_is_not_misrepresented_as_100_percent(self):
        from app.common import connect
        from app.task_efficiency import report
        c=connect(':memory:');self.addCleanup(c.close)
        self.assertIsNone(report(c,15,'2026-11'))
        c.execute("INSERT INTO dataset_state VALUES('task_efficiency:15','2026-05-01','2026-11-03','now')")
        self.assertEqual(report(c,15,'2026-11')['percent'],100)
        self.assertIsNone(report(c,99,'2026-11'))

    def source(self,broken=False,paginated=False):
        from app.bitrix24 import Bitrix24
        class Source:
            ekb=staticmethod(Bitrix24.ekb)
            def log(self,_):pass
            def list_all(self,method,params,key=None):
                if method=='tasks.task.history.list':
                    return [{'createdDate':'2026-11-02T12:00:00+05:00','field':'DEADLINE',
                             'value':{'from':'1793592000','to':'1794283200'}}]
                ids=['1'] if 'RESPONSIBLE_ID' in params['filter'] else ['1','2']
                return [{'id':i,'responsibleId':'15' if i=='1' else '18','createdBy':'80','createdDate':'2026-10-01T10:00:00+05:00',
                         'deadline':'2026-11-10T17:00:00+05:00','status':'2','accomplices':['15']} for i in ids]
            def call(self,method,params):
                return {'result':{'result':{i:{'list':[]} for i in params['cmd']},
                    'result_error':{'1':{'error':'DENIED'}} if broken else {},
                    'result_next':{'2':50} if paginated else {}}}
        return Source()

    def test_fetch_includes_assisting_tasks_once_and_reads_all_history_pages(self):
        from app.task_efficiency import fetch
        r=fetch(self.source(paginated=True),[{'position':'supply','b24_user_id':15}])
        self.assertEqual([t['id'] for t in r],['1','2'])
        self.assertEqual(len(json.loads(r[1]['history'])),1)

    def test_incomplete_history_rejects_refresh(self):
        from app.task_efficiency import fetch
        from app.bitrix24 import B24Error
        with self.assertRaises(B24Error):
            fetch(self.source(broken=True),[{'position':'supply','b24_user_id':15}])

    def test_fetch_discovers_former_assignee_before_first_import(self):
        from app.task_efficiency import fetch
        base=self.source()
        class Source(type(base)):
            def list_all(self,method,params,key=None):
                if 'filter' in params and '>=CHANGED_DATE' in params['filter']:
                    return [{'id':'3','responsibleId':'18','createdBy':'80','createdDate':'2026-10-01T10:00:00+05:00',
                             'deadline':None,'status':'2','accomplices':[]}]
                return super().list_all(method,params,key)
            def call(self,method,params):
                r=super().call(method,params)
                if '3' in params['cmd']:
                    r['result']['result']['3']['list']=[{'createdDate':'2026-11-01T00:00:00+05:00',
                        'field':'RESPONSIBLE_ID','value':{'from':'15','to':'18'}}]
                return r
        rows=fetch(Source(),[{'position':'supply','b24_user_id':15}])
        self.assertEqual({r['id'] for r in rows},{'1','2','3'})
