import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from docx import Document

from app import gpt_office


def docx_bytes(text="Office example"):
    document = Document()
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


class OfficeTest(unittest.TestCase):
    def test_native_document_passes_through_without_starting_office(self):
        raw = docx_bytes()
        with patch.object(gpt_office, "discover") as discover:
            self.assertEqual(gpt_office.normalize("пример.DOCX", raw), ("пример.DOCX", raw))
        discover.assert_not_called()

    def test_legacy_files_map_to_matching_native_format(self):
        expected = {"doc": "docx", "rtf": "docx", "odt": "docx", "xls": "xlsx",
                    "ods": "xlsx", "ppt": "pptx", "odp": "pptx"}
        for source, target in expected.items():
            with self.subTest(source=source), patch.object(gpt_office, "convert", return_value=b"converted") as convert:
                self.assertEqual(gpt_office.normalize("пример." + source, b"input"),
                                 ("пример." + target, b"converted"))
                convert.assert_called_once_with("пример." + source, b"input", "." + target, gpt_office.MAX_BYTES)

    def test_html_is_extracted_locally_without_resource_requests(self):
        raw = ('<html><head><title>Hidden title</title></head><body>'
               '<script>secret()</script><style>secret style</style><h1>Заголовок</h1>'
               '<p>Текст &amp; <a href="https://example.com">ссылка</a></p>'
               '<img src="https://example.com/track.png">'
               '<table><tr><td>A</td><td>B</td></tr></table></body></html>').encode()
        with patch.object(gpt_office, "discover") as discover:
            name, result = gpt_office.normalize("test.html", raw)
        self.assertEqual(name, "test.docx")
        text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(result)).paragraphs)
        self.assertIn("Заголовок", text)
        self.assertIn("Текст & ссылка", text)
        self.assertIn("A\tB", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("Hidden title", text)
        self.assertNotIn("https://", text)
        discover.assert_not_called()

    def test_html_has_visible_text_limit(self):
        with self.assertRaisesRegex(ValueError, "250 000"):
            gpt_office.normalize("test.htm", b"<p>" + b"a" * 250001 + b"</p>")

    def test_empty_oversized_and_invalid_archives_are_rejected(self):
        for raw, name, limit in [(b"", "test.doc", 10), (b"too large", "test.doc", 1),
                                 (b"invalid zip", "test.docx", 100)]:
            with self.subTest(name=name, raw=raw), self.assertRaises(ValueError):
                gpt_office.normalize(name, raw, limit)

    def test_cross_family_conversion_is_rejected(self):
        with self.assertRaises(ValueError):
            gpt_office.convert("test.doc", b"input", "xlsx")

    def test_missing_local_converter_has_actionable_error(self):
        with patch.object(gpt_office, "discover", return_value={}):
            with self.assertRaisesRegex(ValueError, "Word.*LibreOffice"):
                gpt_office.convert("test.doc", b"input", "pdf")

    def test_convert_validates_generated_file_and_bounds(self):
        def office(source, output, owner, application):
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(application, "Word")
            output.write_bytes(b"%PDF-1.7\nexample")
            return 0

        with patch.object(gpt_office, "discover", return_value={"Word": "word.exe"}), \
                patch.object(gpt_office, "_office", side_effect=office):
            self.assertEqual(gpt_office.convert("test.doc", b"original", "pdf"), b"%PDF-1.7\nexample")
            with self.assertRaisesRegex(ValueError, "размер"):
                gpt_office.convert("test.doc", b"original", "pdf", max_bytes=10)

    def test_failed_or_missing_output_does_not_return_partial_file(self):
        for returncode in (0, 1):
            with self.subTest(code=returncode), \
                    patch.object(gpt_office, "discover", return_value={"Word": "word.exe"}), \
                    patch.object(gpt_office, "_office", return_value=returncode):
                with self.assertRaisesRegex(ValueError, "Не удалось преобразовать"):
                    gpt_office.convert("test.doc", b"original", "pdf")

    def test_busy_existing_office_uses_private_libreoffice_when_available(self):
        def libreoffice(executable, source, output):
            output.write_bytes(b"%PDF-1.7\nexample")
            return 0

        with patch.object(gpt_office, "discover", return_value={"Word": "word", "LibreOffice": "soffice"}), \
                patch.object(gpt_office, "_office", return_value=73), \
                patch.object(gpt_office, "_libreoffice", side_effect=libreoffice) as fallback:
            self.assertTrue(gpt_office.convert("test.doc", b"input", "pdf").startswith(b"%PDF-"))
            fallback.assert_called_once()

    def test_office_cleanup_runs_even_when_worker_times_out(self):
        with patch.object(gpt_office, "_run", side_effect=ValueError("timeout")), \
                patch.object(gpt_office, "_cleanup_office") as cleanup:
            with self.assertRaisesRegex(ValueError, "timeout"):
                gpt_office._office(Path("source.doc"), Path("result.docx"), Path("owner.json"), "Word")
            cleanup.assert_called_once_with(Path("owner.json"))

    def test_timeout_kills_only_created_worker(self):
        process = MagicMock()
        process.pid = 12345
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 90), 0]
        process.__enter__.return_value = process
        with patch.object(gpt_office.subprocess, "Popen", return_value=process), \
                patch.object(gpt_office.subprocess, "run") as taskkill, \
                patch.object(gpt_office.os, "killpg", create=True) as killpg:
            with self.assertRaisesRegex(ValueError, "90"):
                gpt_office._run(["worker"])
        process.kill.assert_called_once()
        if gpt_office.os.name == "nt":
            self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
        else:
            self.assertEqual(killpg.call_args.args[0], 12345)


if __name__ == "__main__":
    unittest.main()
