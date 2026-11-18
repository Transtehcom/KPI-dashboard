import io
import socket
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

from app.bitrix_files import BitrixFiles, BitrixFileError, message_files, _public_socket


class Response(io.BytesIO):
    def __init__(self, raw, **headers):
        super().__init__(raw)
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name.replace('_', '-')] = str(value)


class BitrixFilesTest(unittest.TestCase):
    def setUp(self):
        self.config = {"bitrix24": {"webhook": "https://portal.example.test/rest/1/private/"}}
        self.api = mock.Mock()
        self.api.call.return_value = {"downloadUrl": "https://portal.example.test/rest/download.json?token=private"}
        self.files = BitrixFiles(self.api, self.config)

    def test_canonical_message_params_file_id_array_is_parsed_and_deduplicated(self):
        data = {"message": {"params": {"FILE_ID": ["138", "139", "138", True, "https://attacker.test/a"]}}}
        self.assertEqual(message_files(data), [{"id": 138}, {"id": 139}])

    def test_file_object_metadata_aliases_merge_without_accepting_urls(self):
        data = {"message": {"params": {"FILE_ID": ["138"], "FILES": {"138": {"name": "../report.pdf", "size": "15"}}},
                            "files": [{"id": 139, "name": "image.png", "size": 20}], "fileIds": [140]},
                "files": [{"ID": "141", "NAME": "info.txt", "SIZE": "30", "urlDownload": "https://evil.test/x"}]}
        self.assertEqual(message_files(data), [{"id": 138, "name": "report.pdf", "size": 15},
            {"id": 139, "name": "image.png", "size": 20}, {"id": 140}, {"id": 141, "name": "info.txt", "size": 30}])

    def test_text_links_and_attach_card_urls_do_not_become_files(self):
        self.assertEqual(message_files({"message": {"text": "https://portal.example.test/file.pdf",
            "params": {"ATTACH": [{"FILE_ID": "138", "URL": "https://evil.test"}]}}}), [])
        self.assertEqual(message_files({"message": {"params": []}}), [])

    def test_download_gets_fresh_bot_scoped_link_and_safe_pdf_filename(self):
        response = Response(b"%PDF-1.7\nexample", Content_Type="application/pdf",
                            Content_Disposition='attachment; filename="../../report.pdf"')
        with mock.patch("urllib.request.OpenerDirector.open", return_value=response) as opened:
            name, raw = self.files.download(95, "56", {"id": 138, "url": "https://evil.test"})
        self.assertEqual((name, raw), ("report.pdf", b"%PDF-1.7\nexample"))
        self.api.call.assert_called_once_with("imbot.v2.File.download", {"botId": 95, "fileId": 138})
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://portal.example.test/rest/download.json?token=private")
        self.assertNotIn("Authorization", request.headers)

    def test_pdf_magic_supplies_extension_when_download_response_has_no_name(self):
        with mock.patch("urllib.request.OpenerDirector.open", return_value=Response(b"%PDF-1.7", Content_Type="application/octet-stream")):
            name, _ = self.files.download(95, "56", {"id": 138})
        self.assertEqual(name, "file_138.pdf")

    def test_long_source_name_stays_within_document_upload_limit(self):
        for source_name in ("a" * 220 + ".pdf", "a" * 220):
            with self.subTest(source_name=source_name):
                with mock.patch("urllib.request.OpenerDirector.open", return_value=Response(b"%PDF-1.7")):
                    name, _ = self.files.download(95, "56", {"id": 138, "name": source_name})
                self.assertLessEqual(len(name), 180)
                self.assertTrue(name.endswith(".pdf"))

    def test_rejects_untrusted_protocol_host_credentials_ports_and_literal_ip(self):
        for url in ("http://portal.example.test/a", "https://portal.example.test.evil/a",
                    "https://user:password@portal.example.test/a", "https://127.0.0.1/a",
                    "https://169.254.169.254/a", "https://[::1]/a", "https://2130706433/a",
                    "https://cdn-ru.bitrix24.ru:444/a", "https://evil.test/a"):
            with self.subTest(url=url):
                self.api.call.return_value = {"downloadUrl": url}
                with mock.patch("urllib.request.OpenerDirector.open") as opened:
                    with self.assertRaises(BitrixFileError) as error:
                        self.files.download(95, "56", {"id": 138})
                opened.assert_not_called()
                self.assertNotIn("password", str(error.exception))

    def test_redirect_to_official_cdn_is_allowed_and_external_redirect_is_rejected(self):
        for location, succeeds in (("https://cdn-ru.bitrix24.ru/b1/file.pdf", True), ("https://evil.test/a", False)):
            with self.subTest(location=location):
                redirect = HTTPError("https://portal.example.test/download", 302, "redirect",
                                     {"Location": location}, io.BytesIO())
                with mock.patch("urllib.request.OpenerDirector.open", side_effect=[redirect, Response(b"%PDF-1.7")]) as opened:
                    if succeeds:
                        self.assertEqual(self.files.download(95, "56", {"id": 138})[1], b"%PDF-1.7")
                        self.assertEqual(opened.call_args.args[0].full_url, location)
                    else:
                        with self.assertRaises(BitrixFileError):
                            self.files.download(95, "56", {"id": 138})
                        self.assertEqual(opened.call_count, 1)

    def test_document_and_image_download_limits_are_applied_to_actual_bytes(self):
        for raw, name, succeeds in ((b"%PDF-" + b"x" * (3 * 1024 * 1024), "file.pdf", False),
                                  (b"\x89PNG\r\n\x1a\n" + b"x" * (4 * 1024 * 1024), "image.png", True),
                                  (b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024), "image.png", False)):
            with self.subTest(name=name, size=len(raw)):
                with mock.patch("urllib.request.OpenerDirector.open", return_value=Response(raw)):
                    if succeeds:
                        self.assertEqual(len(self.files.download(95, "56", {"id": 138, "name": name})[1]), len(raw))
                    else:
                        with self.assertRaises(BitrixFileError):
                            self.files.download(95, "56", {"id": 138, "name": name})

    def test_html_login_and_json_error_are_rejected_and_network_errors_are_safe(self):
        for content_type in ("text/html", "application/json"):
            with mock.patch("urllib.request.OpenerDirector.open", return_value=Response(b"private", Content_Type=content_type)):
                with self.assertRaises(BitrixFileError):
                    self.files.download(95, "56", {"id": 138})
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("token=private")):
            with self.assertRaises(BitrixFileError) as error:
                self.files.download(95, "56", {"id": 138})
        self.assertNotIn("private", str(error.exception))

    def test_dns_private_destination_is_rejected_before_opening_a_socket(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch("socket.getaddrinfo", return_value=answer), mock.patch("socket.socket") as created:
            with self.assertRaises(BitrixFileError):
                _public_socket(("portal.example.test", 443), 10, None)
        created.assert_not_called()


if __name__ == "__main__":
    unittest.main()
