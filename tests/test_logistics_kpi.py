"""The logistics draft is selectable, faithful to its source and financially inactive."""
import unittest

from app import employees
from app.common import connect, load_rules
from app.engine import Engine
from app.server import App


class LogisticsKPIDraftTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.config = {"holidays": [], "employees": [
            {"login": "boss", "name": "Руководитель", "role": "manager"},
            {"login": "pm", "name": "Менеджер", "position": "pm", "role": "employee"}]}
        employees.apply(self.con, self.config, self.rules)

    def profile(self):
        self.assertIn("logistics", self.rules["positions"])
        return self.rules["positions"]["logistics"]

    def engine_with_draft(self):
        self.profile()
        self.config["employees"].append({"login": "logist", "name": "Логист", "position": "logistics", "visible": True})
        return Engine(self.con, self.rules, self.config)

    def test_source_six_items_keep_85_percent_and_pending_compensation(self):
        p = self.profile()
        self.assertEqual(p["title"], "Специалист отдела логистики")
        self.assertTrue(p["draft"])
        self.assertIsNone(p["salary"])
        self.assertIsNone(p["kpi_bonus"])
        self.assertEqual([i["weight"] for i in p["items"]], [.20, .20, .10, .15, .10, .10])
        self.assertEqual([i["n"] for i in p["items"]], list(range(1, 7)))
        self.assertEqual(len({i["code"] for i in p["items"]}), 6)
        self.assertIn("85%", " ".join(p["setup_issues"]))
        self.assertIn("X", p["items"][4]["description"])
        self.assertIn("стоимость повреждений", p["items"][3]["description"].lower())
        self.assertIn("15 минут", p["items"][5]["description"])

    def test_selectable_profile_does_not_assign_any_employee(self):
        before = [dict(r) for r in self.con.execute("SELECT * FROM employees")]
        positions = employees.listing(self.con, self.rules)["positions"]
        self.assertIn("logistics", [p["key"] for p in positions])
        p = next(p for p in positions if p["key"] == "logistics")
        self.assertTrue(p["draft"])
        self.assertEqual(before, [dict(r) for r in self.con.execute("SELECT * FROM employees")])
        self.assertNotIn("logistics", [e.get("position") for e in self.config["employees"]])

    def test_original_80_percent_statement_remains_an_unresolved_payout_rule(self):
        self.assertTrue(any("80%" in issue for issue in self.profile()["setup_issues"]),
                        "The source's 80% completion statement conflicts with the table and needs an explicit note")

    def test_draft_never_pays_even_with_positive_bonus_and_pay_when_no_data(self):
        engine = self.engine_with_draft()
        self.profile()["kpi_bonus"] = 10000
        self.rules["payout"]["pay_when_no_data"] = True
        for month in ("2026-09", "2026-11"):
            with self.subTest(month=month):
                r = engine.compute("logist", month)
                self.assertTrue(r["draft"])
                self.assertEqual(r["payout"], 0)
                self.assertEqual(r["met_count"], 0)
                self.assertEqual(len(r["items"]), 6)
                self.assertTrue(r["warnings"])
                self.assertTrue(all(i["status"] == "draft" and i["pct"] is None and i["payout"] == 0 for i in r["items"]))
                self.assertEqual(engine.summary("logist", month)["payout"], 0)

    def test_draft_is_excluded_from_team_performance_and_fund(self):
        baseline = Engine(self.con, self.rules, self.config).overview("2026-10")["totals"]
        engine = self.engine_with_draft()
        self.profile()["kpi_bonus"] = 10000
        overview = engine.overview("2026-10")
        self.assertEqual(overview["totals"], baseline)
        group = next(g for g in overview["groups"] if g["position"] == "logistics")
        self.assertTrue(group["employees"][0]["draft"])

    def test_bootstrap_exposes_draft_rules_and_all_setup_issues(self):
        profile = self.profile()
        app = App.__new__(App)
        app.config, app.rules, app.con = self.config, self.rules, self.con
        app.engine = Engine(self.con, self.rules, self.config)
        boot = app.bootstrap(self.config["employees"][0])
        p = boot["positions"]["logistics"]
        self.assertTrue(p["draft"])
        self.assertEqual(p["setup_issues"], profile["setup_issues"])
        self.assertEqual(len(p["items"]), 6)
        self.assertIsNone(p["salary"])
        self.assertIsNone(p["kpi_bonus"])


if __name__ == "__main__":
    unittest.main()
