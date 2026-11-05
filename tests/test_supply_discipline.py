import datetime as dt
import unittest
from unittest import mock

from app import manual
from app.common import connect,load_rules,merge_rows
from app.engine import Engine
from test_engine import CONFIG
from test_task_efficiency import task


class SupplyDisciplineTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(':memory:');self.addCleanup(self.con.close)
        self.config={**CONFIG,'bitrix24':{'webhook':'https://crm.example.test/rest/1/secret/'},
            'employees':[{**e,**({'b24_user_id':15} if e['login']=='supply' else {})} for e in CONFIG['employees']]}
        self.engine=Engine(self.con,load_rules(),self.config)
        # Legacy configurations retain manual monthly assessments.
        next(i for i in self.engine.rules['positions']['supply']['items'] if i['code']=='exec_discipline')['params'].pop('docs_source',None)
        clock=mock.patch('app.engine.today',return_value=dt.date(2026,11,3));clock.start();self.addCleanup(clock.stop)

    def calc(self,month='2026-11'):
        p=next(i['params'] for i in self.engine.rules['positions']['supply']['items'] if i['code']=='exec_discipline')
        return self.engine.calc_exec_discipline(self.engine.employee('supply'),self.engine._ctx(month),p)

    def efficiency(self,uid=15):
        merge_rows(self.con,'task_efficiency_tasks',[task(i,responsible_id=uid,deadline='2026-11-01T10:00:00' if i==0 else '2099-12-31T23:59:00') for i in range(7)])
        self.con.execute("INSERT INTO dataset_state VALUES(?,'2026-11-01','2026-11-03','now')",('task_efficiency:'+str(uid),))

    def test_supply_monthly_assessment_does_not_average_old_weeks(self):
        self.con.execute("INSERT INTO manual_scores VALUES('supply','2026-11-02','docs',100,'','boss','now')")
        result=self.calc()
        self.assertIsNone(result['components'][0]['fact'])
        self.assertEqual(result['manual']['period'],'month')

    def test_monthly_docs_and_efficiency_are_scored_separately(self):
        manual.set_month_score(self.con,'supply','2026-11','docs',90,'за месяц','boss')
        self.efficiency()
        r=self.calc()
        self.assertEqual([c['fact'] for c in r['components']],[90,.86])
        self.assertTrue(all(not c['informative'] for c in r['components']))
        self.assertIsNone(self.calc('2026-10')['components'][1]['fact'])

    def test_monthly_score_validation_and_reset(self):
        manual.set_month_score(self.con,'supply','2026-11','docs',75,'review','boss')
        self.assertEqual(self.calc()['components'][0]['fact'],75)
        with self.assertRaises(ValueError):manual.set_month_score(self.con,'supply','2026-11','docs',101,'','boss')
        with self.assertRaises(ValueError):manual.set_month_score(self.con,'supply','2026-11','tasks',100,'','boss')
        manual.set_month_score(self.con,'supply','2026-11','docs',None,'','boss')
        self.assertIsNone(self.calc()['components'][0]['fact'])

    def test_other_positions_keep_weekly_rules(self):
        self.con.execute("INSERT INTO manual_scores VALUES('pm','2026-09-07','docs',75,'','boss','now')")
        r=self.engine.calc_exec_discipline(self.engine.employee('pm'),self.engine._ctx('2026-09'),{'criteria':['docs','tasks']})
        self.assertEqual(r['components'][0]['fact'],75)
        self.assertIn('weeks',r['manual'])

    def test_monthly_efficiency_payout_is_proportional(self):
        manual.set_month_score(self.con,'supply','2026-11','docs',100,'','boss')
        self.efficiency()
        item=next(i for i in self.engine.compute('supply','2026-11')['items'] if i['code']=='exec_discipline')
        self.assertEqual((item['amount'],item['pct'],item['payout']),(1500,.86,1290))

    def test_wrong_employee_snapshot_is_not_used(self):
        self.efficiency(uid=99)
        self.assertIsNone(self.calc()['components'][1]['fact'])
