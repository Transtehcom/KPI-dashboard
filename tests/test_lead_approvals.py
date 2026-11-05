import datetime as dt
import unittest
from unittest.mock import patch

from app import manual
from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from test_engine import CONFIG


class LeadApprovalTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con,load_rules(),CONFIG)
        self.full = {"id":"lead42","emp":"pm","title":"Лид","qualified_date":"2026-11-01",
                     "need":1,"budget":1,"lpr":1,"solvency":1,"next_step":1}
        replace_rows(self.con,"leads",[self.full])

    def calc(self):
        with patch("app.engine.today",return_value=dt.date(2026,11,5)):
            return next(i for i in self.engine.compute("pm","2026-11")["items"] if i["code"]=="qualified_leads")

    def test_empty_and_yes_count_but_no_excludes_and_can_be_reset(self):
        self.assertEqual(self.calc()["components"][0]["fact"],1)
        for value, count in ((0,0),(1,1),(None,1)):
            manual.set_lead_approval(self.con,"pm","2026-11","lead42",value,"boss")
            item = self.calc()
            self.assertEqual(item["components"][0]["fact"],count)
            self.assertEqual(len(item["details"]["rows"]),1)
            self.assertEqual(item["details"]["lead_approval"]["rows"][0]["value"],value)
        self.assertEqual(self.calc()["payout"],100)

    def test_yes_does_not_qualify_an_incomplete_lead_and_no_survives_refresh(self):
        manual.set_lead_approval(self.con,"pm","2026-11","lead42",1,"boss")
        replace_rows(self.con,"leads",[{**self.full,"budget":0,"qualified_date":None}])
        self.assertEqual(self.calc()["components"][0]["fact"],0)
        self.assertEqual(len(self.calc()["details"]["rows"]),1)
        manual.set_lead_approval(self.con,"pm","2026-11","lead42",0,"boss")
        replace_rows(self.con,"leads",[self.full])
        self.assertEqual(self.calc()["components"][0]["fact"],0)
        self.assertEqual(self.con.execute("SELECT qualified_date FROM leads").fetchone()[0],"2026-11-01")

    def test_approval_is_scoped_to_employee_and_month(self):
        manual.set_lead_approval(self.con,"pm","2026-10","lead42",0,"boss")
        self.assertEqual(self.calc()["components"][0]["fact"],1)
        with self.assertRaises(ValueError):
            manual.set_lead_approval(self.con,"lead","2026-11","lead42",0,"boss")
