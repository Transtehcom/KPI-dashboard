import base64
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from app.common import connect
from app.gpt_documents import Documents
from app.gpt_privacy import PrivacyContext


class ImageAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO gpt_chats(id,owner,title) VALUES ('chat','boss','test')")
        self.docs = Documents(self.con)
        self.privacy = PrivacyContext(documents_only=True)

    def image(self, fmt="PNG", size=(40, 30)):
        stream = io.BytesIO()
        extra = {}
        if fmt == "PNG":
            meta = PngImagePlugin.PngInfo()
            meta.add_text("private", "secret metadata")
            extra["pnginfo"] = meta
        Image.new("RGB", size, "white").save(stream, fmt, **extra)
        return stream.getvalue()

    def upload(self, name="photo.png", protected=False, raw=None):
        return self.docs.upload("chat", name, base64.b64encode(raw or self.image()).decode(), protected=protected)

    def test_upload_validates_pixels_and_strips_metadata_before_vision(self):
        document = self.upload()
        self.assertTrue(document["is_image"])
        self.assertEqual((document["width"], document["height"]), (40, 30))
        items = self.docs.image_read("chat", document["id"], self.privacy)
        self.assertEqual(items[0]["type"], "inputImage")
        raw = base64.b64decode(items[0]["imageUrl"].split(",", 1)[1])
        self.assertNotIn(b"secret metadata", raw)
        self.assertEqual(Image.open(io.BytesIO(raw)).format, "PNG")
        for raw in (b"not image", self.image("GIF"), self.image(size=(5000, 4001))):
            with self.assertRaises(ValueError):
                self.upload(raw=raw)
        with self.assertRaises(ValueError):
            self.upload(raw=b"x" * (10 * 1024 * 1024 + 1))

    def test_protected_mode_masks_local_ocr_and_never_returns_image(self):
        document = self.upload(protected=True)
        with patch("app.gpt_images.local_ocr", return_value="ИНН 7701234567\nСтоимость 150 рублей\nКПП\n770101001"):
            items = self.docs.image_read("chat", document["id"], self.privacy)
        encoded = json.dumps(items, ensure_ascii=False)
        self.assertEqual(items[0]["type"], "inputText")
        self.assertNotIn("7701234567", encoded)
        self.assertNotIn("770101001", encoded)
        self.assertNotIn("imageUrl", encoded)
        self.assertIn("150", encoded)

    def test_protected_ocr_masks_inline_passport_and_snils_preserving_amount_dates(self):
        examples = (
            "Номер паспорта: 4512 345678",
            "Серия 4512 Номер 345678",
            "СНИЛС 112-233-445 95",
            "Паспорт РФ № 45 12 345678",
            "Серия паспорта 45 12; номер паспорта 345678",
            "СНИЛС №11223344595",
            "СНИЛС 112‑233‑445\u00a095",
            "Водительское удостоверение №4512345678",
        )
        document = self.upload(protected=True)
        for example in examples:
            with self.subTest(example=example), patch("app.gpt_images.local_ocr", return_value=example + "\nСтоимость 1234567890 рублей\nДата 06.11.2026"):
                content = self.docs.image_read("chat", document["id"], PrivacyContext(documents_only=True))
                text = content[0]["text"]
                masked_identifier = text.splitlines()[1]
                for identifier in ("4512", "345678", "112", "233", "445", "11223344595"):
                    self.assertNotIn(identifier, masked_identifier)
                self.assertNotRegex(masked_identifier, r"\b45\s+12\b")
                self.assertIn("1234567890", text)
                self.assertIn("06.11.2026", text)
                self.assertEqual(content[0]["type"], "inputText")

    def test_personal_identifier_patterns_keep_ordinary_business_text(self):
        text = "Поставка серии 4512 номер 345678. СНИЛС 112-233-445 95. Стоимость 1234567890 рублей, дата 06.11.2026"
        self.assertEqual(PrivacyContext(documents_only=True).mask(text), text)

    def test_protected_ocr_masks_passport_layouts_and_bare_number(self):
        examples = (
            "Серия\n4512\nНомер\n345678",
            "Серия 4512\nНомер 345678",
            "Паспорт гражданина Российской Федерации\n4512 345678",
            "4512 345678",
            "Серия паспорта\n4512\nНомер паспорта\n345678",
            "45 12 345678",
            "Паспорт\n4512\n345678",
        )
        document = self.upload("Паспорт.png")
        for example in examples:
            with self.subTest(example=example), patch("app.gpt_images.local_ocr", return_value=example + "\nСтоимость 1234567890 рублей\nСумма 4512 345678 рублей\nДата 06.11.2026"):
                privacy = PrivacyContext(documents_only=True)
                content = self.docs.image_read("chat", document["id"], privacy)
                text = content[0]["text"]
                body = text.split("Стоимость", 1)[0]
                self.assertNotIn("4512", body)
                self.assertNotIn("345678", body)
                self.assertNotRegex(body, r"\b45\s+12\b")
                self.assertIn("1234567890", text)
                self.assertIn("4512 345678 рублей", text)
                self.assertIn("06.11.2026", text)
                for value in privacy.values.values():
                    self.assertNotRegex(value, r"^\[[A-ZА-ЯЁ_]+_\d+\]$")

    def test_protected_split_company_name_keeps_one_alias(self):
        document = self.upload(protected=True)
        with patch("app.gpt_images.local_ocr", return_value='Контрагент\nООО «Ромашка»'):
            privacy = PrivacyContext(documents_only=True)
            content = self.docs.image_read("chat", document["id"], privacy)
            self.assertNotIn("Ромашка", content[0]["text"])
            for value in privacy.values.values():
                self.assertNotRegex(value, r"^\[[A-ZА-ЯЁ_]+_\d+\]$")

    def test_protected_filename_cannot_be_unprotected_and_ocr_fails_closed(self):
        document = self.upload("Договор.png")
        self.assertTrue(document["protected"])
        self.assertTrue(self.docs.set_protection("chat", document["id"], False)["protected"])
        with patch("app.gpt_images.local_ocr", side_effect=ValueError("OCR unavailable")):
            with self.assertRaisesRegex(ValueError, "OCR unavailable"):
                self.docs.image_read("chat", document["id"], self.privacy)

    def test_protection_persists_and_enforces_chat_ownership(self):
        document = self.upload()
        self.docs.set_protection("chat", document["id"], True)
        self.assertTrue(self.docs.listing("chat")[0]["protected"])
        with self.assertRaises(ValueError):
            self.docs.set_protection("other", document["id"], False)
        with self.assertRaises(ValueError):
            self.docs.image_read("other", document["id"], self.privacy)
        with self.assertRaises(ValueError):
            self.docs.set_protection("chat", document["id"], "false")
        self.docs.delete("chat", document["id"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM gpt_image_metadata").fetchone()[0], 0)
        self.assertEqual(self.docs.history("chat")[0]["id"], document["id"])

    def test_image_read_tool_directs_native_image_read_and_cannot_edit_binary(self):
        document = self.upload()
        result = self.docs.tool("document_read", "chat", {"id": document["id"]}, self.privacy)
        self.assertEqual(result["tool"], "image_read")
        with self.assertRaises(ValueError):
            self.docs.tool("document_edit", "chat", {"id": document["id"], "changes": []}, self.privacy)

    def test_combined_chat_quota_includes_images(self):
        self.con.execute("INSERT INTO gpt_documents VALUES ('full','chat','old.txt',?,'now',NULL,1)", (b"x" * (30 * 1024 * 1024),))
        with self.assertRaisesRegex(ValueError, "30"):
            self.upload()

    def test_service_protection_checks_owner_and_busy_then_invalidates_context(self):
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"employees": [{"login": "boss", "role": "manager"}, {"login": "other", "role": "manager"}]}, load_rules())
        document = self.upload()
        with self.assertRaises(ValueError):
            service.document_protection("other", "chat", document["id"], True)
        job = service.submit("boss", "chat", "Прочитай изображение", launch=False)
        with self.assertRaises(ValueError):
            service.document_protection("boss", "chat", document["id"], True)
        with self.assertRaises(ValueError):
            service.document_upload("boss", "chat", "new.png", base64.b64encode(self.image()).decode())
        self.con.execute("UPDATE gpt_messages SET status='completed' WHERE id=?", (job["id"],))
        service.active.pop("chat", None)
        self.con.execute("INSERT INTO gpt_codex_sessions(chat_id,thread_id,master_hash,scope) VALUES ('chat','old-thread','hash','scope')")
        service.document_protection("boss", "chat", document["id"], True)
        self.assertTrue(self.docs.listing("chat")[0]["protected"])
        self.assertIsNone(self.con.execute("SELECT thread_id FROM gpt_codex_sessions WHERE chat_id='chat'").fetchone()[0])

    def test_bulk_delete_rollback_and_chat_delete_remove_image_metadata(self):
        from app.common import load_rules
        from app.gpt_assistant import GPTAssistant
        service = GPTAssistant(self.con, {"employees": [{"login": "boss", "role": "manager"}]}, load_rules())
        document = self.upload()
        service.manage_chat("boss", "chat", "clear_documents", [document["id"]])
        self.assertEqual(self.con.execute("SELECT count(*) FROM gpt_image_metadata").fetchone()[0], 0)
        self.con.execute("INSERT INTO gpt_messages(chat_id,role,status,content,created) VALUES ('chat','user','completed','Question','2020-01-01T00:00:00.000001')")
        message = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.upload()
        service.manage_chat("boss", "chat", "rollback", message)
        self.assertEqual(self.con.execute("SELECT count(*) FROM gpt_image_metadata").fetchone()[0], 0)
        self.upload()
        service.manage_chat("boss", "chat", "delete")
        self.assertEqual(self.con.execute("SELECT count(*) FROM gpt_image_metadata").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
