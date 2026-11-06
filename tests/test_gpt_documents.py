import base64
import io
import json
import unittest

from app.common import connect
from app.gpt_documents import Documents
from app.gpt_privacy import PrivacyContext


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO gpt_chats(id,owner,title) VALUES ('chat','boss','test')")
        self.docs = Documents(self.con)
        self.privacy = PrivacyContext(documents_only=True)

    def upload(self, name, raw):
        return self.docs.upload("chat", name, base64.b64encode(raw).decode())

    def test_text_edit_makes_copy_and_checks_old_text(self):
        original = self.upload("notes.txt", "Первая строка\r\nВторая".encode())
        query = {"id": original["id"], "changes": [{"unit": "line:1", "old": "Первая", "new": "Новая"}]}
        result = self.docs.tool("document_edit", "chat", query, self.privacy)
        self.assertEqual(self.docs.download("chat", original["id"])[1], "Первая строка\r\nВторая".encode())
        self.assertEqual(self.docs.download("chat", result["document"]["id"])[1], "Новая строка\r\nВторая".encode())
        with self.assertRaises(ValueError):
            self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "line:1", "old": "Несуществующая", "new": "x"}]}, self.privacy)

    def test_docx_keeps_runs_and_masks_contract(self):
        from docx import Document
        doc = Document()
        para = doc.add_paragraph()
        para.add_run("Анна ").bold = True
        para.add_run("Мерзлякова ИНН 7701234567")
        out = io.BytesIO()
        doc.save(out)
        original = self.upload("Договор.docx", out.getvalue())
        read = self.docs.tool("document_read", "chat", {"id": original["id"]}, self.privacy)
        self.assertNotIn("7701234567", json.dumps(read))
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "p:1", "old": "Анна", "new": "Мария"}]}, self.privacy)
        edited = Document(io.BytesIO(self.docs.download("chat", result["document"]["id"])[1]))
        self.assertTrue(edited.paragraphs[0].runs[0].bold)
        self.assertEqual(edited.paragraphs[0].text, "Мария Мерзлякова ИНН 7701234567")

    def test_xlsx_preserves_formula_and_format(self):
        from openpyxl import Workbook, load_workbook
        book = Workbook()
        book.active.title = "План"
        book.active["A1"] = 100
        book.active["A1"].number_format = "0.00"
        book.active["B1"] = "=A1*2"
        stream = io.BytesIO()
        book.save(stream)
        original = self.upload("plan.xlsx", stream.getvalue())
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "План!A1", "old": "100", "new": 125}]}, self.privacy)
        edited = load_workbook(io.BytesIO(self.docs.download("chat", result["document"]["id"])[1]))
        self.assertEqual(edited.active["A1"].value, 125)
        self.assertEqual(edited.active["A1"].number_format, "0.00")
        self.assertEqual(edited.active["B1"].value, "=A1*2")

    def test_wrong_chat_and_invalid_upload_are_rejected(self):
        document = self.upload("notes.txt", b"hello")
        with self.assertRaises(ValueError):
            self.docs.download("other", document["id"])
        for name, raw in (("run.exe", b"binary"), ("bad.docx", b"not a ZIP")):
            with self.assertRaises(ValueError):
                self.upload(name, raw)

    def test_read_is_paginated_and_document_limit_is_enforced(self):
        document = self.upload("notes.txt", b"a\nb\nc")
        result = self.docs.tool("document_read", "chat", {"id": document["id"], "offset": 1}, self.privacy)
        self.assertEqual(result["units"][0]["id"], "line:2")
        with self.assertRaises(ValueError):
            self.docs.tool("document_read", "chat", {"id": document["id"], "offset": -1}, self.privacy)

    def test_protected_spreadsheet_sheet_names_are_not_sent_raw(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = "Иванов ИНН 7701234567"
        book.active["A1"] = "Реквизиты"
        raw = io.BytesIO()
        book.save(raw)
        document = self.upload("Договор.xlsx", raw.getvalue())
        result = self.docs.tool("document_read", "chat", {"id": document["id"]}, self.privacy)
        self.assertNotIn("7701234567", json.dumps(result))

    def test_spreadsheet_split_labels_hide_requisites_and_credentials_in_prompt(self):
        from openpyxl import Workbook
        from unittest.mock import patch
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        book = Workbook()
        sheet = book.active
        for row in (("КПП", "770101001"), ("БИК", "044525225"), ("Пароль", "unconfiguredCredential"), ("Сумма", 1234567890)):
            sheet.append(row)
        raw = io.BytesIO()
        book.save(raw)
        service = GPTAssistant(self.con, {"employees": []}, load_rules())
        doc = service.document_upload("boss", "chat", "Договор.xlsx", base64.b64encode(raw.getvalue()).decode())
        prompts = []
        def model(prompt, *args, **kwargs):
            prompts.append(prompt)
            result = {"kind": "answer", "tool": "", "query": "", "employee": "", "month": "", "answer": "Прочитано", "sources": []}
            if len(prompts) == 1:
                result.update(kind="tool", tool="document_read", query=json.dumps({"id": doc["id"]}))
            return {"result": result}
        with patch("app.gpt_assistant.call_analysis.run_codex", side_effect=model):
            job = service.submit("boss", "chat", "Прочитай договор", launch=False)
            service.work("chat", job["id"])
        self.assertEqual(len(prompts), 2)
        for value in ("770101001", "044525225", "unconfiguredCredential"):
            self.assertNotIn(value, prompts[-1])
        self.assertIn("1234567890", prompts[-1])

    def test_text_append_and_docx_cross_run_replace(self):
        from docx import Document
        original = self.upload("notes.txt", b"first")
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "append", "old": "", "new": "second"}]}, self.privacy)
        self.assertEqual(self.docs.download("chat", result["document"]["id"])[1], b"first\nsecond")
        document = Document()
        para = document.add_paragraph()
        para.add_run("begin OLD ").bold = True
        para.add_run("TEXT end").italic = True
        raw = io.BytesIO()
        document.save(raw)
        original = self.upload("notes.docx", raw.getvalue())
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "p:1", "old": "OLD TEXT", "new": "new"}, {"unit": "append", "old": "", "new": "extra"}]}, self.privacy)
        edited = Document(io.BytesIO(self.docs.download("chat", result["document"]["id"])[1]))
        self.assertEqual(edited.paragraphs[0].text, "begin new end")
        self.assertTrue(edited.paragraphs[0].runs[0].bold)
        self.assertTrue(edited.paragraphs[0].runs[1].italic)
        self.assertEqual(edited.paragraphs[-1].text, "extra")

    def test_append_does_not_change_existing_header_unit_ids(self):
        from docx import Document
        document = Document()
        document.add_paragraph("Body")
        document.sections[0].header.paragraphs[0].text = "Header"
        raw = io.BytesIO()
        document.save(raw)
        original = self.upload("notes.docx", raw.getvalue())
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": "append", "old": "", "new": "Appendix"}, {"unit": "p:2", "old": "Header", "new": "Updated"}]}, self.privacy)
        edited = Document(io.BytesIO(self.docs.download("chat", result["document"]["id"])[1]))
        self.assertEqual(edited.paragraphs[-1].text, "Appendix")
        self.assertEqual(edited.sections[0].header.paragraphs[0].text, "Updated")

    def test_csv_semantics_protect_split_requisites_but_keep_amounts(self):
        for text in ("КПП;БИК;Сумма\n770101001;044525225;1234567890", "КПП,770101001\nБИК,044525225\nСумма,1234567890"):
            original = self.upload("Договор.csv", text.encode())
            result = self.docs.tool("document_read", "chat", {"id": original["id"]}, self.privacy)
            encoded = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("770101001", encoded)
            self.assertNotIn("044525225", encoded)
            self.assertIn("1234567890", encoded)

    def test_csv_returned_block_can_be_used_for_whole_block_edit(self):
        original = self.upload("notes.csv", b'a,"plain"\n')
        read = self.docs.tool("document_read", "chat", {"id": original["id"]}, self.privacy)
        unit = read["units"][0]
        result = self.docs.tool("document_edit", "chat", {"id": original["id"], "changes": [{"unit": unit["id"], "old": unit["text"], "new": "a,new"}]}, self.privacy)
        self.assertEqual(self.docs.download("chat", result["document"]["id"])[1], b"a,new\n")

    def test_scanned_pdf_and_oversized_zip_are_rejected(self):
        import zipfile
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        raw = io.BytesIO()
        writer.write(raw)
        with self.assertRaisesRegex(ValueError, "текстового слоя"):
            self.upload("scan.pdf", raw.getvalue())
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            for index in range(1501):
                archive.writestr(str(index), "")
        with self.assertRaisesRegex(ValueError, "слишком велик"):
            self.upload("large.docx", raw.getvalue())

    def test_text_pdf_is_readable_and_read_only(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(200, 100)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 50 Td (Hello PDF) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        raw = io.BytesIO()
        writer.write(raw)
        document = self.upload("text.pdf", raw.getvalue())
        result = self.docs.tool("document_read", "chat", {"id": document["id"]}, self.privacy)
        self.assertIn("Hello PDF", result["units"][0]["text"])
        with self.assertRaisesRegex(ValueError, "только для чтения"):
            self.docs.tool("document_edit", "chat", {"id": document["id"], "changes": [{"unit": "page:1:1", "old": "Hello", "new": "Bye"}]}, self.privacy)

    def test_service_document_loop_masks_input_and_produces_downloadable_copy(self):
        from unittest.mock import patch
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"employees": []}, load_rules())
        document = service.document_upload("boss", "chat", "Договор.txt", base64.b64encode("Стоимость 100 рублей\nИНН 7701234567".encode()).decode())
        with self.assertRaises(ValueError):
            service.document_download("other", "chat", document["id"])
        prompts = []
        base = {"kind": "tool", "tool": "document_read", "query": json.dumps({"id": document["id"]}), "employee": "", "month": "", "answer": "", "sources": []}
        def model(prompt, *args, **kwargs):
            prompts.append(prompt)
            self.assertNotIn("7701234567", prompt)
            if len(prompts) == 1:
                return {"result": base}
            if len(prompts) == 2:
                data = json.loads(prompt.split("ДАННЫЕ:\n", 1)[1])
                unit = data["tool_results"][-1]["result"]["units"][0]["id"]
                return {"result": {**base, "tool": "document_edit", "query": json.dumps({"id": document["id"], "changes": [{"unit": unit, "old": "100", "new": "200"}]})}}
            return {"result": {**base, "kind": "answer", "tool": "", "query": "", "answer": "Исправленная версия готова"}}
        with patch("app.gpt_assistant.call_analysis.run_codex", side_effect=model):
            job = service.submit("boss", "chat", "Измени стоимость со 100 до 200 рублей в договоре", launch=False)
            service.work("chat", job["id"])
        result = service.chat("boss", "chat")
        self.assertEqual(result["messages"][-1]["status"], "completed")
        self.assertEqual(len(result["documents"]), 2)
        name, raw = service.documents.download("chat", result["documents"][-1]["id"])
        self.assertIn("200", raw.decode())
        self.assertIn("7701234567", raw.decode())
