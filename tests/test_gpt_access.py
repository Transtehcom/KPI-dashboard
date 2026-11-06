import copy
import json
import unittest
from unittest.mock import Mock, patch

from app.common import connect, load_rules
from app.gpt_access import Access
from app.gpt_privacy import PrivacyContext
from app.gpt_tools import DataTools
from tests.test_engine import CONFIG


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(CONFIG)
        self.config["ttkgpt_runtime"] = "legacy"
        self.config["bitrix24"] = {"webhook": "https://crm.example.test/rest/1/secret/"}
        self.config["employees"] += [{"login": "logist", "name": "Логист", "role": "employee", "position": "logistics", "b24_user_id": 88}]
        for employee in self.config["employees"]:
            if employee["login"] == "pm":
                employee["b24_user_id"] = 14
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.p = PrivacyContext(documents_only=True)

    def tools(self, login):
        return DataTools(self.con, self.config, load_rules(), access=Access(self.config, login))

    def test_position_and_user_checks_fail_closed(self):
        sales = Access(self.config, "pm")
        self.assertTrue(sales.allows_intent({"domains": ["sales"], "other_people": False}))
        self.assertFalse(sales.allows_intent({"domains": ["logistics"], "other_people": False}))
        self.assertFalse(sales.allows_intent({"domains": ["self"], "other_people": True}))
        self.assertFalse(Access(self.config, "logist").allows_intent({"domains": ["sales"], "other_people": False}))
        with self.assertRaises(ValueError):
            Access(self.config, "unknown")

    def test_search_filters_rows_and_totals_and_employees(self):
        self.con.execute("INSERT INTO clients(id,emp,name) VALUES ('own','pm','Клиент'),('other','tender','Клиент')")
        self.con.execute("INSERT INTO forwarder_orders(id,emp,doc,route) VALUES ('truck','logist','Клиент','Маршрут')")
        tools = self.tools("pm")
        result = tools.execute("search", "Клиент", "", "2026-11", self.p)
        self.assertEqual(result["totals"]["clients"], 1)
        self.assertEqual([r["id"] for r in result["rows"]], ["own"])
        self.assertNotIn("forwarder_orders", result["totals"])
        people = tools.execute("employees", "", "", "2026-11", self.p)
        self.assertEqual(len(people["employees"]), 1)
        with self.assertRaises(ValueError):
            tools.execute("kpi", "", "tender", "2026-11", self.p)
        with self.assertRaises(ValueError):
            tools.execute("money", "", "", "2026-11", self.p)

    def test_calls_and_calendar_cannot_target_other_employee(self):
        self.con.execute("INSERT INTO call_records(id,emp,dt) VALUES ('own','pm','2026-11-06'),('other','tender','2026-11-06')")
        tools = self.tools("pm")
        result = tools.execute("call_detail", "other", "", "2026-11", self.p)
        self.assertEqual(result["calls"], [])
        with self.assertRaises(ValueError):
            tools.execute("calendar", '{"user_id":"99"}', "", "2026-11", self.p)

    def test_b24_filter_is_forced_and_foreign_card_is_blocked(self):
        tools = self.tools("pm")
        tools.live.b24 = Mock()
        tools.live.b24.call.return_value = {"result": [{"ID": "1", "TITLE": "Own", "ASSIGNED_BY_ID": "14"}], "total": 1}
        result = tools.execute("b24_search", '{"entity":"company","responsible":"99"}', "", "2026-11", self.p)
        self.assertEqual(tools.live.b24.call.call_args.args[1]["filter"]["ASSIGNED_BY_ID"], "14")
        self.assertEqual(len(result["rows"]), 1)
        tools.live.b24.call.return_value = {"result": {"ID": "2", "TITLE": "SECRET", "ASSIGNED_BY_ID": "99"}}
        with self.assertRaises(ValueError):
            tools.execute("b24_card", '{"entity":"company","id":"2"}', "", "2026-11", self.p)
        with self.assertRaises(ValueError):
            self.tools("logist").execute("b24_search", '{"entity":"deal"}', "", "2026-11", self.p)

    def test_onec_owner_filter_and_fields_cannot_be_overridden(self):
        tools = self.tools("pm")
        tools.live._metadata = {"Document_ЗаказКлиента": {"fields": {"Ref_Key": "Edm.Guid", "Number": "Edm.String", "СуммаДокумента": "Edm.Decimal", "ТТК_ДатаДоставки": "Edm.DateTime"}, "references": {}}}
        import time
        tools.live._metadata_at = time.monotonic()
        tools.live.onec = Mock()
        tools.live.onec._odata.return_value = []
        tools.execute("onec_search", json.dumps({"entity": "Document_ЗаказКлиента", "text": "123"}), "", "2026-11", self.p)
        _entity, args = tools.live.onec._odata.call_args.args
        self.assertIn("Менеджер/Description eq 'ПМ'", args["$filter"])
        self.assertNotIn("ТТК_ДатаДоставки", args["$select"])
        with self.assertRaises(ValueError):
            tools.execute("onec_search", json.dumps({"entity": "Document_ЗаказКлиента", "fields": ["ТТК_ДатаДоставки"], "text": "123"}), "", "2026-11", self.p)
        with self.assertRaises(ValueError):
            tools.execute("onec_card", json.dumps({"entity": "Catalog_Пользователи", "id": "123"}), "", "2026-11", self.p)

    def test_guid_mapping_uses_onec_owner_key(self):
        employee = next(e for e in self.config["employees"] if e["login"] == "pm")
        employee["onec_manager"] = "12345678-1234-1234-1234-123456789abc"
        _fields, where = Access(self.config, "pm").onec_scope("Document_ЗаказКлиента")
        self.assertIn("Менеджер_Key eq guid'12345678-1234-1234-1234-123456789abc'", where)
        self.assertNotIn("/Description", where)

    def test_existing_recording_permission_is_preserved(self):
        self.config["call_records"] = {"listen_own": False}
        self.con.execute("INSERT INTO call_records(id,emp,dt) VALUES ('own','pm','2026-11-06')")
        self.con.execute("INSERT INTO call_transcripts(id,segments) VALUES ('own',?)", (json.dumps([[0, "SECRET_TRANSCRIPT"]]),))
        tools = self.tools("pm")
        with self.assertRaisesRegex(ValueError, "Нет прав"):
            tools.execute("call_detail", "own", "", "2026-11", self.p)
        result = tools.execute("calls", "", "", "2026-11", self.p)
        self.assertIsNone(result["calls"][0]["analysis"])

    def test_admin_chat_and_files_are_hidden_after_role_change(self):
        import base64
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, self.config, load_rules())
        chat = service.create_chat("boss")
        doc = service.document_upload("boss", chat["id"], "notes.txt", base64.b64encode(b"admin-data").decode())
        boss = next(e for e in self.config["employees"] if e["login"] == "boss")
        boss.update(role="employee", position="pm")
        self.assertEqual(service.chats("boss")["chats"], [])
        for call in (lambda: service.chat("boss", chat["id"]),
                     lambda: service.document_download("boss", chat["id"], doc["id"]),
                     lambda: service.preview("boss", chat["id"], 1)):
            with self.assertRaises(ValueError):
                call()
        self.assertTrue(service.create_chat("boss")["id"])

    def test_semantic_denial_stops_before_business_data_or_documents(self):
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, self.config, load_rules())
        chat = service.create_chat("pm")
        with patch("app.gpt_assistant.call_analysis.run_codex", return_value={"thread_id":"11111111-1111-4111-8111-111111111111","result": {"domains": ["logistics"], "other_people": False}}) as model, patch.object(service.documents, "tool") as documents:
            job = service.submit("pm", chat["id"], "Скоро ли приедет машина?", launch=False)
            service.work(chat["id"], job["id"])
        result = service.chat("pm", chat["id"])["messages"][-1]
        self.assertEqual(result["status"], "completed")
        self.assertIn("пределы вашего доступа", result["content"])
        self.assertEqual(model.call_count, 1)
        documents.assert_not_called()

    def test_staff_normal_entry_only_sends_own_rows_to_model(self):
        from app.gpt_assistant import GPTAssistant
        self.con.execute("INSERT INTO clients(id,emp,name) VALUES ('own','pm','OWN_CLIENT'),('other','tender','FOREIGN_CLIENT')")
        service = GPTAssistant(self.con, self.config, load_rules())
        chat = service.create_chat("pm")
        base = {"kind": "tool", "tool": "search", "query": "CLIENT", "employee": "", "month": "", "answer": "", "sources": []}
        prompts = []
        def model(prompt, config, **kwargs):
            prompts.append(prompt)
            self.assertNotIn("FOREIGN_CLIENT", prompt)
            if len(prompts) == 1:
                return {"thread_id":"11111111-1111-4111-8111-111111111111","result": {"domains": ["sales"], "other_people": False}}
            if len(prompts) == 2:
                return {"thread_id":"11111111-1111-4111-8111-111111111111","result": base}
            self.assertIn("OWN_CLIENT", prompt)
            return {"thread_id":"11111111-1111-4111-8111-111111111111","result": {**base, "kind": "answer", "answer": "Найден ваш клиент", "tool": "", "query": ""}}
        with patch("app.gpt_assistant.call_analysis.run_codex", side_effect=model):
            job = service.submit("pm", chat["id"], "Найди моих клиентов", launch=False)
            service.work(chat["id"], job["id"])
        self.assertEqual(service.chat("pm", chat["id"])["messages"][-1]["status"], "completed")
        self.assertEqual(len(prompts), 3)

    def test_role_change_during_model_call_does_not_publish_old_access_answer(self):
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, self.config, load_rules())
        chat = service.create_chat("boss")
        def model(*args, **kwargs):
            boss = next(e for e in self.config["employees"] if e["login"] == "boss")
            boss.update(role="employee", position="pm")
            return {"thread_id":"11111111-1111-4111-8111-111111111111","result": {"kind": "answer", "tool": "", "query": "", "employee": "", "month": "", "answer": "SECRET_ADMIN_RESULT", "sources": []}}
        with patch("app.gpt_assistant.call_analysis.run_codex", side_effect=model):
            job = service.submit("boss", chat["id"], "Сводка по компании", launch=False)
            service.work(chat["id"], job["id"])
        row = self.con.execute("SELECT status,content FROM gpt_messages WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "error")
        self.assertNotIn("SECRET_ADMIN_RESULT", row["content"])
        with self.assertRaises(ValueError):
            service.chat("boss", chat["id"])

    def test_owned_task_can_include_full_cross_functional_description(self):
        tools = self.tools("pm")
        tools.live.b24 = Mock()
        tools.live.b24.call.side_effect = [
            {"result": {"task": {"id": "17", "responsibleId": "14", "title": "Моя задача", "description": "Прибытие машины в 17:00"}}},
            {"result": {"fields": {}}},
        ]
        result = tools.execute("b24_card", '{"entity":"task","id":"17"}', "", "2026-11", self.p)
        self.assertIn("Прибытие машины", json.dumps(result, ensure_ascii=False))
