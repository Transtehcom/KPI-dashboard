import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from app.common import connect, db_lock
from app.server import App, Handler


class BitrixAppHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = App.__new__(App)
        app.config = {'bitrix24': {'webhook': 'https://portal.example/rest/1/test-key/'}, 'employees': []}
        app.con = connect(':memory:')
        app.lock = db_lock()
        cls.app = app

        class TestHandler(Handler):
            pass

        TestHandler.app = app
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.app.con.close()

    def request(self, path, data=None, headers=None, method=None):
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read().decode('utf-8')

    def test_public_pages_allow_only_configured_portal_frame(self):
        for path in ['/bitrix24/ttkgpt/', '/bitrix24/ttkgpt/install']:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn('ttkGPT', body)
                self.assertIn("frame-ancestors 'self' https://portal.example", headers['Content-Security-Policy'])
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                self.assertNotIn('Set-Cookie', headers)
                self.assertNotIn('test-key', body)

    def test_install_accepts_bitrix_form_without_dashboard_cookie(self):
        body = urllib.parse.urlencode({'AUTH_ID': 'test-oauth-secret', 'DOMAIN': 'portal.example'}).encode()
        status, headers, result = self.request('/bitrix24/ttkgpt/install', body,
                                               {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 200)
        self.assertIn('client_id', result)
        self.assertNotIn('test-oauth-secret', result)
        self.assertNotIn('BX24.installFinish()', result)
        self.assertNotIn('Set-Cookie', headers)

    def test_duplicate_fields_are_rejected(self):
        status, _, _ = self.request('/bitrix24/ttkgpt/install', b'AUTH_ID=first&AUTH_ID=second',
                                    {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 400)

    def test_query_and_body_conflicting_portals_are_rejected(self):
        status, _, _ = self.request('/bitrix24/ttkgpt/install?DOMAIN=other.example', b'DOMAIN=portal.example',
                                    {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 400)

    def test_oversized_body_is_rejected_before_reading_it(self):
        status, _, _ = self.request('/bitrix24/ttkgpt/install', b'x',
                                    {'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '70000'})
        self.assertEqual(status, 413)

    def test_json_is_rejected_for_bitrix_form_route(self):
        status, _, _ = self.request('/bitrix24/ttkgpt/install', json.dumps({'AUTH_ID': 'token'}).encode(),
                                    {'Content-Type': 'application/json'})
        self.assertEqual(status, 415)

    def test_normal_dashboard_api_still_requires_its_session(self):
        status, _, _ = self.request('/api/gpt-chats')
        self.assertEqual(status, 401)

    def test_activation_route_rejects_forged_ticket_without_dashboard_cookie(self):
        self.app.config['bitrix_app'] = {'client_id': 'local.test'}
        try:
            status, _, result = self.request('/bitrix24/ttkgpt/activate', b'ticket=forged',
                                             {'Content-Type': 'application/x-www-form-urlencoded'})
            self.assertEqual(status, 403)
            self.assertNotIn('Set-Cookie', result)
        finally:
            self.app.config.pop('bitrix_app', None)
