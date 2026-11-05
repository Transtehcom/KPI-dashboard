"""Тесты обезличивания и разбора звонков. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import json
import os
import pathlib
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
          "secretary": False, "next_step": "выслать КП"}


class AnalyzeTest(unittest.TestCase):
    def run_with(self, answer):
        sent = {}

        def fake_run(cmd, input, **kw):
            schema_path = pathlib.Path(cmd[cmd.index("--output-schema") + 1])
            answer_path = pathlib.Path(cmd[cmd.index("--output-last-message") + 1])
            sent.update(cmd=cmd, input=input, kwargs=kw, schema=json.loads(schema_path.read_text(encoding="utf-8")))
            answer_path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        segs = [[0.5, 3.0, "Добрый день, Транстехком, Мария Мельникова."], [3.5, 6.0, "Слушаю вас. Вышлите КП до пятницы."]]
        with mock.patch.object(call_analysis, "_execute_cli", fake_run):
            res = call_analysis.analyze(segs, {"direction": "out", "duration": 65}, {})
        return res, sent

    def test_prompt_is_anonymized_and_result_restored(self):
        (res, pii, call, prompt), sent = self.run_with(ANSWER)
        self.assertNotIn("Мельникова", sent["input"])
        self.assertIn("1 [0:00] Добрый день, Транстехком, [ИМЯ_1].", sent["input"])
        self.assertIn("3 [0:03] Вышлите КП до пятницы.", sent["input"])
        self.assertIn("недоверенные данные", sent["input"])
        self.assertIn("подождите 20 секунд", sent["input"])
        self.assertIn("Не додумывай", sent["input"])
        self.assertEqual(res["summary"], "Мария Мельникова договорилась выслать КП")
        self.assertEqual([l[1] for l in res["lines"]], ["М", "К", "М"])
        self.assertEqual((call["model"], call["attempts"], call["tokens_in"], call["tokens_out"], call["cost_usd"]),
                         (call_analysis.MODEL, 1, None, None, None))
        self.assertEqual((call_analysis.MODEL, call_analysis.EFFORT), ("gpt-5.6-terra", "high"))
        for flag in ("--output-schema", "--output-last-message", "--ephemeral", "--ignore-rules",
                     "--skip-git-repo-check"):
            self.assertIn(flag, sent["cmd"])
        self.assertIn("--ignore-user-config", sent["cmd"])
        self.assertEqual(sent["cmd"][1], "exec")
        self.assertEqual(sent["cmd"][sent["cmd"].index("--sandbox") + 1], "read-only")
        self.assertEqual(sent["cmd"][sent["cmd"].index("--disable") + 1], "shell_tool")
        self.assertEqual(sent["cmd"][-1], "-")
        self.assertTrue(sent["kwargs"]["env"]["CODEX_HOME"].endswith(".codex"))
        self.assertFalse(sent["schema"]["additionalProperties"])
        self.assertIn("secretary", sent["schema"]["required"])

    def test_broken_json_is_retried(self):
        answers = iter(['{"turns": [] "call_type": }', json.dumps(ANSWER, ensure_ascii=False)])

        def fake_run(cmd, input, **kw):
            pathlib.Path(cmd[cmd.index("--output-last-message") + 1]).write_text(next(answers), encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        with mock.patch.object(call_analysis, "_execute_cli", fake_run):
            call = call_analysis.run_codex("текст", {})
        self.assertEqual((call["attempts"], call["result"]["call_type"], call["tokens_out"]), (2, "продажа", None))

    def test_partial_turns_leave_gaps(self):
        (res, *_), _ = self.run_with({**ANSWER, "turns": [{"from": 1, "to": 2, "who": "K"}]})  # латиница, без 3-го
        self.assertFalse(res["speakers_ok"])
        self.assertEqual([l[1] for l in res["lines"]], ["К", "К", ""])


ANYTIME = {}


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
            if error:
                return SimpleNamespace(stdout="", stderr=error, returncode=1)
            pathlib.Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                json.dumps(answer or ANSWER, ensure_ascii=False), encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return mock.patch.object(call_analysis, "_execute_cli", run)

    def analyze_current(self):
        """Разобрать все звонки с текущей расшифровкой без разбора (как ручная обработка): (разобрано, ошибок, лимит)."""
        ok = failed = 0
        for r in self.con.execute("SELECT c.*, t.segments, t.text FROM call_records c JOIN call_transcripts t ON t.id = c.id "
                                  "LEFT JOIN call_analysis a ON a.id = c.id WHERE t.engine=? AND a.result IS NULL "
                                  "ORDER BY c.dt DESC", (self.tr.ENGINE,)).fetchall():
            res = call_analysis.analyze_row(self.con, ANYTIME, dict(r), {}, log=lambda *a: None)
            if res == "limit":
                return ok, failed, True
            ok += res == "ok"
            failed += res == "failed"
        return ok, failed, False

    def test_analysis_sends_only_long_current_transcripts_and_stores_masked(self):
        with self.fake_run():
            self.assertEqual(self.analyze_current(), (2, 0, False))
        self.assertEqual(len(self.sent), 1)  # call2 — слишком короткий, call3 — старая модель
        self.assertNotIn("Мельникова", self.sent[0])
        self.assertNotIn("Нытвинский", self.sent[0])
        stored = self.con.execute("SELECT result, roles, offer, agreement, model, tokens_in, tokens_out, cost_usd, attempts "
                                  "FROM call_analysis WHERE id='call1'").fetchone()
        self.assertNotIn("Мельникова", stored["result"])  # в базе — с метками
        self.assertEqual((stored["roles"], stored["offer"], stored["agreement"]), ("МКМК", 1, 1))
        self.assertEqual((stored["model"], stored["tokens_in"], stored["tokens_out"], stored["cost_usd"], stored["attempts"]),
                         (call_analysis.MODEL, None, None, None, 1))
        self.assertEqual(call_analysis.get(self.con, "call1")["summary"], "Мария Мельникова договорилась выслать КП")
        self.assertEqual(call_analysis.get(self.con, "call2")["call_type"], "недозвон")
        with self.fake_run():
            self.assertEqual(self.analyze_current(), (0, 0, False))  # всё уже разобрано
        a = {c["id"]: c["analysis"] for c in self.cr.listing(self.con, {})}
        self.assertEqual((a["call1"]["offer"], a["call1"]["summary"][:16]), (True, "Мария Мельникова"))
        self.assertIsNone(a["call3"])

    def test_manual_job_transcribes_missing_and_analyzes_selected(self):
        import time
        from app.call_jobs import JobQueue
        queue = JobQueue(self.con, ANYTIME)
        self.addCleanup(queue.stop)
        queue.start()
        self.assertEqual(call_analysis.todo(self.con, ["call1", "call2", "call3"]), {"call1": False, "call2": False, "call3": True})

        def fake_transcribe(con, config, rdir, log, ids=None, progress=None):  # «расшифровывает» call3 текущей моделью
            self.assertEqual(ids, ["call3"])
            con.execute("UPDATE call_transcripts SET engine=? WHERE id='call3'", (self.tr.ENGINE,))
            con.commit()
            return 1, 0
        with self.fake_run(), mock.patch.object(self.tr, "run", fake_transcribe):
            job = queue.create(["call1", "call2", "call3"])
            for _ in range(100):
                if queue.get(job["id"])["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(tuple(queue.get(job["id"])[k] for k in ("status", "done", "failed")), ("completed", 3, 0))
        self.assertEqual(len(self.sent), 2)  # call2 — короткий, без Codex
        self.assertEqual(call_analysis.todo(self.con, ["call1", "call2", "call3"]), {})
        with self.assertRaises(ValueError):
            queue.create(["call1", "call2", "call3"])  # всё уже обработано
        with self.fake_run():  # один звонок — заново
            job = queue.create(["call1"], force=True)
            for _ in range(100):
                if queue.get(job["id"])["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(len(self.sent), 3)

    def test_usage_limit_stops_without_counting_attempt(self):
        with self.fake_run(error="Codex usage limit reached|1761058800"):
            self.assertEqual(self.analyze_current()[2], True)
        self.assertIsNone(self.con.execute("SELECT 1 FROM call_analysis WHERE id='call1'").fetchone())

    def test_secretary_from_codex_and_manager_mark(self):
        with self.fake_run({**ANSWER, "secretary": True}):
            self.analyze_current()
        self.assertEqual(call_analysis.secretary_marks(self.con, ["call1"]), {"call1": {"ai": 1, "manual": None}})
        call_analysis.set_secretary(self.con, "call1", 0, "boss")
        a = {c["id"]: c["analysis"] for c in self.cr.listing(self.con, {})}
        self.assertEqual((a["call1"]["secretary"], a["call1"]["secretary_manual"]), (0, True))
        g = call_analysis.get(self.con, "call1")
        self.assertEqual((g["secretary_ai"], g["secretary_manual"]), (1, 0))
        call_analysis.save(self.con, "call1", {**ANSWER, "secretary": True}, "", {})  # повторный разбор
        self.assertEqual(call_analysis.secretary_marks(self.con, ["call1"])["call1"]["manual"], 0)  # отметка осталась
        call_analysis.set_secretary(self.con, "call1", None, "boss")
        self.assertIsNone(call_analysis.secretary_marks(self.con, ["call1"])["call1"]["manual"])

    def test_purge_keeps_flags_but_drops_names(self):
        with self.fake_run():
            self.analyze_current()
        self.con.execute("UPDATE call_records SET dt='2020-01-01T10:00:00' WHERE id='call1'")
        self.cr.purge_old(self.con, {"call_records": {"keep_days": 30}})
        a = call_analysis.get(self.con, "call1")
        self.assertEqual((a["offer"]["made"], a["summary"]), (True, "[ИМЯ_1] договорилась выслать КП"))


if __name__ == "__main__":
    unittest.main()


class CLITimeoutTest(unittest.TestCase):
    def test_timeout_depends_on_effort_and_can_be_configured(self):
        self.assertEqual(call_analysis.analysis_timeout({},"max"),900)
        self.assertEqual(call_analysis.analysis_timeout({},"high"),600)
        self.assertEqual(call_analysis.analysis_timeout({},"low"),300)
        self.assertEqual(call_analysis.analysis_timeout({"call_records":{"analysis_timeout_seconds":1200}},"max"),1200)

    def test_cli_timeout_terminates_its_process_tree_and_reports_clean_error(self):
        process=mock.Mock()
        process.communicate.side_effect=call_analysis.subprocess.TimeoutExpired(["codex"],900)
        with mock.patch.object(call_analysis.subprocess,"Popen",return_value=process),mock.patch.object(call_analysis,"_kill_cli_tree") as kill:
            with self.assertRaisesRegex(RuntimeError,"900"):
                call_analysis._execute_cli(["codex"],input="masked",timeout=900,text=True)
        kill.assert_called_once_with(process)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_cli_io_failure_also_terminates_process_tree(self):
        process=mock.Mock()
        process.communicate.side_effect=OSError("broken pipe")
        with mock.patch.object(call_analysis.subprocess,"Popen",return_value=process),mock.patch.object(call_analysis,"_kill_cli_tree") as kill:
            with self.assertRaisesRegex(OSError,"broken pipe"):
                call_analysis._execute_cli(["codex"],input="masked",timeout=900,text=True)
        kill.assert_called_once_with(process)
