import datetime as dt
import json
import unittest
from unittest.mock import patch

from app.common import connect, load_rules
from app.gpt_privacy import PrivacyContext
from app.gpt_tools import DataTools


class WeatherTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.tools = DataTools(self.con, {"employees": []}, load_rules())
        self.privacy = PrivacyContext(documents_only=True)

    def test_weather_uses_separate_public_context_and_cites_sources(self):
        today = dt.date.today().isoformat()
        result = {"status": "ok", "date": today, "summary": "Облачно, +2 °C", "sources": [{"title": "Прогноз", "url": "https://example.com/weather"}]}
        def model(prompt, config, **kwargs):
            self.assertTrue(config["codex_web_search"])
            self.assertIn("Perm, Russia", prompt)
            self.assertNotIn("history", prompt)
            config["codex_web_used"] = True
            return {"result": result}
        with patch("app.gpt_weather.call_analysis.run_codex", side_effect=model):
            out = self.tools.execute("weather", json.dumps({"city": "Пермь", "date": today}), "", "", self.privacy)
        self.assertEqual(out["summary"], "Облачно, +2 °C")
        self.assertTrue(out["fetched_at"])
        self.assertTrue(out["sources"][0]["source"])

    def test_weather_rejects_missing_live_search_and_bad_city(self):
        with patch("app.gpt_weather.call_analysis.run_codex", return_value={"result": {"status": "ok"}}):
            with self.assertRaises(ValueError):
                self.tools.execute("weather", '{"city":"Москва"}', "", "", self.privacy)
        with self.assertRaises(ValueError):
            self.tools.execute("weather", '{"city":"https://internal.example/secret"}', "", "", self.privacy)

    def test_non_location_strings_never_reach_public_model(self):
        with patch("app.gpt_weather.call_analysis.run_codex") as model:
            with self.assertRaises(ValueError):
                self.tools.execute("weather", '{"city":"Acme Confidential Client John Smith"}', "", "", self.privacy)
            with self.assertRaises(ValueError):
                self.tools.execute("weather", '{"city":"Пермь, Acme secret"}', "", "", self.privacy)
        model.assert_not_called()

    def test_web_evidence_requires_completed_success(self):
        from types import SimpleNamespace
        from pathlib import Path
        from app import call_analysis
        for status, expected in (("failed", False), ("completed", True)):
            config = {"codex_web_search": True}
            def execute(cmd, **kwargs):
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{"ok":true}')
                events = [
                    {"type": "item.started", "item": {"type": "web_search"}},
                    {"type": "item.completed", "item": {"type": "web_search", "status": status}},
                ]
                return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(json.dumps(e) for e in events))
            with patch.object(call_analysis, "_execute_cli", side_effect=execute):
                call_analysis.run_codex("Public weather only", config, attempts=1)
            self.assertEqual(config["codex_web_used"], expected)
