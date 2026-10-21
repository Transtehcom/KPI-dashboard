"""Тесты обезличивания и разбора звонков. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import call_analysis  # noqa: E402
from app.anonymize import Pseudonymizer  # noqa: E402


class AnonymizeTest(unittest.TestCase):
    def test_people_orgs_contacts_masked_numbers_kept(self):
        p = Pseudonymizer(["Нытвинский маслозавод, ООО"])
        text = p.mask("Здравствуйте, Транстехком, меня зовут Мария Мельникова. Звоню в Нытвинский маслозавод. "
                      "Почта snab@nytva-maslo.ru, телефон 8 912 345-67-89, ИНН 5906012345. "
                      "Цена 280 тысяч за 12 штук, до 27-го числа.")
        for secret in ("Мельникова", "Нытвинский", "snab@", "345-67-89", "5906012345"):
            self.assertNotIn(secret, text)
        for kept in ("Транстехком", "280 тысяч", "12 штук", "27-го"):
            self.assertIn(kept, text)

    def test_same_person_same_label_in_any_case_and_order(self):
        p = Pseudonymizer()
        a = p.mask("Письмо отправила Мария Мельникова.")
        b = p.mask("Передайте Марии Мельниковой.")
        c = p.mask("Мельникова Мария на связи.")
        lab = a.split("отправила ")[1].rstrip(".")
        self.assertIn(lab, b)
        self.assertIn(lab, c)
        self.assertEqual(p.restore(f"{lab} предложила КП"), "Мария Мельникова предложила КП")  # именительный, род сохранён

    def test_names_and_orgs_missed_by_natasha(self):
        p = Pseudonymizer()
        text = p.mask("Союзгидротрас, Мария. Юрий, здравствуйте. Это Кантех? Пришлите в Битрикс, КП в Max.")
        for secret in ("Союзгидротрас", "Мария", "Юрий", "Кантех"):
            self.assertNotIn(secret, text)
        for kept in ("Битрикс", "КП", "Max", "здравствуйте"):
            self.assertIn(kept, text)

    def test_restore_nested(self):
        p = Pseudonymizer()
        p.mask("Звонил Андрей Петров.")
        self.assertEqual(p.restore({"a": ["[ИМЯ_1] перезвонит"], "b": 1, "c": "[ИМЯ_9]"}),
                         {"a": ["Андрей Петров перезвонит"], "b": 1, "c": "[ИМЯ_9]"})


ANSWER = {"turns": [{"from": 1, "to": 1, "who": "М"}, {"from": 2, "to": 2, "who": "К"}, {"from": 3, "to": 3, "who": "М"},
                    {"from": 4, "to": 4, "who": "К"}], "call_type": "продажа", "summary": "[ИМЯ_1] договорилась выслать КП",
          "offer": {"made": True, "what": "защитные сооружения", "kp_promised": True, "price_named": False, "sentences": [3]},
          "agreement": {"reached": True, "what": "КП до пятницы", "when": "пятница", "sentences": [3]},
          "prices": [], "deadlines": [], "objections": [], "tone": {"client": "нейтральный", "manager": "позитивный"},
          "next_step": "выслать КП"}


class AnalyzeTest(unittest.TestCase):
    def run_with(self, answer):
        sent = {}

        def fake_run(cmd, input, **kw):
            sent.update(cmd=cmd, input=input)
            env = {"type": "result", "is_error": False, "result": "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```",
                   "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "cache_creation_input_tokens": 900,
                                                     "output_tokens": 120}, "modelUsage": {"claude-sonnet-5-5": {}}}
            return SimpleNamespace(stdout=json.dumps(env, ensure_ascii=False), stderr="", returncode=0)
        segs = [[0.5, 3.0, "Добрый день, Транстехком, Мария Мельникова."], [3.5, 6.0, "Слушаю вас. Вышлите КП до пятницы."]]
        with mock.patch.object(call_analysis.subprocess, "run", fake_run):
            res = call_analysis.analyze(segs, {"direction": "out", "duration": 65}, {})
        return res, sent

    def test_prompt_is_anonymized_and_result_restored(self):
        (res, pii, call, prompt), sent = self.run_with(ANSWER)
        self.assertNotIn("Мельникова", sent["input"])
        self.assertIn("1 [0:00] Добрый день, Транстехком, [ИМЯ_1].", sent["input"])
        self.assertIn("3 [0:03] Вышлите КП до пятницы.", sent["input"])
        self.assertEqual(res["summary"], "Мария Мельникова договорилась выслать КП")
        self.assertEqual([l[1] for l in res["lines"]], ["М", "К", "М"])
        self.assertEqual((call["tokens_in"], call["tokens_out"]), (905, 120))
        for flag in ("--tools", "--strict-mcp-config", "--no-session-persistence", "--system-prompt"):
            self.assertIn(flag, sent["cmd"])
        self.assertEqual(sent["cmd"][sent["cmd"].index("--tools") + 1], "")  # без инструментов

    def test_broken_json_is_retried(self):
        answers = iter(['{"turns": [] "call_type": }', json.dumps(ANSWER, ensure_ascii=False)])

        def fake_run(cmd, input, **kw):
            env = {"is_error": False, "result": next(answers), "total_cost_usd": 0.01, "usage": {"output_tokens": 10}}
            return SimpleNamespace(stdout=json.dumps(env, ensure_ascii=False), stderr="", returncode=0)
        with mock.patch.object(call_analysis.subprocess, "run", fake_run):
            call = call_analysis.run_claude("текст", {})
        self.assertEqual((call["attempts"], call["result"]["call_type"], call["tokens_out"]), (2, "продажа", 20))

    def test_partial_turns_leave_gaps(self):
        (res, *_), _ = self.run_with({**ANSWER, "turns": [{"from": 1, "to": 2, "who": "K"}]})  # латиница, без 3-го
        self.assertFalse(res["speakers_ok"])
        self.assertEqual([l[1] for l in res["lines"]], ["К", "К", ""])


class QueueTest(unittest.TestCase):
    """Очередь разбора в дашборде: что отправляется, как хранится, что остаётся после удаления записи."""

    def setUp(self):
        from app import call_records, transcripts
        from app.common import connect
        self.cr, self.tr = call_records, transcripts
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        rows = [{"id": f"call{i}", "emp": "pm", "dt": f"2026-10-{10 + i}T10:00:00", "direction": "out", "duration": 60,
                 "client": "Нытвинский маслозавод, ООО"} for i in range(1, 4)]
        call_records.upsert(self.con, rows)
        self.con.execute("UPDATE call_records SET file='x.mp3'")
        long_segs = [[0.5, 3.0, "Добрый день, Транстехком, Мария Мельникова."],
                     [3.5, 9.0, "Слушаю вас. Нытвинский маслозавод. Вышлите КП до пятницы, рассмотрим."]]
        for rid, segs, engine in (("call1", long_segs, transcripts.ENGINE), ("call2", [[0, 1, "Алло?"]], transcripts.ENGINE),
                                  ("call3", long_segs, "gigaam-v2")):
            self.con.execute("INSERT INTO call_transcripts (id, segments, text, engine) VALUES (?,?,?,?)",
                             (rid, json.dumps(segs, ensure_ascii=False), " ".join(s[2] for s in segs), engine))
        self.con.commit()
        self.sent = []

    def fake_run(self, answer=None, error=None):
        def run(cmd, input, **kw):
            self.sent.append(input)
            env = {"is_error": bool(error), "result": error or json.dumps(answer or ANSWER, ensure_ascii=False),
                   "usage": {"output_tokens": 50}}
            return SimpleNamespace(stdout=json.dumps(env, ensure_ascii=False), stderr="", returncode=0)
        return mock.patch.object(call_analysis.subprocess, "run", run)

    def test_queue_sends_only_long_current_transcripts_and_stores_masked(self):
        with self.fake_run():
            self.assertEqual(call_analysis.run(self.con, {}, log=lambda *a: None), (2, 0, False))
        self.assertEqual(len(self.sent), 1)  # call2 — слишком короткий, call3 — старая модель
        self.assertNotIn("Мельникова", self.sent[0])
        self.assertNotIn("Нытвинский", self.sent[0])
        stored = self.con.execute("SELECT result, roles, offer, agreement FROM call_analysis WHERE id='call1'").fetchone()
        self.assertNotIn("Мельникова", stored["result"])  # в базе — с метками
        self.assertEqual((stored["roles"], stored["offer"], stored["agreement"]), ("МКМК", 1, 1))
        self.assertEqual(call_analysis.get(self.con, "call1")["summary"], "Мария Мельникова договорилась выслать КП")
        self.assertEqual(call_analysis.get(self.con, "call2")["call_type"], "недозвон")
        with self.fake_run():
            self.assertEqual(call_analysis.run(self.con, {}, log=lambda *a: None), (0, 0, False))  # всё уже разобрано
        a = {c["id"]: c["analysis"] for c in self.cr.listing(self.con, {})}
        self.assertEqual((a["call1"]["offer"], a["call1"]["summary"][:16]), (True, "Мария Мельникова"))
        self.assertIsNone(a["call3"])

    def test_usage_limit_stops_without_counting_attempt(self):
        with self.fake_run(error="Claude AI usage limit reached|1761058800"):
            self.assertEqual(call_analysis.run(self.con, {}, log=lambda *a: None)[2], True)
        self.assertIsNone(self.con.execute("SELECT 1 FROM call_analysis WHERE id='call1'").fetchone())

    def test_new_prompt_version_redoes_analysis(self):
        with self.fake_run():
            call_analysis.run(self.con, {}, log=lambda *a: None)
        self.con.execute("UPDATE call_analysis SET version = version - 1 WHERE id='call1'")
        self.assertEqual([r["id"] for r in call_analysis.pending(self.con, 10)], ["call1"])

    def test_purge_keeps_flags_but_drops_names(self):
        with self.fake_run():
            call_analysis.run(self.con, {}, log=lambda *a: None)
        self.con.execute("UPDATE call_records SET dt='2020-01-01T10:00:00' WHERE id='call1'")
        self.cr.purge_old(self.con, {"call_records": {"keep_days": 30}})
        a = call_analysis.get(self.con, "call1")
        self.assertEqual((a["offer"]["made"], a["summary"]), (True, "[ИМЯ_1] договорилась выслать КП"))


if __name__ == "__main__":
    unittest.main()
