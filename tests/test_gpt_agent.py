import copy
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

from app.common import connect, load_rules
from app.gpt_assistant import GPTAssistant
from tests.test_engine import CONFIG


THREAD = "11111111-1111-4111-8111-111111111111"


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.config = copy.deepcopy(CONFIG)
        self.service = GPTAssistant(self.con, self.config, load_rules())
        self.calls = []

    def request(self, owner, question, behavior, chat=None):
        chat = chat or self.service.create_chat(owner)["id"]
        message = self.service.submit(owner, chat, question, launch=False)
        server = Mock()
        def run(**kwargs):
            self.calls.append(kwargs)
            kwargs["on_thread"](kwargs["thread_id"] or THREAD)
            return behavior(kwargs, json.loads(kwargs["input"][0]["text"]))
        server.run.side_effect = run
        with patch("app.gpt_agent.AppServer") as factory:
            factory.return_value.__enter__.return_value = server
            self.service.work(chat, message["id"])
        return chat, self.service.chat(owner, chat)["messages"][-1]

    def test_native_agent_calls_tools_and_saves_sources(self):
        def behavior(args, payload):
            result = args["tool_handler"]("ttk_employees", {"query":""})
            text = json.loads(result[0]["text"])
            self.assertIn("employees", text)
            return {"kind":"answer","answer":"Данные проверены","sources":[]}
        chat, reply = self.request("boss", "Покажи сотрудников", behavior)
        self.assertEqual(reply["status"], "completed")
        self.assertEqual(reply["content"], "Данные проверены")
        self.assertEqual(self.service.sessions.state(chat)["thread_id"], THREAD)
        self.assertIsNone(self.service.sessions.state(chat)["active_message"])
        self.assertIn('"tool": "employees"', self.service.preview("boss", chat, reply["id"])["outbound"])

    def test_work_master_excludes_internal_classification_instructions(self):
        def behavior(args, payload):
            self.assertNotIn("Верни только классификацию", args["master"])
            return {"kind":"answer","answer":"Для поиска писем подключите почту","sources":[]}
        _chat, reply = self.request("boss", "Найди письма от GitHub в моей почте", behavior)
        self.assertEqual(reply["status"], "completed")

    def test_nested_classification_is_repaired_before_user_sees_it(self):
        replies = iter([
            {"kind":"answer","answer":'{"domains":["general"],"other_people":false}',"sources":[]},
            {"kind":"answer","answer":"Для поиска писем нужно подключить почту","sources":[]},
        ])
        _chat, reply = self.request("boss", "Найди письма от GitHub в моей почте", lambda args,payload:next(replies))
        self.assertEqual(reply["status"], "completed")
        self.assertIn("подключить почту", reply["content"])
        self.assertEqual(len(self.calls), 2)

    def test_classification_and_work_use_separate_thread_contexts(self):
        def behavior(args, payload):
            if payload["mode"] == "access_check":
                self.assertTrue(args["ephemeral"])
                self.assertEqual(args["tools"], [])
                self.assertIn("Верни только классификацию", args["master"])
                return {"domains":["self"],"other_people":False}
            self.assertIsNone(args["thread_id"])
            self.assertNotIn("Верни только классификацию", args["master"])
            return {"kind":"answer","answer":"Ваши данные","sources":[]}
        _chat, reply = self.request("pm", "Покажи мои показатели", behavior)
        self.assertEqual(reply["status"], "completed")
        self.assertEqual(len(self.calls), 2)

    def test_format_repair_cannot_repeat_tool_actions(self):
        def behavior(args, payload):
            if len(self.calls) == 1:
                return {"kind":"answer","answer":'{"domains":["general"],"other_people":false}',"sources":[]}
            with self.assertRaises(ValueError):
                args["tool_handler"]("ttk_document_edit", {"query":"{}"})
            return {"kind":"answer","answer":"Нужно подключить почту","sources":[]}
        _chat, reply = self.request("boss", "Найди письма", behavior)
        self.assertEqual(reply["status"], "completed")

    def test_repeated_classification_output_never_becomes_completed_answer(self):
        result = {"kind":"answer","answer":'{"domains":["general"],"other_people":false}',"sources":[]}
        _chat, reply = self.request("boss", "Найди письма от GitHub", lambda args,payload:result)
        self.assertEqual(reply["status"], "error")
        self.assertNotIn('"domains"', reply["content"])

    def test_old_classification_artifacts_do_not_prime_new_work_turn(self):
        chat = self.service.create_chat("boss")["id"]
        self.con.execute("INSERT INTO gpt_messages(chat_id,role,status,content) VALUES (?,'user','completed','Проверь мою почту')", (chat,))
        self.con.execute("INSERT INTO gpt_messages(chat_id,role,status,content) VALUES (?,'assistant','completed',?)", (chat,'{"domains":["general"],"other_people":false}'))
        self.con.commit()
        def behavior(args, payload):
            self.assertFalse(any('"domains"' in item["content"] for item in payload["history"]))
            return {"kind":"answer","answer":"Подключите почту для поиска писем","sources":[]}
        _chat, reply = self.request("boss", "Найди письма GitHub", behavior, chat)
        self.assertEqual(reply["status"], "completed")

    def test_staff_preflight_denies_other_direction_before_source_reads(self):
        def behavior(args, payload):
            self.assertEqual(payload["mode"], "access_check")
            with self.assertRaises(ValueError):
                args["tool_handler"]("ttk_employees", {"query":""})
            return {"domains":["logistics"],"other_people":False}
        with patch.object(self.service.data, "execute") as sources:
            _chat, reply = self.request("pm", "Скоро приедет машина?", behavior)
        self.assertEqual(reply["status"], "completed")
        self.assertIn("выходит за пределы", reply["content"])
        self.assertEqual(len(self.calls), 1)
        sources.assert_not_called()

    def test_staff_tools_keep_the_bound_employee(self):
        outputs = []
        def behavior(args, payload):
            if payload["mode"] == "access_check":
                return {"domains":["self"],"other_people":False}
            outputs.extend(args["tool_handler"]("ttk_employees", {"query":""}))
            foreign = args["tool_handler"]("ttk_kpi", {"query":"","employee":"boss"})
            self.assertIn("error", json.loads(foreign[0]["text"]))
            return {"kind":"answer","answer":"Ваши данные","sources":[]}
        _chat, reply = self.request("pm", "Покажи мои KPI", behavior)
        self.assertEqual(reply["status"], "completed")
        employees = json.loads(outputs[0]["text"])["employees"]
        self.assertEqual(len(employees), 1)
        self.assertEqual(employees[0]["employee"], next(e["name"] for e in self.config["employees"] if e["login"] == "pm"))

    def test_permission_change_during_tool_read_blocks_result(self):
        def behavior(args, payload):
            def change(*_args, **_kwargs):
                self.config["employees"][0]["role"] = "employee"
                return {"secret":"must not reach model"}
            with patch.object(self.service.data,"execute",side_effect=change):
                args["tool_handler"]("ttk_employees", {"query":""})
            self.fail("Changed access must interrupt tool return")
        with self.assertRaises(ValueError):
            self.request("boss", "Проверь данные", behavior)
        # A scope change intentionally blocks the normal chat API.
        self.assertEqual(self.con.execute("SELECT status FROM gpt_messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0], "error")

    def test_cancellation_invalidates_thread_and_releases_chat(self):
        def behavior(args, payload):
            args["cancel_event"].set()
            return {"kind":"answer","answer":"Запоздавший ответ","sources":[]}
        chat, reply = self.request("boss", "Проверь данные", behavior)
        self.assertEqual(reply["status"], "cancelled")
        self.assertIsNone(self.service.sessions.state(chat)["thread_id"])
        self.assertNotIn(chat, self.service.active)

    def test_resume_uses_exact_thread_and_only_new_history(self):
        answer = lambda args, payload: {"kind":"answer","answer":"Готово","sources":[]}
        chat, _ = self.request("boss", "Первая реплика", answer)
        self.request("boss", "Вторая реплика", answer, chat)
        self.assertEqual(self.calls[-1]["thread_id"], THREAD)
        payload = json.loads(self.calls[-1]["input"][0]["text"])
        self.assertEqual([m["content"] for m in payload["history"]], ["Вторая реплика"])

    def test_protected_pasted_document_is_masked_before_native_turn(self):
        def behavior(args, payload):
            self.assertNotIn("7701234567", json.dumps(payload))
            return {"kind":"answer","answer":"Документ прочитан","sources":[]}
        _chat, reply = self.request("boss", "Договор поставки\nИНН 7701234567\nСумма 100 рублей", behavior)
        self.assertEqual(reply["status"], "completed")

    def test_native_calculation_uses_separate_bounded_tool(self):
        def behavior(args, payload):
            result = args["tool_handler"]("ttk_python_calculate", {"query":json.dumps({"code":"result = sum(data)","data":[1,2,3]})})
            self.assertEqual(json.loads(result[0]["text"])["result"],6)
            return {"kind":"answer","answer":"6","sources":[]}
        _chat, reply = self.request("boss", "Посчитай рабочие показатели", behavior)
        self.assertEqual(reply["status"], "completed")

    def test_broken_native_response_does_not_keep_unsafe_session(self):
        chat, reply = self.request("boss", "Проверь данные", lambda args, payload:{"answer":"missing fields"})
        self.assertEqual(reply["status"], "error")
        self.assertIsNone(self.service.sessions.state(chat)["thread_id"])

    def test_timed_out_document_edit_cannot_save_after_rollback(self):
        chat = self.service.create_chat("boss")["id"]
        document = self.service.documents.save(chat, "notes.txt", b"old")
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def slow_edit(*_args):
            entered.set()
            release.wait(5)
            return b"new"
        def behavior(args, payload):
            def invoke():
                try:
                    args["tool_handler"]("ttk_document_edit", {"query":json.dumps({"id":document["id"],"changes":[{"unit":"line:1","old":"old","new":"new"}]})})
                except Exception as exc:
                    errors.append(exc)
                finally:
                    done.set()
            threading.Thread(target=invoke, daemon=True).start()
            self.assertTrue(entered.wait(2))
            raise RuntimeError("Transport timed out")
        with patch("app.gpt_documents.edit", side_effect=slow_edit):
            _chat, reply = self.request("boss", "Исправь рабочий документ", behavior, chat)
            self.assertEqual(reply["status"], "error")
            first = self.con.execute("SELECT id FROM gpt_messages WHERE chat_id=? AND role='user'", (chat,)).fetchone()[0]
            self.service.manage_chat("boss", chat, "rollback", first)
            release.set()
            self.assertTrue(done.wait(3))
        self.assertEqual(len(self.service.documents.listing(chat)), 1)
        self.assertTrue(errors)

    def test_new_image_is_attached_to_native_turn_once(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (40,40), "red").save(stream, format="PNG")
        chat = self.service.create_chat("boss")["id"]
        self.service.documents.save(chat, "photo.png", stream.getvalue())
        def behavior(args, payload):
            self.assertEqual(sum(item["type"] == "image" for item in args["input"]),1)
            return {"kind":"answer","answer":"Изображение прочитано","sources":[]}
        _chat, reply = self.request("boss", "Прочитай рабочее изображение", behavior, chat)
        self.assertEqual(reply["status"], "completed")
        def continuation(args, payload):
            self.assertFalse(any(item["type"] == "image" for item in args["input"]))
            return {"kind":"answer","answer":"Продолжение","sources":[]}
        self.request("boss", "Продолжим", continuation, chat)

    def test_protected_image_sends_only_masked_text_to_native_turn(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (40,40), "white").save(stream, format="PNG")
        chat = self.service.create_chat("boss")["id"]
        self.service.documents.save(chat, "Паспорт.png", stream.getvalue(), protected=True)
        def behavior(args, payload):
            self.assertTrue(all(item["type"] == "text" for item in args["input"]))
            self.assertNotIn("4512", json.dumps(args["input"]))
            self.assertNotIn("345678", json.dumps(args["input"]))
            return {"kind":"answer","answer":"Документ проверен","sources":[]}
        with patch("app.gpt_images.local_ocr", return_value="Серия\n4512\nНомер\n345678"):
            _chat, reply = self.request("boss", "Проверь рабочий документ", behavior, chat)
        self.assertEqual(reply["status"], "completed")
