"""Права на прослушивание записей и журнал: запросы к настоящему HTTP-серверу на временной базе.
Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import call_records  # noqa: E402
from app.common import connect, db_lock, load_rules  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.server import App, Handler, set_password  # noqa: E402

AUDIO = bytes(range(256)) * 40  # 10 240 байт


class ListenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        rec_dir = pathlib.Path(cls.tmp.name)
        (rec_dir / "2026-10").mkdir()
        (rec_dir / "2026-10" / "call1.mp3").write_bytes(AUDIO)
        config = {"holidays": [], "call_records": {"dir": str(rec_dir), "keep_days": 90},
                  "bitrix24": {"webhook": "https://portal.example.test/rest/1/secret/"},
                  "employees": [{"login": "boss", "name": "Руководитель", "role": "manager"},
                                {"login": "pm", "name": "ПМ", "position": "pm", "role": "employee", "department": "ОП"},
                                {"login": "tender", "name": "Тендерный", "position": "tender", "role": "employee",
                                 "department": "ОС"},
                                {"login": "lead", "name": "Руководитель ОС", "position": "tender_lead", "role": "employee",
                                 "department": "ОС", "listen_dept": True}]}
        con = connect(":memory:")
        app = App.__new__(App)  # без рабочей базы и config.json
        app.config, app.rules, app.con = config, load_rules(), con
        app.engine, app.lock, app.login_fails = Engine(con, app.rules, config), db_lock(), {}
        for e in config["employees"]:
            set_password(con, e["login"], "pw-" + e["login"])
        call_records.upsert(con, [{"id": "call1", "emp": "pm", "dt": "2026-10-20T10:00:00", "direction": "out",
                                   "duration": 95, "client": "Ромашка", "phone": "79990000000"},
                                  {"id": "call2", "emp": "pm", "dt": "2026-10-19T10:00:00", "direction": "in", "duration": 30},
                                  {"id": "call3", "emp": "tender", "dt": "2026-10-18T10:00:00", "direction": "out", "duration": 60}])
        con.execute("UPDATE call_records SET file='2026-10/call1.mp3', bindings='CONTACT:5,DEAL:12' WHERE id='call1'")
        con.execute("UPDATE call_records SET file='2026-10/call1.mp3' WHERE id='call3'")
        con.commit()
        cls.con, cls.app = con, app
        Handler.app = app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.tokens = {}
        for login in ("boss", "pm", "tender", "lead"):
            req = urllib.request.Request(cls.base + "/api/login", method="POST",
                                         data=json.dumps({"login": login, "password": "pw-" + login}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                cls.tokens[login] = r.headers["Set-Cookie"].split(";")[0]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.con.close()
        cls.tmp.cleanup()

    def setUp(self):
        with db_lock():
            self.con.execute("DELETE FROM listen_log")
            self.con.commit()

    def get(self, login, path, headers=None):
        req = urllib.request.Request(self.base + path, headers={"Cookie": self.tokens[login], **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def log(self):
        return [(r["login"], r["record"], r["emp"], r["result"]) for r in
                self.con.execute("SELECT * FROM listen_log ORDER BY id")]

    def test_owner_listens_and_is_logged_once(self):
        code, h, body = self.get("pm", "/api/call-audio?id=call1")
        self.assertEqual((code, body, h["Content-Type"]), (200, AUDIO, "audio/mpeg"))
        self.get("pm", "/api/call-audio?id=call1", {"Range": "bytes=0-"})  # повтор сразу после — то же прослушивание
        self.assertEqual(self.log(), [("pm", "call1", "pm", "ok")])

    def test_seek_by_range_is_not_a_new_listen(self):
        code, h, body = self.get("boss", "/api/call-audio?id=call1", {"Range": "bytes=100-199"})
        self.assertEqual((code, body, h["Content-Range"]), (206, AUDIO[100:200], f"bytes 100-199/{len(AUDIO)}"))
        self.assertEqual(self.get("boss", "/api/call-audio?id=call1", {"Range": "bytes=-10"})[2], AUDIO[-10:])
        self.assertEqual(self.get("boss", "/api/call-audio?id=call1", {"Range": f"bytes={len(AUDIO)}-"})[0], 416)
        self.assertEqual(self.log(), [])

    def test_other_employee_is_denied_and_logged(self):
        code, _, body = self.get("tender", "/api/call-audio?id=call1")
        self.assertEqual(code, 403)
        self.assertNotIn(AUDIO[:50], body)
        self.assertEqual(self.log(), [("tender", "call1", "pm", "denied")])

    def test_not_downloaded_record(self):
        self.assertEqual(self.get("pm", "/api/call-audio?id=call2")[0], 404)
        self.assertEqual(self.get("pm", "/api/call-audio?id=nope")[0], 404)

    def test_calls_list_rights(self):
        self.assertEqual(self.get("tender", "/api/calls?login=pm&month=2026-10")[0], 400)  # чужие звонки не выбрать
        d = json.loads(self.get("pm", "/api/calls?month=2026-10")[2])
        self.assertEqual([e["login"] for e in d["employees"]], ["pm"])
        self.assertEqual([(c["id"], c["audio"]) for c in d["calls"]], [("call1", "ready"), ("call2", "pending")])
        self.assertNotIn("listens", d["calls"][0])
        self.assertEqual(d["portal"], "https://portal.example.test")  # без секрета вебхука
        self.get("pm", "/api/call-audio?id=call1")
        d = json.loads(self.get("boss", "/api/calls?login=&deal=12")[2])
        self.assertEqual([(c["id"], c["deals"], c["listens"]) for c in d["calls"]], [("call1", [12], 1)])
        self.assertEqual(self.get("pm", "/api/calls?deal=abc")[0], 400)

    def test_listen_journal_only_for_admin(self):
        self.get("pm", "/api/call-audio?id=call1")
        self.assertEqual(self.get("pm", "/api/listen-log")[0], 403)
        code, _, body = self.get("boss", "/api/listen-log?date_from=2020-01-01")
        rows = json.loads(body)["rows"]
        self.assertEqual((code, len(rows)), (200, 1))
        self.assertEqual((rows[0]["name"], rows[0]["emp_name"], rows[0]["client"], rows[0]["ip"]),
                         ("ПМ", "ПМ", "Ромашка", "127.0.0.1"))

    def test_calls_period(self):
        ids = lambda q: [c["id"] for c in json.loads(self.get("pm", "/api/calls?" + q)[2])["calls"]]
        self.assertEqual(ids("date_from=2026-10-20&date_to=2026-10-20"), ["call1"])
        self.assertEqual(ids("date_from=2026-10-19"), ["call1", "call2"])
        self.assertEqual(ids("all=1"), ["call1", "call2"])
        self.assertEqual(ids("month=2026-09"), [])
        self.assertEqual(self.get("pm", "/api/calls?date_from=2026-10-20&date_to=2026-10-01")[0], 400)

    def test_department_head_listens_to_own_department_only(self):
        d = json.loads(self.get("lead", "/api/calls?month=2026-10")[2])
        self.assertEqual([(c["id"], c["can_listen"]) for c in d["calls"]], [("call3", True)])
        self.assertEqual(sorted(e["login"] for e in d["employees"]), ["lead", "tender"])
        self.assertNotIn("listens", d["calls"][0])
        self.assertEqual(self.get("lead", "/api/calls?login=pm&month=2026-10")[0], 400)
        self.assertEqual(self.get("lead", "/api/calls?deal=12")[2].count(b"call1"), 0)  # сделка чужого отдела — пусто
        self.assertEqual(self.get("lead", "/api/call-audio?id=call3")[0], 200)
        self.assertEqual(self.get("lead", "/api/call-audio?id=call1")[0], 403)
        self.assertEqual(self.get("lead", "/api/listen-log")[0], 403)
        self.assertEqual(self.log(), [("lead", "call3", "tender", "ok"), ("lead", "call1", "pm", "denied")])
        self.assertEqual(self.get("tender", "/api/call-audio?id=call3")[0], 200)  # без права — только свои
        self.assertEqual(self.get("tender", "/api/call-audio?id=call1")[0], 403)

    def test_call_funnel_rights(self):
        d = json.loads(self.get("pm", "/api/call-funnel?login=pm,tender,id:77&date_from=2026-10-01&date_to=2026-10-31")[2])
        self.assertEqual([(p["login"], "error" in p) for p in d["people"]], [("pm", False), ("tender", True)])
        d = json.loads(self.get("boss", "/api/call-funnel?login=pm,tender")[2])
        self.assertEqual([("error" in p) for p in d["people"]], [False, False])
        self.assertEqual(d["date_from"][8:], "01")  # без периода — текущий месяц
        self.assertEqual(self.get("boss", "/api/call-funnel?login=pm&date_from=2026-10-20&date_to=2026-10-01")[0], 400)

    def test_transcript_rights_and_journal(self):
        with db_lock():
            self.con.execute("INSERT OR REPLACE INTO call_transcripts (id, segments, text, engine) VALUES "
                             "('call1', '[[1.0, 2.5, \"цена устраивает\"]]', 'цена устраивает', 'gigaam-v2')")
            self.con.commit()
        d = json.loads(self.get("pm", "/api/call-transcript?id=call1")[2])
        self.assertEqual(d["segments"], [[1.0, 2.5, "цена устраивает"]])
        self.get("pm", "/api/call-transcript?id=call1")  # повтор сразу — одна строка журнала
        self.assertEqual(self.get("tender", "/api/call-transcript?id=call1")[0], 403)
        self.assertEqual(self.get("pm", "/api/call-transcript?id=call2")[0], 404)
        self.assertEqual(self.log(), [("pm", "call1", "pm", "read"), ("tender", "call1", "pm", "denied")])
        calls = json.loads(self.get("pm", "/api/calls?month=2026-10")[2])["calls"]
        self.assertEqual({c["id"]: c["transcript"] for c in calls}, {"call1": "ready", "call2": None})
        with db_lock():  # разбор Claude: роли по предложениям, имена — из таблицы замен
            self.con.execute("INSERT OR REPLACE INTO call_analysis (id, version, call_type, offer, agreement, result, roles, pii) "
                             "VALUES ('call1', 2, 'продажа', 1, 0, ?, 'К', ?)",
                             ('{"summary": "[ИМЯ_1] согласен с ценой", "call_type": "продажа", "offer": {"made": true}}',
                              '{"[ИМЯ_1]": "Иван Петров"}'))
            self.con.commit()
        d = json.loads(self.get("pm", "/api/call-transcript?id=call1")[2])
        self.assertEqual(d["sentences"], [[1.0, "К", "цена устраивает"]])
        self.assertEqual(d["analysis"]["summary"], "Иван Петров согласен с ценой")
        self.assertNotIn("roles", d["analysis"])
        c1 = next(c for c in json.loads(self.get("pm", "/api/calls?month=2026-10")[2])["calls"] if c["id"] == "call1")
        self.assertEqual(c1["analysis"], {"call_type": "продажа", "offer": True, "agreement": False,
                                          "summary": "Иван Петров согласен с ценой"})

    def test_listen_own_can_be_disabled(self):
        self.app.config["call_records"]["listen_own"] = False
        try:
            self.assertEqual(self.get("pm", "/api/call-audio?id=call1")[0], 403)
            self.assertEqual(self.get("boss", "/api/call-audio?id=call1")[0], 200)
        finally:
            del self.app.config["call_records"]["listen_own"]

    def test_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/api/call-audio?id=call1")
        e.exception.close()
        self.assertEqual(e.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
