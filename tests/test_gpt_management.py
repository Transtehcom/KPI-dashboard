import base64
import copy
import unittest

from app.common import connect, load_rules
from app.gpt_assistant import GPTAssistant
from tests.test_engine import CONFIG


class ChatManagementTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.service = GPTAssistant(self.con, copy.deepcopy(CONFIG), load_rules())
        self.chat = self.service.create_chat("pm")["id"]

    def upload(self, name="notes.txt"):
        return self.service.document_upload("pm", self.chat, name, base64.b64encode(b"text").decode())

    def turn(self, text):
        reply = self.service.submit("pm", self.chat, text, launch=False)
        self.con.execute("UPDATE gpt_messages SET status='completed' WHERE id=?", (reply["id"],))
        self.con.commit()
        self.service.active.pop(self.chat)
        return reply["id"] - 1

    def test_rename_group_and_ownership(self):
        for action, value in (("rename", "Рабочий чат"), ("group", "Продажи")):
            self.service.manage_chat("pm", self.chat, action, value)
            with self.assertRaises(ValueError):
                self.service.manage_chat("boss", self.chat, action, value)
        item = self.service.chats("pm")["chats"][0]
        self.assertEqual((item["title"], item["group_name"]), ("Рабочий чат", "Продажи"))
        self.service.manage_chat("pm", self.chat, "group", "")
        self.assertEqual(self.service.chats("pm")["chats"][0]["group_name"], "")

    def test_bulk_cleanup_is_atomic_scoped_and_has_history(self):
        first, second = self.upload(), self.upload("other.txt")
        with self.assertRaises(ValueError):
            self.service.manage_chat("pm", self.chat, "clear_documents", [first["id"], "missing"])
        self.assertEqual(len(self.service.documents.listing(self.chat)), 2)
        self.service.manage_chat("pm", self.chat, "clear_documents", [first["id"]])
        data = self.service.chat("pm", self.chat)
        self.assertEqual([r["id"] for r in data["documents"]], [second["id"]])
        self.assertEqual(data["document_history"][0]["id"], first["id"])
        self.assertNotIn("content", data["document_history"][0])

    def test_rollback_removes_tail_and_new_files_and_invalidates_session(self):
        before = self.upload()
        first = self.turn("Первый")
        target = self.turn("Второй")
        after = self.upload("after.txt")
        self.turn("Третий")
        self.service.sessions.begin(self.chat, "scope", "master", 99)
        self.service.sessions.finish(self.chat, 99)
        self.service.manage_chat("pm", self.chat, "rollback", target)
        data = self.service.chat("pm", self.chat)
        self.assertEqual([m["id"] for m in data["messages"]], [first, first + 1])
        self.assertEqual([d["id"] for d in data["documents"]], [before["id"]])
        self.assertEqual(data["document_history"][0]["id"], after["id"])
        self.assertIsNone(self.con.execute("SELECT 1 FROM gpt_codex_sessions WHERE chat_id=?", (self.chat,)).fetchone())

    def test_legacy_rollback_preserves_ambiguous_same_second_attachment(self):
        before = self.upload()
        target = self.turn("Старый запрос")
        after = self.upload("after.txt")
        self.con.execute("UPDATE gpt_messages SET created='2026-11-06T12:00:00' WHERE id=?", (target,))
        self.con.execute("UPDATE gpt_documents SET created='2026-11-06T12:00:00.100000' WHERE id=?", (before["id"],))
        self.con.execute("UPDATE gpt_documents SET created='2026-11-06T12:00:01.100000' WHERE id=?", (after["id"],))
        self.con.commit()
        self.service.manage_chat("pm", self.chat, "rollback", target)
        self.assertEqual([d["id"] for d in self.service.documents.listing(self.chat)], [before["id"]])

    def test_busy_chat_rejects_destructive_actions(self):
        doc = self.upload()
        self.service.submit("pm", self.chat, "Тест", launch=False)
        for action, value in (("delete", None), ("rollback", 1), ("clear_documents", [doc["id"]])):
            with self.assertRaises(ValueError):
                self.service.manage_chat("pm", self.chat, action, value)

    def test_delete_removes_owned_chat_data_only(self):
        other = self.service.create_chat("boss")["id"]
        self.upload()
        self.turn("Привет")
        self.service.manage_chat("pm", self.chat, "group", "Продажи")
        self.service.manage_chat("pm", self.chat, "delete")
        with self.assertRaises(ValueError):
            self.service.chat("pm", self.chat)
        for table in ("gpt_messages", "gpt_documents", "gpt_document_deletions", "gpt_chat_scopes", "gpt_chat_groups", "gpt_codex_sessions"):
            self.assertEqual(self.con.execute(f"SELECT count(*) FROM {table} WHERE chat_id=?", (self.chat,)).fetchone()[0], 0)
        self.assertEqual(self.service.chat("boss", other)["id"], other)

    def test_invalid_actions_and_ranges_do_not_change_data(self):
        first = self.turn("Оставить")
        for action, value in (("rename", " "), ("group", "x" * 81), ("rollback", first + 1), ("rollback", "1"), ("rollback", 999), ("clear_documents", []), ("unknown", None)):
            with self.assertRaises(ValueError):
                self.service.manage_chat("pm", self.chat, action, value)
        self.assertEqual(len(self.service.chat("pm", self.chat)["messages"]), 2)
