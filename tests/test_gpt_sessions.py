import unittest
from unittest.mock import patch
import json
import os
import tempfile
from types import SimpleNamespace

from app.common import connect
from app.gpt_sessions import Sessions


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.sessions = Sessions(self.con)

    def test_resume_uses_exact_thread_and_delta_without_master(self):
        first = "11111111-1111-4111-8111-111111111111"
        calls = []
        def model(prompt, config, **kwargs):
            calls.append((prompt, kwargs["session"].copy()))
            return {"result": {"answer": "ok"}, "thread_id": first}
        self.sessions.begin("chat", "rights", "MASTER", 2)
        with patch("app.gpt_sessions.call_analysis.run_codex", side_effect=model):
            self.sessions.call("chat", "MASTER", {"delta": 1}, {"bootstrap": 1}, {}, "model", "low", {}, 1)
            self.sessions.call("chat", "MASTER", {"delta": 2}, {"bootstrap": 2}, {}, "model", "low", {}, 1)
        self.assertIn('"bootstrap": 1', calls[0][0])
        self.assertIn('"delta": 2', calls[1][0])
        self.assertNotIn("bootstrap", calls[1][0])
        self.assertIsNone(calls[0][1]["id"])
        self.assertEqual(calls[1][1]["id"], first)
        self.assertEqual(calls[0][1]["master"], "MASTER")
        self.assertIsNone(calls[1][1]["master"])

    def test_completed_session_survives_restart_but_interrupted_session_resets(self):
        thread = "11111111-1111-4111-8111-111111111111"
        self.sessions.begin("chat", "rights", "MASTER", 2)
        with patch("app.gpt_sessions.call_analysis.run_codex", return_value={"result": {}, "thread_id": thread}):
            self.sessions.call("chat", "MASTER", {}, {}, {}, "model", "low", {}, 1)
        self.sessions.finish("chat", 2)
        restarted = Sessions(self.con)
        restarted.begin("chat", "rights", "MASTER", 4)
        self.assertEqual(restarted.state("chat")["thread_id"], thread)
        interrupted = Sessions(self.con)
        self.assertIsNone(interrupted.state("chat")["thread_id"])

    def test_changed_rights_or_master_do_not_resume_old_thread(self):
        self.sessions.begin("chat", "rights", "MASTER", 2)
        with patch("app.gpt_sessions.call_analysis.run_codex", return_value={"result": {}, "thread_id": "11111111-1111-4111-8111-111111111111"}):
            self.sessions.call("chat", "MASTER", {}, {}, {}, "model", "low", {}, 1)
        self.sessions.finish("chat", 2)
        self.sessions.begin("chat", "new-rights", "MASTER", 4)
        self.assertIsNone(self.sessions.state("chat")["thread_id"])

    def test_failed_call_invalidates_thread_without_automatic_retry(self):
        self.sessions.begin("chat", "rights", "MASTER", 2)
        with patch("app.gpt_sessions.call_analysis.run_codex", side_effect=RuntimeError("timeout")) as model:
            with self.assertRaises(RuntimeError):
                self.sessions.call("chat", "MASTER", {}, {}, {}, "model", "low", {}, 1)
        self.assertEqual(model.call_count, 1)
        self.assertIsNone(self.sessions.state("chat")["thread_id"])

    def test_same_thread_cannot_be_bound_to_two_chats(self):
        thread = "11111111-1111-4111-8111-111111111111"
        with patch("app.gpt_sessions.call_analysis.run_codex", return_value={"result": {}, "thread_id": thread}):
            self.sessions.begin("first", "rights", "MASTER", 2)
            self.sessions.call("first", "MASTER", {}, {}, {}, "model", "low", {}, 1)
            self.sessions.finish("first", 2)
            self.sessions.begin("second", "rights", "MASTER", 4)
            with self.assertRaises(Exception):
                self.sessions.call("second", "MASTER", {}, {}, {}, "model", "low", {}, 3)
        self.assertIsNone(self.sessions.state("second")["thread_id"])
        self.assertEqual(self.sessions.state("first")["thread_id"], thread)

    def test_cli_persists_master_in_file_and_resumes_only_exact_uuid(self):
        from app import call_analysis
        thread = "11111111-1111-4111-8111-111111111111"
        commands = []
        def execute(cmd, **kwargs):
            commands.append(cmd)
            with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as out:
                json.dump({"answer": "ok"}, out)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "thread.started", "thread_id": thread}), stderr="")
        with tempfile.TemporaryDirectory() as folder:
            session = {"id": None, "cwd": folder, "master": "MASTER" * 4000}
            with patch.object(call_analysis, "_execute_cli", side_effect=execute):
                call_analysis.run_codex("first", {}, session=session)
                self.assertEqual(session["id"], thread)
                self.assertTrue(os.path.isfile(os.path.join(folder, "master.txt")))
                session["master"] = None
                call_analysis.run_codex("second", {}, session=session)
        self.assertNotIn("--ephemeral", commands[0])
        self.assertFalse(any("MASTER" in arg for arg in commands[0]))
        self.assertIn("model_instructions_file=", " ".join(commands[0]))
        self.assertNotIn("model_instructions_file=", " ".join(commands[1]))
        self.assertEqual(commands[1][commands[1].index("resume") + 1], thread)
        self.assertNotIn("--last", commands[1])
        self.assertNotEqual(commands[0][commands[0].index("--output-last-message") + 1], commands[1][commands[1].index("--output-last-message") + 1])

    def test_chat_second_message_sends_only_new_user_message(self):
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"ttkgpt_runtime":"legacy", "employees": [{"login": "boss", "name": "Администратор", "role": "manager"}]}, load_rules())
        chat = service.create_chat("boss")
        calls = []
        def model(prompt, config, **kwargs):
            calls.append((prompt, kwargs["session"].copy()))
            return {"thread_id": "11111111-1111-4111-8111-111111111111", "result": {"kind": "answer", "tool": "", "query": "", "employee": "", "month": "", "answer": "Готово", "sources": []}}
        with patch("app.gpt_sessions.call_analysis.run_codex", side_effect=model):
            for text in ("initial-marker", "new-marker"):
                job = service.submit("boss", chat["id"], text, launch=False)
                service.work(chat["id"], job["id"])
                self.assertEqual(service.chat("boss", chat["id"])["messages"][-1]["status"], "completed")
        self.assertIsNotNone(calls[0][1]["master"])
        self.assertIsNone(calls[1][1]["master"])
        self.assertIn("new-marker", calls[1][0])
        self.assertNotIn("initial-marker", calls[1][0])

    def test_tool_continuation_sends_only_latest_result(self):
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"ttkgpt_runtime":"legacy", "employees": [{"login": "boss", "name": "Администратор", "role": "manager"}]}, load_rules())
        chat = service.create_chat("boss")
        packets = []
        def model(prompt, config, **kwargs):
            data = json.loads(prompt.split("ДАННЫЕ:\n", 1)[1])
            packets.append(data)
            result = {"kind": "tool", "tool": "employees", "query": "", "employee": "", "month": "", "answer": "", "sources": []}
            if len(packets) == 2:
                result.update(tool="search", query="nothing")
            elif len(packets) == 3:
                result.update(kind="answer", tool="", answer="Готово")
            return {"thread_id": "11111111-1111-4111-8111-111111111111", "result": result}
        with patch("app.gpt_sessions.call_analysis.run_codex", side_effect=model):
            job = service.submit("boss", chat["id"], "Проверь данные", launch=False)
            service.work(chat["id"], job["id"])
        self.assertEqual([r["tool"] for r in packets[1]["tool_results"]], ["employees"])
        self.assertEqual([r["tool"] for r in packets[2]["tool_results"]], ["search"])
        self.assertEqual(packets[1]["history"], [])

    def test_denied_access_turn_persists_alias_map_before_codex_call(self):
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"ttkgpt_runtime":"legacy", "employees": [{"login": "pm", "name": "Менеджер", "role": "employee", "position": "pm"}]}, load_rules())
        chat = service.create_chat("pm")
        seen = []
        def model(prompt, config, **kwargs):
            state = json.loads(self.con.execute("SELECT privacy FROM gpt_chats WHERE id=?", (chat["id"],)).fetchone()[0])
            self.assertIn("7701234567", state["values"].values())
            seen.append(state)
            return {"thread_id": "11111111-1111-4111-8111-111111111111", "result": {"domains": ["sales"], "other_people": True}}
        with patch("app.gpt_sessions.call_analysis.run_codex", side_effect=model):
            for number in ("7701234567", "7707654321"):
                job = service.submit("pm", chat["id"], "Покажи чужой договор\nДоговор\nИНН " + number, launch=False)
                service.work(chat["id"], job["id"])
                self.assertEqual(service.chat("pm", chat["id"])["messages"][-1]["status"], "completed")
        first_label = next(key for key, value in seen[0]["values"].items() if value == "7701234567")
        self.assertEqual(seen[1]["values"][first_label], "7701234567")
        self.assertIn("7707654321", seen[1]["values"].values())
