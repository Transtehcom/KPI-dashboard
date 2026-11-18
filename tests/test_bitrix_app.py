import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import io
import json
import re
import sqlite3
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs

from app import bitrix_app


class BitrixAppTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
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
        if request.full_url == "https://oauth.bitrix24.tech/oauth/token/":
            values = parse_qs(request.data.decode())
            if values.get("client_secret") != ["client-private"]:
                return io.BytesIO(b'{"error":"invalid_client"}')
            return io.BytesIO(json.dumps({"access_token": "new-access", "refresh_token": "new-refresh",
                "expires_in": 3600, "client_endpoint": "https://portal.example.test/rest/",
                "member_id": "member-expected", "user_id": 7}).encode())
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

    def test_oauth_call_uses_stored_token_fixed_portal_and_nested_json_parameters(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)

        def http(request, timeout):
            self.assertEqual(request.full_url, "https://portal.example.test/rest/imbot.v2.Bot.register.json")
            self.assertEqual(json.loads(request.data), {"fields": {"code": "test"}, "auth": "access-private"})
            return io.BytesIO(b'{"result":{"bot":{"id":42}}}')

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=http):
            self.assertEqual(self.service.call("imbot.v2.Bot.register", {"fields": {"code": "test"}}), {"bot": {"id": 42}})
        for method in ("../evil", "bot.get?auth=bad", "https://attacker.test", "x/#evil"):
            with self.assertRaises(bitrix_app.BitrixAppError):
                self.service.call(method)

    def test_expired_oauth_refresh_verifies_portal_and_preserves_application_token(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.config["bitrix_app"]["client_secret"] = "client-private"
        self.con.execute("UPDATE bitrix_app_installation SET expires_at='2020-01-01T00:00:00+00:00'")
        renewed = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
                   "client_endpoint": "https://portal.example.test/rest/", "member_id": "member-expected",
                   "user_id": 7, "scope": "im,imbot"}

        def http(request, timeout):
            if request.full_url == "https://oauth.bitrix24.tech/oauth/token/":
                self.assertEqual(parse_qs(request.data.decode()), {
                    "grant_type": ["refresh_token"], "client_id": ["local.expected"],
                    "client_secret": ["client-private"], "refresh_token": ["refresh-private"]})
                return io.BytesIO(json.dumps(renewed).encode())
            self.assertEqual(request.full_url, "https://portal.example.test/rest/profile.json")
            self.assertEqual(json.loads(request.data)["auth"], "new-access")
            return io.BytesIO(b'{"result":{"ID":"7"}}')

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=http):
            self.assertEqual(self.service.call("profile"), {"ID": "7"})
        row = self.installed()[0]
        self.assertEqual((row["access_token"], row["refresh_token"]), ("new-access", "new-refresh"))
        self.assertEqual(row["application_token"], "application-private")

    def test_untrusted_refresh_response_does_not_replace_credentials(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.config["bitrix_app"]["client_secret"] = "client-private"
        self.con.execute("UPDATE bitrix_app_installation SET expires_at='2020-01-01T00:00:00+00:00'")
        before = dict(self.installed()[0])
        response = {"access_token": "new", "refresh_token": "new", "expires_in": 3600,
                    "client_endpoint": "https://attacker.test/rest/", "member_id": "member-expected"}
        with mock.patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaises(bitrix_app.BitrixAppError) as error:
                self.service.call("profile")
        self.assertNotIn("client-private", str(error.exception))
        self.assertEqual(dict(self.installed()[0]), before)

    def test_missing_refresh_secret_fails_safely(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.con.execute("UPDATE bitrix_app_installation SET expires_at='2020-01-01T00:00:00+00:00'")
        with self.assertRaises(bitrix_app.BitrixAppError) as error:
            self.service.call("profile")
        self.assertNotIn("refresh-private", str(error.exception))

    def test_only_verified_admin_receives_activation_ticket_without_oauth_secrets(self):
        bridge = _Bridge()
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        status, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        self.assertEqual(status, 200)
        self.assertIn('action="/bitrix24/ttkgpt/activate"', body)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        row = self.con.execute("SELECT * FROM bitrix_app_activation_tickets").fetchone()
        self.assertNotIn(ticket, tuple(row))
        self.assertEqual((row["client_id"], row["member_id"], row["user_id"]), ("local.expected", "member-expected", 7))
        self.assertEqual(bridge.activations, 0)
        self.assertIn('type="password"', body)
        self.assertIn('name="client_secret"', body)
        for secret in ("access-private", "refresh-private", "application-private"):
            self.assertNotIn(secret, body)
        self.profile["result"]["ADMIN"] = False
        status, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        self.assertEqual(status, 200)
        self.assertNotIn('name="ticket"', body)

    def test_activation_requires_single_use_ticket_and_displays_bot_chat(self):
        bridge = _Bridge()
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        status, body = self.service.page("POST", bitrix_app.ACTIVATE_PATH,
            {"ticket": ticket, "client_secret": "client-private"})
        self.assertEqual(status, 200)
        self.assertEqual(bridge.activations, 1)
        self.assertIn("https://portal.example.test/online/?IM_DIALOG=42", body)
        self.assertNotIn("BX24.installFinish", body)
        secret = self.con.execute("SELECT client_secret FROM bitrix_app_credentials WHERE client_id='local.expected'").fetchone()
        self.assertEqual(secret[0], "client-private")
        self.assertEqual(self.installed()[0]["access_token"], "new-access")
        status, _ = self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})
        self.assertEqual(status, 403)
        self.assertEqual(bridge.activations, 1)

    def test_expired_ticket_and_changed_installation_cannot_activate_bot(self):
        bridge = _Bridge()
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        self.con.execute("UPDATE bitrix_app_activation_tickets SET expires_at=0")
        self.assertEqual(self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})[0], 403)
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        self.con.execute("UPDATE bitrix_app_installation SET member_id='changed-member'")
        self.assertEqual(self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})[0], 403)
        self.assertEqual(bridge.activations, 0)

    def test_nonadmin_can_open_registered_chat_but_cannot_issue_ticket(self):
        bridge = _Bridge()
        bridge.bot_id = 42
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.profile["result"]["ADMIN"] = False
        status, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        self.assertEqual(status, 200)
        self.assertIn("IM_DIALOG=42", body)
        self.assertNotIn('name="ticket"', body)
        self.assertEqual(self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": "forged"})[0], 403)
        self.assertEqual(bridge.activations, 0)

    def test_activation_exception_is_safe_and_ticket_stays_consumed(self):
        bridge = _Bridge()
        bridge.error = RuntimeError("access-private refresh-private client-private")
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        status, body = self.service.page("POST", bitrix_app.ACTIVATE_PATH,
            {"ticket": ticket, "client_secret": "client-private"})
        self.assertEqual(status, 503)
        self.assertNotIn("private", body)
        self.assertEqual(self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})[0], 403)

    def test_invalid_activation_secret_is_removed_and_preserves_oauth_tokens(self):
        bridge = _Bridge()
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        before = dict(self.installed()[0])
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        status, body = self.service.page("POST", bitrix_app.ACTIVATE_PATH,
            {"ticket": ticket, "client_secret": "bad-private"})
        self.assertIn(status, (403, 503))
        self.assertNotIn("bad-private", body)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM bitrix_app_credentials").fetchone()[0], 0)
        self.assertEqual(dict(self.installed()[0]), before)
        self.assertEqual(bridge.activations, 0)

    def test_config_secret_avoids_password_input_and_is_preferred(self):
        bridge = _Bridge()
        self.config["bitrix_app"]["client_secret"] = "client-private"
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        self.assertNotIn('name="client_secret"', body)
        self.assertNotIn("client-private", body)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        status, _ = self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})
        self.assertEqual(status, 200)
        self.assertEqual(bridge.activations, 1)

    def test_invalid_previously_stored_secret_is_removed_for_retry(self):
        bridge = _Bridge()
        self.service = bitrix_app.BitrixApp(self.con, self.config, bot_factory=lambda: bridge)
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.con.execute("INSERT INTO bitrix_app_credentials VALUES ('local.expected','bad-private')")
        _, body = self.service.page("POST", bitrix_app.MAIN_PATH, self.fields)
        ticket = re.search(r'name="ticket" value="([^"]+)"', body).group(1)
        status, _ = self.service.page("POST", bitrix_app.ACTIVATE_PATH, {"ticket": ticket})
        self.assertIn(status, (403, 503))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM bitrix_app_credentials").fetchone()[0], 0)
        self.assertEqual(bridge.activations, 0)

    def test_server_expired_token_retries_once_with_locally_stored_refresh_secret(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.con.execute("INSERT INTO bitrix_app_credentials VALUES ('local.expected','client-private')")
        old_attempts = []

        def http(request, timeout):
            if request.full_url == "https://oauth.bitrix24.tech/oauth/token/":
                return self.http(request, timeout)
            self.assertEqual(request.full_url, "https://portal.example.test/rest/profile.json")
            if json.loads(request.data)["auth"] == "access-private":
                old_attempts.append(1)
                return io.BytesIO(b'{"error":"expired_token"}')
            self.assertEqual(json.loads(request.data)["auth"], "new-access")
            return io.BytesIO(b'{"result":{"ID":"7"}}')

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=http):
            self.assertEqual(self.service.call("profile"), {"ID": "7"})
        self.assertEqual(len(old_attempts), 1)

    def test_parallel_calls_refresh_once_and_share_rotated_credentials(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        self.config["bitrix_app"]["client_secret"] = "client-private"
        self.con.execute("UPDATE bitrix_app_installation SET expires_at='2020-01-01T00:00:00+00:00'")
        started, release = threading.Event(), threading.Event()
        refresh_count = []

        def http(request, timeout):
            if request.full_url == "https://oauth.bitrix24.tech/oauth/token/":
                refresh_count.append(1)
                started.set()
                self.assertTrue(release.wait(2))
                return io.BytesIO(json.dumps({"access_token": "new-access", "refresh_token": "new-refresh",
                    "expires_in": 3600, "member_id": "member-expected",
                    "client_endpoint": "https://portal.example.test/rest/"}).encode())
            self.assertEqual(json.loads(request.data)["auth"], "new-access")
            return io.BytesIO(b'{"result":{"ID":"7"}}')

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=http):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.service.call, "profile") for _ in range(2)]
                self.assertTrue(started.wait(2))
                release.set()
                self.assertEqual([future.result(timeout=3) for future in futures], [{"ID": "7"}, {"ID": "7"}])
        self.assertEqual(len(refresh_count), 1)

    def test_rate_limit_rejections_are_retryable_without_ambiguity(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        for code in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
            with self.subTest(code=code):
                response = io.BytesIO(json.dumps({"error": code, "error_description": "access-private"}).encode())
                with mock.patch("urllib.request.OpenerDirector.open", return_value=response):
                    with self.assertRaises(bitrix_app.BitrixAppError) as caught:
                        self.service.call("profile")
                self.assertTrue(caught.exception.retryable)
                self.assertFalse(caught.exception.ambiguous)
                self.assertNotIn("access-private", str(caught.exception))
        error = HTTPError("https://portal.example.test/rest/profile.json", 429, "secret", {}, io.BytesIO(b'{}'))
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=error):
            with self.assertRaises(bitrix_app.BitrixAppError) as caught:
                self.service.call("profile")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.ambiguous)

    def test_rest_transport_is_ambiguous_and_pre_request_refresh_failure_is_retryable(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("access-private")):
            with self.assertRaises(bitrix_app.BitrixAppError) as caught:
                self.service.call("profile")
        self.assertTrue(caught.exception.ambiguous)
        self.assertFalse(caught.exception.retryable)
        self.config["bitrix_app"]["client_secret"] = "client-private"
        self.con.execute("UPDATE bitrix_app_installation SET expires_at='2020-01-01T00:00:00+00:00'")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("client-private")):
            with self.assertRaises(bitrix_app.BitrixAppError) as caught:
                self.service.call("profile")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.ambiguous)
        self.assertNotIn("private", str(caught.exception))

    def test_permanent_rest_rejection_is_neither_retryable_nor_ambiguous(self):
        self.service.page("POST", bitrix_app.INSTALL_PATH, self.fields)
        response = io.BytesIO(b'{"error":"ACCESS_DENIED","error_description":"private"}')
        with mock.patch("urllib.request.OpenerDirector.open", return_value=response):
            with self.assertRaises(bitrix_app.BitrixAppError) as caught:
                self.service.call("profile")
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(caught.exception.ambiguous)


class _Bridge:
    def __init__(self):
        self.bot_id = None
        self.activations = 0
        self.error = None

    def status(self):
        return {"bot_id": self.bot_id}

    def activate(self):
        self.activations += 1
        if self.error:
            raise self.error
        self.bot_id = 42
        return self.status()


if __name__ == "__main__":
    unittest.main()
