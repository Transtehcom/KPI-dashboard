import copy
import datetime as dt
import io
import json
import sqlite3
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs

from app import bitrix_app


class BitrixAppTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.config = {
            "bitrix24": {"webhook": "https://portal.example.test/rest/1/webhook-secret/"},
            "bitrix_app": {"client_id": "local.expected"},
        }
        self.fields = {
            "DOMAIN": "portal.example.test", "AUTH_ID": "access-private",
            "REFRESH_ID": "refresh-private", "APPLICATION_TOKEN": "application-private",
            "member_id": "member-expected", "APPLICATION_SCOPE": "forged-scope",
        }
        self.info = {
            "result": {
                "client_id": "local.expected", "user_id": "7", "scope": "app",
                "expires": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
                "install": {
                    "domain": "portal.example.test", "uri": "https://portal.example.test",
                    "client_endpoint": "https://portal.example.test/rest/",
                    "member_id": "member-expected", "scope": "im,imbot",
                    "installed": False, "status": "L", "version": "1", "member_type": "C",
                },
            },
        }
        self.profile = {"result": {"ID": "7", "ADMIN": True, "NAME": "Тест <script>", "LAST_NAME": "Пользователь"}}
        self.requests = []
        patcher = mock.patch("urllib.request.OpenerDirector.open", new=self.http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = bitrix_app.BitrixApp(self.con, self.config)

    def http(self, request, timeout):
        self.requests.append(request)
        self.assertEqual(parse_qs(request.data.decode())["auth"], ["access-private"])
        self.assertNotIn("?", request.full_url)
        self.assertEqual(request.get_method(), "POST")
        if request.full_url == "https://oauth.bitrix24.tech/rest/app.info/":
            return io.BytesIO(json.dumps(self.info).encode())
        if request.full_url == "https://portal.example.test/rest/profile.json":
            return io.BytesIO(json.dumps(self.profile).encode())
        raise AssertionError("Untrusted outbound destination")

    def installed(self):
        return self.con.execute("SELECT * FROM bitrix_app_installation").fetchall()

    def test_pending_configuration_never_saves_tokens_or_finishes(self):
        self.config["bitrix_app"].clear()
        status, html = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 200)
        self.assertIn("client_id", html)
        self.assertNotIn("BX24.installFinish", html)
        self.assertEqual(self.installed(), [])
        self.assertEqual(self.requests, [])
        for secret in ("access-private", "refresh-private", "application-private", "webhook-secret"):
            self.assertNotIn(secret, html)

    def test_get_is_safe_with_forged_fields(self):
        status, html = self.service.page("GET", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 200)
        self.assertNotIn("BX24.installFinish", html)
        self.assertNotIn("access-private", html)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.installed(), [])

    def test_valid_install_stores_verified_identity_and_finishes(self):
        status, html = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 200)
        self.assertIn("BX24.installFinish", html)
        row = dict(self.installed()[0])
        self.assertEqual(row["client_id"], "local.expected")
        self.assertEqual(row["member_id"], "member-expected")
        self.assertEqual(row["user_id"], 7)
        self.assertEqual(row["scopes"], "im,imbot")
        self.assertEqual(row["access_token"], "access-private")
        self.assertEqual(row["refresh_token"], "refresh-private")
        self.assertEqual(row["application_token"], "application-private")
        self.assertNotIn("access-private", html)
        self.assertNotIn("<script>", html.split("<body>")[1].split("</body>")[0].split("<script src=")[0])

    def test_rejects_token_for_another_application(self):
        self.info["result"]["client_id"] = "local.attacker"
        status, html = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 403)
        self.assertNotIn("BX24.installFinish", html)
        self.assertEqual(self.installed(), [])

    def test_rejects_wrong_verified_portal_or_member(self):
        for key, value in (("domain", "attacker.test"), ("client_endpoint", "https://attacker.test/rest/"),
                           ("client_endpoint", "https://portal.example.test/rest/../evil/"),
                           ("member_id", "other-member")):
            with self.subTest(key=key, value=value):
                original = copy.deepcopy(self.info)
                self.info["result"]["install"][key] = value
                status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
                self.assertEqual(status, 403)
                self.assertEqual(self.installed(), [])
                self.info = original

    def test_incoming_endpoint_never_controls_outbound_requests(self):
        self.fields["SERVER_ENDPOINT"] = "http://127.0.0.1/private"
        self.fields["auth[client_endpoint]"] = "https://attacker.test/"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.requests), 2)

    def test_rejects_forged_domain_before_network(self):
        self.fields["DOMAIN"] = "attacker.test"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 403)
        self.assertEqual(self.requests, [])

    def test_requires_admin_and_same_known_user_for_installation(self):
        for patch in ({"ADMIN": False}, {"ADMIN": "true"}, {"ID": "8"}, {"ID": ""}):
            with self.subTest(patch=patch):
                self.profile = {"result": {"ID": "7", "ADMIN": True, **patch}}
                status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
                self.assertEqual(status, 403)
                self.assertEqual(self.installed(), [])

    def test_main_does_not_complete_first_install_or_replace_admin_credentials(self):
        status, html = self.service.page("POST", "/bitrix24/ttkgpt/", self.fields)
        self.assertEqual(status, 409)
        self.assertNotIn("BX24.installFinish", html)
        self.assertEqual(self.installed(), [])
        self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        before = dict(self.installed()[0])
        self.profile["result"]["ADMIN"] = False
        status, html = self.service.page("POST", "/bitrix24/ttkgpt/", self.fields)
        self.assertEqual(status, 200)
        self.assertIn("Тест &lt;script&gt;", html)
        self.assertNotIn("BX24.installFinish", html)
        self.assertEqual(dict(self.installed()[0]), before)

    def test_reinstallation_cannot_change_pinned_member(self):
        self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        before = dict(self.installed()[0])
        self.info["result"]["install"]["member_id"] = "different-member"
        self.fields["member_id"] = "different-member"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 403)
        self.assertEqual(dict(self.installed()[0]), before)

    def test_main_requires_stored_application_token(self):
        self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.fields["APPLICATION_TOKEN"] = "forged"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/", self.fields)
        self.assertEqual(status, 403)

    def test_non_ascii_application_token_is_rejected_without_server_error(self):
        self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.fields["APPLICATION_TOKEN"] = "подделка"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/", self.fields)
        self.assertEqual(status, 403)

    def test_rejects_missing_or_invalid_verified_user_id(self):
        for user_id in (None, "0", True, "unknown", "99999999999999999999"):
            with self.subTest(user_id=user_id):
                self.info["result"]["user_id"] = user_id
                self.profile["result"]["ID"] = user_id
                status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
                self.assertEqual(status, 403)
                self.assertEqual(self.installed(), [])

    def test_rejects_expired_verified_token(self):
        self.info["result"]["expires"] = "2020-01-01T00:00:00+00:00"
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 403)
        self.assertEqual(self.installed(), [])

    def test_callback_auth_fields_are_supported(self):
        fields = {"auth[access_token]": "access-private", "auth[refresh_token]": "refresh-private",
                  "auth[application_token]": "application-private", "auth[domain]": "portal.example.test",
                  "auth[member_id]": "member-expected"}
        status, _ = self.service.page("POST", "/bitrix24/ttkgpt/install", fields)
        self.assertEqual(status, 200)
        self.assertEqual(self.installed()[0]["member_id"], "member-expected")

    def test_network_error_is_generic_and_does_not_store_credentials(self):
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("access-private")):
            status, html = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 503)
        self.assertNotIn("access-private", html)
        self.assertEqual(self.installed(), [])

    def test_http_authorization_error_is_rejected_without_raw_error(self):
        error = HTTPError("https://oauth.bitrix24.tech/rest/app.info/", 401, "access-private", {}, None)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=error):
            status, html = self.service.page("POST", "/bitrix24/ttkgpt/install", self.fields)
        self.assertEqual(status, 403)
        self.assertNotIn("access-private", html)
        self.assertEqual(self.installed(), [])


if __name__ == "__main__":
    unittest.main()
