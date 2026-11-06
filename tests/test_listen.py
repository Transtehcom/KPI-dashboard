"""Права на прослушивание записей и журнал: запросы к настоящему HTTP-серверу на временной базе.
Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import json
import gzip
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

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
                                {"login": "pm", "name": "ПМ", "position": "pm", "role": "employee", "department": "ОП",
                                 "b24_user_id": 14},
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

    def test_processing_jobs_admin_only_create_schedule_and_cancel(self):
        def post(login,path,body):
            req=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),
                headers={"Cookie":self.tokens[login],"Content-Type":"application/json"},method="POST")
            try:
                with urllib.request.urlopen(req) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:
                with exc:return exc.code,json.load(exc)
        future=(__import__('datetime').datetime.now()+__import__('datetime').timedelta(days=1)).isoformat(timespec="minutes")
        self.assertEqual(self.get("pm","/api/calls-job")[0],403)
        self.assertEqual(post("pm","/api/calls-process",{"id":"call1"})[0],403)
        self.assertEqual(post("boss","/api/calls-process",{"id":"call1","run_at":"invalid"})[0],400)
        code,response=post("boss","/api/calls-process",{"id":"call1","run_at":future})
        self.assertEqual(code,200)
        job=response["job"]
        self.assertEqual((job["status"],job["total"],job["author"]),("queued",1,"boss"))
        self.assertNotIn("ids",job)
        self.assertTrue(any(j["id"]==job["id"] for j in json.loads(self.get("boss","/api/calls-job")[2])["jobs"]))
        self.assertEqual(post("pm","/api/calls-job-cancel",{"id":job["id"]})[0],403)
        code,response=post("boss","/api/calls-job-cancel",{"id":job["id"]})
        self.assertEqual((code,response["job"]["status"]),(200,"cancelled"))
        self.assertEqual(post("boss","/api/calls-job-cancel",{"id":"missing"})[0],400)

    def test_feedback_recheck_admin_revision_schedule_and_privacy(self):
        from app import call_feedback
        with db_lock():
            feedback=call_feedback.save(self.con,"call1","pm",{"rating":4,"comment":"Есть согласие на встречу","criteria":["agreement"]})
        def post(login,body):
            req=urllib.request.Request(self.base+"/api/call-feedback-reprocess",data=json.dumps(body).encode(),
                headers={"Cookie":self.tokens[login],"Content-Type":"application/json"},method="POST")
            try:
                with urllib.request.urlopen(req) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:
                with exc:return exc.code,json.load(exc)
        body={"id":"call1","author":"pm","revision":feedback["revision"]}
        self.assertEqual(post("pm",body)[0],403)
        self.assertEqual(post("boss",{**body,"revision":feedback["revision"]+1})[0],409)
        self.assertEqual(post("boss",{**body,"revision":True})[0],400)
        code,response=post("boss",body)
        self.assertEqual(code,200)
        self.assertTrue(response["job"]["feedback_recheck"])
        self.assertEqual(call_feedback.get(self.con,"call1","pm")["status"],"pending")
        own=json.loads(self.get("pm","/api/calls?month=2026-10")[2])["calls"]
        row=next(r for r in own if r["id"]=="call1")
        self.assertTrue(next(f for f in row["feedback"] if f["author"]=="pm")["rechecks"])
        self.app.processing_jobs().cancel(response["job"]["id"])
        status=self.con.execute("SELECT status FROM call_feedback_rechecks WHERE id=?",(response["job"]["id"],)).fetchone()[0]
        self.assertEqual(status,"cancelled")

    def test_gpt_admin_access_owned_chats_and_async_response(self):
        from app import gpt_assistant
        import time
        def post(login,path,body):
            req=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),
                headers={"Cookie":self.tokens[login],"Content-Type":"application/json"},method="POST")
            try:
                with urllib.request.urlopen(req) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as e:
                with e:return e.code,json.load(e)
        self.assertEqual(self.get("pm","/api/gpt-chats")[0],403)
        self.assertEqual(post("pm","/api/gpt-new",{})[0],403)
        code,chat=post("boss","/api/gpt-new",{})
        self.assertEqual(code,200)
        other=self.app.gpt_service().create_chat("other")
        self.assertEqual(self.get("boss","/api/gpt-chat?id="+other["id"])[0],400)
        self.assertEqual(self.get("pm","/api/gpt-preview?id="+chat["id"]+"&message=1")[0],403)
        self.assertEqual(post("boss","/api/gpt-send",{"id":chat["id"],"text":"x","model":"invalid"})[0],400)
        response={"result":{"kind":"answer","tool":"","query":"","employee":"","month":"","answer":"Здравствуйте","sources":[]}}
        with patch.object(gpt_assistant.call_analysis,"run_codex",return_value=response):
            code,job=post("boss","/api/gpt-send",{"id":chat["id"],"text":"Привет"})
            self.assertEqual(code,200)
            for _ in range(200):
                data=json.loads(self.get("boss","/api/gpt-chat?id="+chat["id"])[2])
                if data["messages"][-1]["status"]!="running":break
                time.sleep(.01)
        self.assertEqual(data["messages"][-1]["content"],"Здравствуйте")
        self.assertNotIn("outbound",data["messages"][-1])
        preview=json.loads(self.get("boss",f"/api/gpt-preview?id={chat['id']}&message={job['id']}")[2])
        self.assertIn("ДАННЫЕ",preview["outbound"])

    def test_gpt_document_upload_download_and_owner_access(self):
        import base64
        service=self.app.gpt_service()
        chat=service.create_chat("boss")
        other=service.create_chat("other")
        def upload(login,identity):
            req=urllib.request.Request(self.base+"/api/gpt-upload",data=json.dumps({"id":identity,"name":"notes.txt","content":base64.b64encode(b"hello").decode()}).encode(),headers={"Cookie":self.tokens[login],"Content-Type":"application/json"},method="POST")
            try:
                with urllib.request.urlopen(req) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as error:
                with error:return error.code,json.load(error)
        self.assertEqual(upload("pm",chat["id"])[0],403)
        self.assertEqual(upload("boss",other["id"])[0],400)
        code,document=upload("boss",chat["id"])
        self.assertEqual(code,200)
        path=f"/api/gpt-document?id={chat['id']}&document={document['id']}"
        self.assertEqual(self.get("pm",path)[0],403)
        code,_headers,raw=self.get("boss",path)
        self.assertEqual(code,200)
        self.assertEqual(base64.b64decode(json.loads(raw)["content"]),b"hello")
        self.assertEqual(self.get("boss",f"/api/gpt-document?id={other['id']}&document={document['id']}")[0],400)

    def test_monthly_supply_score_access_and_validation(self):
        employee={"login":"supply-test","name":"Снабжение","position":"supply","role":"employee"}
        self.app.config['employees'].append(employee)
        self.addCleanup(lambda:self.app.config['employees'].remove(employee))
        def post(login,body):
            req=urllib.request.Request(self.base+'/api/month-score',data=json.dumps(body).encode(),
                headers={'Cookie':self.tokens[login],'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(req) as r:return r.status
            except urllib.error.HTTPError as e:
                with e:return e.code
        body={'emp':'supply-test','month':'2026-11','criterion':'docs','score':90,'comment':'месяц'}
        self.assertEqual(post('boss',body),200)
        self.assertEqual(post('pm',{**body,'score':100}),403)
        self.assertEqual(post('boss',{**body,'criterion':'tasks'}),400)
        self.assertEqual(post('boss',{**body,'score':101}),400)
        self.assertEqual(post('boss',{**body,'emp':'pm'}),400)
        row=self.con.execute("SELECT score FROM monthly_scores WHERE emp='supply-test' AND month='2026-11'").fetchone()
        self.assertEqual(row['score'],90)

    def test_client_approval_is_admin_only_and_invalidates_the_employee_report(self):
        with db_lock():
            self.con.execute("INSERT INTO clients(id,emp,name,need,strategy) VALUES('co99001','lead','Тест согласования',1,1)")
            self.con.commit()
        def cleanup():
            with db_lock():
                self.con.execute("DELETE FROM clients WHERE id='co99001'")
                self.con.execute("DELETE FROM client_approvals WHERE ref='co99001'")
                self.con.commit()
        self.addCleanup(cleanup)
        body = {"emp":"lead","month":"2026-11","ref":"co99001","value":0}
        def post(login, value):
            req = urllib.request.Request(self.base+"/api/client-approval",data=json.dumps(value).encode(),
                                         headers={"Cookie":self.tokens[login],"Content-Type":"application/json"})
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code
        def count():
            report = json.loads(self.get("boss","/api/employee?login=lead&month=2026-11")[2])
            return next(i for i in report["items"] if i["code"]=="base_development")["components"][0]["fact"]
        initial = count()
        self.assertEqual(post("lead",body),403)
        self.assertEqual(post("boss",{**body,"value":True}),400)
        self.assertEqual(post("boss",{**body,"emp":"pm"}),400)
        self.assertEqual(post("boss",{**body,"ref":"missing"}),400)
        self.assertEqual(post("boss",body),200)
        self.assertEqual(count(),initial-1)
        self.assertEqual(post("boss",{**body,"value":None}),200)
        self.assertEqual(count(),initial)

    def test_lead_approval_is_admin_only_and_checks_owner_and_profile(self):
        with db_lock():
            self.con.execute("INSERT INTO leads(id,emp,title) VALUES('lead99111','pm','Тест согласования лида')")
            self.con.commit()
        def cleanup():
            with db_lock():
                self.con.execute("DELETE FROM leads WHERE id='lead99111'")
                self.con.execute("DELETE FROM lead_approvals WHERE ref='lead99111'")
                self.con.commit()
        self.addCleanup(cleanup)
        body = {"emp":"pm","month":"2026-11","ref":"lead99111","value":0}
        def post(login, value):
            req = urllib.request.Request(self.base+"/api/lead-approval",data=json.dumps(value).encode(),
                                         headers={"Cookie":self.tokens[login],"Content-Type":"application/json"})
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code
        self.assertEqual(post("pm",body),403)
        self.assertEqual(post("boss",{**body,"emp":"lead"}),400)
        self.assertEqual(post("boss",{**body,"ref":"missing"}),400)
        self.assertEqual(post("boss",{**body,"value":True}),400)
        self.assertEqual(post("boss",body),200)
        self.assertEqual(self.con.execute("SELECT approved FROM lead_approvals WHERE ref='lead99111'").fetchone()[0],0)
        self.assertEqual(post("boss",{**body,"value":None}),200)
        self.assertIsNone(self.con.execute("SELECT approved FROM lead_approvals WHERE ref='lead99111'").fetchone())

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

    def test_workload_is_temporarily_admin_only(self):
        for login in ("pm","tender","lead"):
            self.assertEqual(self.get(login,"/api/workload")[0],403)
        self.assertEqual(self.get("boss","/api/workload")[0],200)

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
        with db_lock():  # разбор Codex: роли по предложениям, имена — из таблицы замен
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

    def test_funnel_department_param(self):
        with db_lock():
            self.con.execute("INSERT OR REPLACE INTO crm_deals (id, entity, emp, responsible_id, responsible, category_id, "
                             "category, stage_id, stage, semantic) VALUES "
                             "('d1','deal','pm','14','ПМ','0','Продажи','S','В работе','P'),"
                             "('d2','deal',NULL,'99','Чужой','0','Продажи','S','В работе','P')")
            self.con.commit()
        total = lambda q: json.loads(self.get("boss", "/api/funnel?kind=deals" + q)[2])["total"]
        op = urllib.parse.quote("ОП")
        self.assertEqual((total(""), total("&department=" + op), total("&department=_none"), total(f"&department={op},_none")),
                         (2, 1, 1, 2))

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

    def test_gzip_responses_and_conditional_page_loading(self):
        with urllib.request.urlopen(self.base + "/") as response:
            plain = response.read()
            tag = response.headers.get("ETag")
        self.assertTrue(tag)
        req = urllib.request.Request(self.base + "/",headers={"Accept-Encoding":"gzip"})
        with urllib.request.urlopen(req) as response:
            compressed = response.read()
            self.assertEqual(response.headers.get("Content-Encoding"),"gzip")
            self.assertLess(len(compressed),len(plain) / 2)
            self.assertEqual(gzip.decompress(compressed),plain)
            self.assertIn("Accept-Encoding",response.headers.get("Vary",""))
        req = urllib.request.Request(self.base + "/",headers={"If-None-Match":tag})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        with error.exception:
            self.assertEqual(error.exception.code,304)
            self.assertEqual(error.exception.read(),b"")
        code, headers, body = self.get("pm","/api/employee?month=2026-11",{"Accept-Encoding":"gzip"})
        self.assertEqual(code,200)
        self.assertEqual(json.loads(gzip.decompress(body))["login"],"pm")
        self.assertEqual(headers.get("Cache-Control"),"no-store")
        code, headers, body = self.get("pm","/api/employee?month=2026-11",{"Accept-Encoding":"gzip;q=0, *;q=1"})
        self.assertEqual(code,200)
        self.assertNotIn("Content-Encoding",headers)
        self.assertEqual(json.loads(body)["login"],"pm")
        self.assertEqual(self.get("tender","/api/employee?login=pm&month=2026-11")[0],403)

    def test_employee_feedback_and_admin_review(self):
        with db_lock():
            self.con.execute("INSERT OR REPLACE INTO call_analysis(id,result) VALUES (?,?)",
                             ("call1", json.dumps({"summary": "Договорились о встрече"})))
            self.con.commit()
        def clean_feedback():
            with db_lock():
                self.con.execute("DELETE FROM call_analysis WHERE id='call1'")
                self.con.execute("DELETE FROM call_feedback WHERE record='call1'")
                self.con.commit()
        self.addCleanup(clean_feedback)

        def post(login, path, body):
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Cookie": self.tokens[login], "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.load(e)

        body = {"id": "call1", "rating": 3, "comment": "Договорились о встрече, это не секретарь",
                "criteria": ["secretary", "agreement", "tone"]}
        self.assertEqual(post("tender", "/api/call-feedback", body)[0], 403)
        self.assertEqual(post("boss", "/api/call-feedback", body)[0], 403)
        self.assertEqual(post("pm", "/api/call-feedback", {**body, "comment": "   "})[0], 400)
        self.assertEqual(post("pm", "/api/call-feedback", {**body, "criteria": ["invented"]})[0], 400)
        for rating in (True, None, "3", 0, 6, 3.5):
            self.assertEqual(post("pm", "/api/call-feedback", {**body, "rating": rating})[0], 400)
        code, saved = post("pm", "/api/call-feedback", body)
        self.assertEqual(code, 200)
        revision = saved["feedback"]["revision"]
        self.assertEqual(saved["feedback"]["status"], "pending")
        pending = json.loads(self.get("boss", "/api/calls?all=1&feedback=pending")[2])
        self.assertEqual([c["id"] for c in pending["calls"]], ["call1"])
        manager = json.loads(self.get("boss", "/api/calls?all=1")[2])
        call = next(c for c in manager["calls"] if c["id"] == "call1")
        self.assertEqual(call["feedback"][0]["criteria"], body["criteria"])
        self.assertEqual(call["feedback"][0]["comment"], body["comment"])
        self.assertIn("secretary", {c["key"] for c in manager["feedback_criteria"]})
        lead = self.app.user("lead")
        old_department = lead["department"]
        lead["department"] = "ОП"
        try:
            coworker = json.loads(self.get("lead", "/api/calls?all=1")[2])
            self.assertEqual(post("lead", "/api/call-feedback", body)[0], 403)
        finally:
            lead["department"] = old_department
        self.assertEqual(next(c for c in coworker["calls"] if c["id"] == "call1")["feedback"], [])
        review = {"id": "call1", "author": "pm", "revision": revision, "note": "Проверено вручную"}
        self.assertEqual(post("pm", "/api/call-feedback-review", review)[0], 403)
        code, reviewed = post("boss", "/api/call-feedback-review", review)
        self.assertEqual(code, 200)
        self.assertEqual(reviewed["feedback"]["status"], "resolved")
        self.assertEqual(reviewed["feedback"]["resolution"], review["note"])
        pending = json.loads(self.get("boss", "/api/calls?all=1&feedback=pending")[2])
        self.assertEqual(pending["calls"], [])
        unchanged = post("pm", "/api/call-feedback", body)[1]["feedback"]
        self.assertEqual((unchanged["revision"], unchanged["status"]), (revision, "resolved"))
        code, saved = post("pm", "/api/call-feedback", {**body, "comment": "Есть ещё ошибка в интонации"})
        self.assertEqual(code, 200)
        self.assertEqual(saved["feedback"]["status"], "pending")
        self.assertGreater(saved["feedback"]["revision"], revision)
        self.assertEqual(post("boss", "/api/call-feedback-review", review)[0], 409)
        self.assertEqual(post("pm", "/api/call-feedback", {"id": "call1", "rating": 5})[0], 200)
        own = json.loads(self.get("pm", "/api/calls?all=1")[2])
        feedback = next(c for c in own["calls"] if c["id"] == "call1")["feedback"][0]
        self.assertEqual((feedback["rating"], feedback["comment"], feedback["criteria"], feedback["status"]),
                         (5, "", [], "accepted"))
        self.assertIsNone(self.con.execute("SELECT secretary FROM call_marks WHERE id='call1'").fetchone())

    def test_feedback_respects_listening_permission_and_requires_analysis(self):
        req = urllib.request.Request(self.base + "/api/call-feedback",
                                     data=json.dumps({"id": "call1", "rating": 5}).encode(),
                                     headers={"Cookie": self.tokens["pm"], "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        error.exception.close()
        self.assertEqual(error.exception.code, 400)
        self.app.config["call_records"]["listen_own"] = False
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            error.exception.close()
            self.assertEqual(error.exception.code, 403)
        finally:
            del self.app.config["call_records"]["listen_own"]

    def test_slow_response_does_not_block_another_employee_request(self):
        sending, release, completed = threading.Event(), threading.Event(), threading.Event()
        original = Handler.send_json
        results = []

        def slow_send(handler, *args, **kwargs):
            if "slow_client=1" in handler.path:
                sending.set()
                release.wait(5)
            return original(handler, *args, **kwargs)

        def quick_request():
            results.append(self.get("pm", "/api/employee?month=2026-11"))
            completed.set()

        with patch.object(Handler, "send_json", slow_send):
            slow = threading.Thread(target=lambda: self.get("pm", "/api/calls?slow_client=1"))
            quick = threading.Thread(target=quick_request)
            slow.start()
            try:
                self.assertTrue(sending.wait(2), "slow response did not start")
                quick.start()
                self.assertTrue(completed.wait(2), "a slow client held the shared database lock")
                self.assertEqual(results[0][0], 200)
            finally:
                release.set()
                slow.join(5)
                if quick.ident is not None:
                    quick.join(5)


if __name__ == "__main__":
    unittest.main()
