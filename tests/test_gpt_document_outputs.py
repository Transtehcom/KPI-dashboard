import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from app.common import connect, load_rules
from app.gpt_assistant import GPTAssistant
from tests.test_engine import CONFIG


class DocumentOutputTest(unittest.TestCase):
    def run_tool(self, runtime, tool):
        con = connect(":memory:")
        self.addCleanup(con.close)
        config = copy.deepcopy(CONFIG)
        config["ttkgpt_runtime"] = runtime
        service = GPTAssistant(con, config, load_rules())
        chat = service.create_chat("boss")["id"]
        if tool == "document_convert":
            import fitz
            from docx import Document

            with fitz.open() as pdf:
                pdf.new_page().insert_text((50, 50), "Source document")
                source = service.documents.save(chat, "source.pdf", pdf.tobytes())
            stream = io.BytesIO()
            Document().save(stream)
            expected = service.documents.save(chat, "source.docx", stream.getvalue(), source["id"])
            query = {"id": source["id"], "format": "docx"}
        else:
            source = service.documents.save(chat, "source.txt", b"old")
            expected = None
            query = {"id": source["id"], "changes": [{"unit": "line:1", "old": "old", "new": "new"}]}
        message = service.submit("boss", chat, "Подготовь документ", launch=False)
        answer = {"kind": "answer", "answer": "Готово", "sources": []}
        if runtime == "legacy":
            step = {"kind": "tool", "tool": tool, "query": json.dumps(query), "employee": "", "month": "", "answer": "", "sources": []}
            with patch.object(service.sessions, "call", side_effect=[step, {**step, **answer}]):
                service.work(chat, message["id"])
        else:
            server = Mock()

            def run(**kwargs):
                kwargs["on_thread"]("11111111-1111-4111-8111-111111111111")
                kwargs["tool_handler"]("ttk_" + tool, {"query": json.dumps(query)})
                return answer

            server.run.side_effect = run
            with patch("app.gpt_agent.AppServer") as factory:
                factory.return_value.__enter__.return_value = server
                service.work(chat, message["id"])
        reply = service.chat("boss", chat)["messages"][-1]
        self.assertEqual(reply["status"], "completed", reply["content"])
        if expected is None:
            expected = next(row for row in service.documents.listing(chat) if row["parent_id"] == source["id"])
        bound = con.execute("SELECT chat_id,message_id,document_id FROM gpt_document_outputs").fetchall()
        self.assertEqual([tuple(row) for row in bound], [(chat, message["id"], expected["id"])])
        return con, service, chat, message, expected

    def test_cached_conversion_is_bound_to_current_turn_in_both_runtimes(self):
        for runtime in ("appserver", "legacy"):
            with self.subTest(runtime=runtime):
                self.run_tool(runtime, "document_convert")

    def test_edit_output_is_bound_and_removed_with_message_or_document(self):
        for runtime in ("appserver", "legacy"):
            with self.subTest(runtime=runtime):
                con, service, chat, message, document = self.run_tool(runtime, "document_edit")
                con.execute("DELETE FROM gpt_messages WHERE id=?", (message["id"],))
                self.assertEqual(con.execute("SELECT count(*) FROM gpt_document_outputs").fetchone()[0], 0)
                con.execute("INSERT INTO gpt_document_outputs VALUES (?,?,?)", (chat, message["id"], document["id"]))
                service.documents.delete(chat, document["id"])
                self.assertEqual(con.execute("SELECT count(*) FROM gpt_document_outputs").fetchone()[0], 0)
