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

    def test_restore_nested(self):
        p = Pseudonymizer()
        p.mask("Звонил Андрей Петров.")
        self.assertEqual(p.restore({"a": ["[ИМЯ_1] перезвонит"], "b": 1, "c": "[ИМЯ_9]"}),
                         {"a": ["Андрей Петров перезвонит"], "b": 1, "c": "[ИМЯ_9]"})


ANSWER = {"speakers": "МКМ", "call_type": "продажа", "summary": "[ИМЯ_1] договорилась выслать КП",
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
        answers = iter(['{"speakers": "МКМ" "call_type": }', json.dumps(ANSWER, ensure_ascii=False)])

        def fake_run(cmd, input, **kw):
            env = {"is_error": False, "result": next(answers), "total_cost_usd": 0.01, "usage": {"output_tokens": 10}}
            return SimpleNamespace(stdout=json.dumps(env, ensure_ascii=False), stderr="", returncode=0)
        with mock.patch.object(call_analysis.subprocess, "run", fake_run):
            call = call_analysis.run_claude("текст", {})
        self.assertEqual((call["attempts"], call["result"]["call_type"], call["tokens_out"]), (2, "продажа", 20))

    def test_wrong_speaker_count_is_ignored(self):
        (res, *_), _ = self.run_with({**ANSWER, "speakers": "МК"})
        self.assertFalse(res["speakers_ok"])
        self.assertEqual([l[1] for l in res["lines"]], ["", "", ""])


if __name__ == "__main__":
    unittest.main()
