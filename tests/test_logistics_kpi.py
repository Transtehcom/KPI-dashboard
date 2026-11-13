"""Confirmed logistics delivery rules pay while unresolved items remain drafts."""
import json
import unittest
from unittest.mock import patch

from app import employees, manual
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

    def test_six_items_use_approved_weights_and_compensation(self):
        p = self.profile()
        self.assertEqual(p["title"], "Специалист отдела логистики")
        self.assertFalse(p["draft"])
        self.assertEqual([item["n"] for item in p["items"] if item.get("draft")], [5, 6])
        self.assertEqual(p["salary"], 70000)
        self.assertEqual(p["kpi_bonus"], 10000)
        self.assertEqual([i["weight"] for i in p["items"]], [.20, .20, .15, .15, .15, .15])
        self.assertAlmostEqual(sum(i["weight"] for i in p["items"]), 1)
        self.assertEqual([i["n"] for i in p["items"]], list(range(1, 7)))
        self.assertEqual(len({i["code"] for i in p["items"]}), 6)
        self.assertFalse(any("85%" in issue for issue in p["setup_issues"]))
        self.assertFalse(any("Оклад и размер премии" in issue for issue in p["setup_issues"]))
        self.assertIn("X", p["items"][4]["description"])
        self.assertIn("руководитель", p["items"][3]["description"].lower())
        self.assertIn("15 минут", p["items"][5]["description"])

    def test_selectable_profile_does_not_assign_any_employee(self):
        before = [dict(r) for r in self.con.execute("SELECT * FROM employees")]
        positions = employees.listing(self.con, self.rules)["positions"]
        self.assertIn("logistics", [p["key"] for p in positions])
        p = next(p for p in positions if p["key"] == "logistics")
        self.assertFalse(p["draft"])
        self.assertEqual(before, [dict(r) for r in self.con.execute("SELECT * FROM employees")])
        self.assertNotIn("logistics", [e.get("position") for e in self.config["employees"]])

    def test_original_80_percent_statement_remains_an_unresolved_payout_rule(self):
        self.assertTrue(any("80%" in issue for issue in self.profile()["setup_issues"]),
                        "The source's 80% completion statement conflicts with the table and needs an explicit note")

    def test_missing_delivery_data_and_draft_items_never_create_payout(self):
        engine = self.engine_with_draft()
        self.profile()["kpi_bonus"] = 10000
        self.rules["payout"]["pay_when_no_data"] = True
        for month in ("2026-09", "2026-11"):
            with self.subTest(month=month):
                r = engine.compute("logist", month)
                self.assertFalse(r["draft"])
                self.assertEqual(r["salary"], 70000)
                self.assertEqual(r["kpi_bonus"], 10000)
                self.assertEqual([item["amount"] for item in r["items"]], [2000, 2000, 1500, 1500, 1500, 1500])
                self.assertEqual(sum(item["amount"] for item in r["items"]), 10000)
                self.assertEqual(r["payout"], 0)
                self.assertEqual(r["met_count"], 0)
                self.assertEqual(len(r["items"]), 6)
                self.assertTrue(r["warnings"])
                self.assertIsNone(r["items"][0]["pct"])
                self.assertEqual(r["items"][1]["payout"], 0)
                drafts = [r["items"][n - 1] for n in (5, 6)]
                self.assertTrue(all(i["status"] == "draft" and i["pct"] is None and i["payout"] == 0 for i in drafts))
                self.assertEqual(r["items"][3]["status"], "awaiting")
                self.assertEqual(engine.summary("logist", month)["payout"], 0)

    def test_partially_active_profile_enters_team_fund(self):
        baseline = Engine(self.con, self.rules, self.config).overview("2026-10")["totals"]
        engine = self.engine_with_draft()
        self.profile()["kpi_bonus"] = 10000
        overview = engine.overview("2026-10")
        self.assertEqual(overview["totals"]["payout"], baseline["payout"])
        self.assertEqual(overview["totals"]["fund"], baseline["fund"] + 10000)
        self.assertEqual(overview["totals"]["employees"], baseline["employees"] + 1)
        group = next(g for g in overview["groups"] if g["position"] == "logistics")
        self.assertFalse(group["employees"][0]["draft"])

    def test_cargo_safety_monthly_manager_score_drives_proportional_payout(self):
        engine = self.engine_with_draft()
        for score, payout in ((50, 750), (100, 1500), (0, 0)):
            with self.subTest(score=score):
                manual.set_month_score(self.con, "logist", "2026-11", "cargo_safety", score, "Проверено", "boss")
                item = engine.compute("logist", "2026-11")["items"][3]
                self.assertEqual((item["pct"], item["payout"]), (score / 100, payout))
                self.assertEqual(item["manual"]["criteria"], ["cargo_safety"])
                self.assertEqual(item["manual"]["scores"]["cargo_safety"]["comment"], "Проверено")
        self.assertIsNone(engine.compute("logist", "2026-10")["items"][3]["pct"])
        manual.set_month_score(self.con, "logist", "2026-11", "cargo_safety", None, "", "boss")
        item = engine.compute("logist", "2026-11")["items"][3]
        self.assertEqual((item["status"], item["pct"], item["payout"]), ("awaiting", None, 0))
        for invalid in (-1, 101, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                manual.set_month_score(self.con, "logist", "2026-11", "cargo_safety", invalid, "", "boss")

    def test_cargo_safety_exposes_ten_manual_incident_criteria(self):
        engine = self.engine_with_draft()
        item = engine.compute("logist", "2026-11")["items"][3]
        self.assertIn("cargo_checks", item["manual"])
        rows = item["manual"]["cargo_checks"]["rows"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["title"], "Повреждения груза")
        self.assertEqual(rows[-1]["title"], "Претензии заказчика по транспортным причинам")
        self.assertTrue(all(r["cases"] is None and r["damage"] is None and r["comment"] == "" for r in rows))

    def test_cargo_auto_score_manual_override_zero_and_clear(self):
        from app import logistics_cargo

        engine = self.engine_with_draft()
        rows = [{"criterion": key, "cases": 0, "damage": 0} for key, _ in logistics_cargo.CRITERIA]
        rows[0].update(cases=1, damage=100000)
        logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
        logistics_cargo.save_summary(self.con, "logist", "2026-11", 1000000, "Автоматический расчёт", "boss")
        item = engine.compute("logist", "2026-11")["items"][3]
        self.assertEqual(item["pct"], .95)
        self.assertEqual(item["payout"], 1425)
        self.assertIsNone(item["manual"]["scores"]["cargo_safety"]["score"])
        self.assertEqual(item["manual"]["scores"]["cargo_safety"]["comment"], "Автоматический расчёт")
        for override, payout in ((50, 750), (0, 0)):
            manual.set_month_score(self.con, "logist", "2026-11", "cargo_safety", override, "Корректировка", "boss")
            item = engine.compute("logist", "2026-11")["items"][3]
            self.assertEqual((item["pct"], item["payout"]), (override / 100, payout))
        manual.set_month_score(self.con, "logist", "2026-11", "cargo_safety", None, "", "boss")
        self.assertEqual(engine.compute("logist", "2026-11")["items"][3]["payout"], 1425)

    def test_cargo_auto_half_percent_rounding_matches_payout(self):
        from app import logistics_cargo

        engine = self.engine_with_draft()
        rows = [{"criterion": key, "cases": 0, "damage": 0} for key, _ in logistics_cargo.CRITERIA]
        logistics_cargo.save_summary(self.con, "logist", "2026-11", 1000000, "", "boss")
        for damage, whole_pct in ((862250, 15), (729250, 29), (463250, 57), (453750, 58),
                                  (862725, 15), (729725, 29)):
            with self.subTest(damage=damage):
                rows[0]["damage"] = damage
                logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
                item = engine.compute("logist", "2026-11")["items"][3]
                self.assertEqual(item["payout"], whole_pct * 15)
                self.assertEqual(item["manual"]["cargo_checks"]["auto_score"], whole_pct)

    def test_discipline_previews_required_forwarder_fields_without_accruing_bonus(self):
        engine = self.engine_with_draft()
        self.con.execute("INSERT INTO dataset_state(dataset,date_from,date_to,updated_at) VALUES(?,?,?,?)",
                         ("forwarder_orders", "2026-09-01", "2026-11-30", "2026-11-11"))
        rows = [
            ("good", "logist", "2026-10-01", "Выдан", 0, "2026-10-02", "2026-10-03", "2026-10-02", "2026-10-03"),
            ("missing", "logist", "2026-10-02", "ВПути", 0, "2026-10-02", "2026-10-03", None, "0001-01-01"),
            ("deleted", "logist", "2026-10-03", None, 1, None, None, None, None),
            ("other", "pm", "2026-10-03", None, 0, None, None, None, None),
            ("outside", "logist", "2026-09-30", None, 0, None, None, None, None),
            ("status", "logist", "2026-10-31", " ", 0, "2026-10-02", "2026-10-03", "2026-10-02", "2026-10-03"),
        ]
        self.con.executemany("INSERT INTO forwarder_orders(id,emp,dt,status,deleted,dop,dpp,dof,dfp) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        result = engine.compute("logist", "2026-10")
        item = next(i for i in result["items"] if i["code"] == "logistics_discipline")
        self.assertEqual(item["components"][0]["fact"], 1 / 3)
        self.assertTrue(item["components"][0]["informative"])
        self.assertEqual(len(item["details"]["rows"]), 3)
        self.assertEqual(item["details"]["rows"][1][-2], "ДОФ, ДФП")
        self.assertEqual(item["details"]["rows"][2][-2], "Статус")
        self.assertEqual(item["status"], "draft")
        self.assertIsNone(item["pct"])
        self.assertEqual(item["payout"], 0)

    def test_discipline_empty_or_unsynced_source_is_unknown(self):
        engine = self.engine_with_draft()
        item = next(i for i in engine.compute("logist", "2026-10")["items"] if i["code"] == "logistics_discipline")
        self.assertIsNone(item["components"][0]["fact"])
        self.assertEqual(item["details"]["rows"], [])

    def test_request_speed_uses_first_arrival_and_last_completion_with_24_hour_norm(self):
        engine = self.engine_with_draft()
        history = [["Отработана", "2026-08-05T07:58:32"],
                   ["Поступила", "2026-08-04T19:18:33"],
                   ["Отработана", "2026-08-04T20:00:00"],
                   ["Поступила", "2026-08-04T19:30:00"]]
        self.con.execute("INSERT INTO transport_requests(id,emp,dt,doc,status,deleted,history) VALUES(?,?,?,?,?,?,?)",
                         ("request", "logist", "2026-08-04", "000001973", "Отработана", 0, json.dumps(history)))
        self.con.execute("INSERT INTO dataset_state VALUES(?,?,?,?)",
                         ("transport_requests", "2026-08-01", "2026-11-30", "2026-11-12"))
        for ref, emp, date, deleted in (("other", "pm", "2026-08-04", 0),
                                         ("outside", "logist", "2026-07-31", 0),
                                         ("deleted", "logist", "2026-08-04", 1)):
            self.con.execute("INSERT INTO transport_requests(id,emp,dt,deleted,history) VALUES(?,?,?,?,?)",
                             (ref, emp, date, deleted, json.dumps(history)))
        item = next(i for i in engine.compute("logist", "2026-08")["items"]
                    if i["code"] == "logistics_request_speed")
        self.assertIn("details", item)
        self.assertEqual(item["details"]["columns"],
                         ["Заявка", "Дата заявки", "Ответственный логист", "Статус", "Поступила", "Отработана", "Время обработки", "Результат"])
        self.assertEqual(item["details"]["rows"], [["000001973", "2026-08-04", "Логист", "Отработана",
                         "2026-08-04T19:18:33", "2026-08-05T07:58:32", "12 ч 39 мин 59 с", "В норматив"]])
        self.assertEqual(item["status"], "met")
        self.assertEqual(item["pct"], 1)
        self.assertEqual(item["payout"], 1500)

    def test_request_speed_missing_or_invalid_dates_stay_unknown(self):
        engine = self.engine_with_draft()
        cases = [([], "Нет дат статусов"),
                 ([["Поступила", "2026-11-01T08:00:00"]], "Нет даты «Отработана»"),
                 ([["Отработана", "2026-11-01T08:00:00"]], "Нет даты «Поступила»"),
                 ([["Поступила", "0001-01-01T00:00:00"], ["Отработана", "bad"]], "Нет дат статусов"),
                 ([["Поступила", "2026-11-01T09:00:00"], ["Отработана", "2026-11-01T08:00:00"]],
                  "«Отработана» раньше «Поступила»"),
                 ([["Поступила", "2026-11-01T08:00:00"], ["Отработана", "2026-11-01T08:00:00"]],
                  "0 ч 00 мин 00 с")]
        for index, (history, _) in enumerate(cases):
            self.con.execute("INSERT INTO transport_requests(id,emp,dt,doc,history) VALUES(?,?,?,?,?)",
                             (str(index), "logist", "2026-11-01", str(index), json.dumps(history)))
        self.con.execute("INSERT INTO transport_requests(id,emp,dt,doc,history) VALUES(?,?,?,?,?)",
                         ("broken", "logist", "2026-11-01", "broken", "invalid json"))
        item = next(i for i in engine.compute("logist", "2026-11")["items"]
                    if i["code"] == "logistics_request_speed")
        self.assertIn("details", item)
        self.assertEqual([r[-2] for r in item["details"]["rows"]],
                         [expected for _, expected in cases] + ["Нет дат статусов"])
        self.assertEqual(item["extra"][-1]["value"], "Период загружен не полностью")
        self.assertEqual(item["payout"], 0)

    def test_sync_records_coverage_for_empty_logistics_datasets(self):
        from app import sync
        config = {**self.config, "mode": "real", "onec": {"base_url": "https://example.test", "datasets": {}}}
        with patch("app.sync.OneC") as source, patch("app.sync.onec_sales_register.fetch", return_value=[]), \
                patch("app.sync.onec_money.fetch", return_value={"cash": [], "plan": []}):
            source.return_value.fetch.return_value = {"forwarder_orders": [], "transport_requests": []}
            result = sync.run(self.con, config)
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(self.con.execute("SELECT * FROM dataset_state WHERE dataset='forwarder_orders'").fetchone())
        self.assertIsNotNone(self.con.execute("SELECT * FROM dataset_state WHERE dataset='transport_requests'").fetchone())

    def test_bootstrap_exposes_draft_rules_and_all_setup_issues(self):
        profile = self.profile()
        app = App.__new__(App)
        app.config, app.rules, app.con = self.config, self.rules, self.con
        app.engine = Engine(self.con, self.rules, self.config)
        boot = app.bootstrap(self.config["employees"][0])
        p = boot["positions"]["logistics"]
        self.assertFalse(p["draft"])
        self.assertEqual(p["setup_issues"], profile["setup_issues"])
        self.assertEqual(len(p["items"]), 6)
        self.assertEqual(p["salary"], 70000)
        self.assertEqual(p["kpi_bonus"], 10000)
        self.assertEqual([i["weight"] for i in p["items"]], [.20, .20, .15, .15, .15, .15])


if __name__ == "__main__":
    unittest.main()
