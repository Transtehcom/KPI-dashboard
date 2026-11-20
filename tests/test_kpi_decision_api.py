import unittest

from app.common import connect, db_lock, load_rules, replace_rows
from app.engine import Engine
from app.server import App, Handler
from test_engine import CONFIG


class KpiDecisionApiTest(unittest.TestCase):
    def setUp(self):
        app = App.__new__(App)
        app.con = connect(":memory:")
        self.addCleanup(app.con.close)
        app.lock, app.config, app.rules = db_lock(), CONFIG, load_rules()
        app.engine = Engine(app.con, app.rules, app.config)
        self.user = {"login": "boss", "role": "manager"}
        app.session_user = lambda token: self.user
        self.handler = Handler.__new__(Handler)
        self.handler.app = app
        self.handler.token = lambda: "fixture"
        self.key = dict(emp="pm", month="2026-11", code="qualified_leads", kind="leads", ref="test-lead")
        replace_rows(app.con, "leads", [dict(id="test-lead", emp="pm", title="Тест", qualified_date="2026-11-01",
                                             need=1, budget=0, lpr=1, solvency=1, next_step=1)])

    def post(self, endpoint, value, **changes):
        self.handler.body = lambda: {**self.key, "value": value, **changes}
        return self.handler.api_response("POST", "/api/kpi-" + endpoint, {}).code

    def fact(self):
        items = self.handler.app.engine.compute("pm", "2026-11")["items"]
        return next(item for item in items if item["code"] == "qualified_leads")["components"][0]["fact"]

    def test_approval_exclusion_restore_and_auto(self):
        self.assertEqual(self.fact(), 0)
        for endpoint, value, fact in [("decision", 1, 1), ("exclusion", True, 0),
                                      ("exclusion", False, 1), ("decision", None, 0)]:
            self.assertEqual(self.post(endpoint, value), 200)
            self.assertEqual(self.fact(), fact)

    def test_access_and_candidate_validation(self):
        self.assertEqual(self.post("decision", 1, ref="unknown"), 400)
        self.assertEqual(self.post("exclusion", 1), 400)
        self.user = {"login": "pm", "role": "employee"}
        self.assertEqual(self.post("decision", 1), 403)
        self.assertEqual(self.post("exclusion", True), 403)
