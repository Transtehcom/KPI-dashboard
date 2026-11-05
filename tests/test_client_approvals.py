import datetime as dt
import unittest
from unittest.mock import patch

from app import manual
from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from test_engine import CONFIG


class ClientApprovalTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con,load_rules(),CONFIG)
        self.client = {"id":"co42","emp":"lead","name":"Компания","need":1,"strategy":1}
        replace_rows(self.con,"clients",[self.client])

    def calc(self):
        with patch("app.engine.today",return_value=dt.date(2026,11,5)):
            return self.engine.calc_base_development(self.engine.employee("lead"),self.engine._ctx("2026-11"),{"active_clients":7})

    def test_only_explicit_no_excludes_client_and_rejected_client_stays_reviewable(self):
        self.assertEqual(self.calc()["components"][0]["fact"],1)
        for approval, counted in ((1,1),(0,0),(None,1)):
            manual.set_client_approval(self.con,"lead","2026-11","co42",approval,"boss")
            result = self.calc()
            self.assertEqual(result["components"][0]["fact"],counted)
            self.assertEqual(result["extra"][0]["client_approvals"][0]["value"],approval)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM client_approvals").fetchone()[0],0)

    def test_approval_survives_source_refresh_and_is_scoped_to_month_and_owner(self):
        manual.set_client_approval(self.con,"lead","2026-11","co42",0,"boss")
        replace_rows(self.con,"clients",[self.client])
        self.assertEqual(self.calc()["components"][0]["fact"],0)
        self.assertEqual(manual.client_approvals(self.con,"lead","2026-10"),{})
        self.assertEqual(manual.client_approvals(self.con,"tender","2026-11"),{})
        with self.assertRaises(ValueError):
            manual.set_client_approval(self.con,"tender","2026-11","co42",1,"boss")

    def test_previously_rejected_client_can_be_qualified_in_the_next_month(self):
        self.con.execute("INSERT INTO clients_monthly VALUES('2026-10','co42','lead','Компания',1,1)")
        manual.set_client_approval(self.con,"lead","2026-10","co42",0,"boss")
        self.assertEqual(self.calc()["components"][0]["fact"],1)
