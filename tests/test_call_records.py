"""Тесты записей звонков. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import call_records, transcripts  # noqa: E402
from app.bitrix24 import B24Error, Bitrix24  # noqa: E402
from app.common import connect  # noqa: E402


def rec(i, day=None, **kw):
    day = day or dt.date.today().isoformat()
    row = {"id": f"call{i}", "call_id": f"externalCall.{i}", "emp": "pm", "dt": f"{day}T10:00:00", "direction": "out",
           "duration": 120, "phone": "79990000000", "crm_entity": "CONTACT:5", "client": "Ромашка",
           "crm_activity_id": str(100 + i), "record_file_id": str(500 + i), "record_url": None}
    row.update(kw)
    return row


class FakeB24:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def batch(self, cmds):
        self.calls.append(cmds)
        res, err = {}, {}
        for k, (m, p) in cmds.items():
            a = self.answers(m, p)
            (err if isinstance(a, Exception) else res)[k] = a
        return res, err


class CallRecordsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.config = {"call_records": {"dir": str(self.dir / "records")}}
        self.audio = self.dir / "src.mp3"
        self.audio.write_bytes(b"\xff\xe3(\xc4" + b"0" * 100)

    def row(self, id_):
        return dict(self.con.execute("SELECT * FROM call_records WHERE id=?", (id_,)).fetchone())

    def test_upsert_keeps_downloaded_file_and_bindings(self):
        call_records.upsert(self.con, [rec(1)])
        self.con.execute("UPDATE call_records SET file='2026-10/call1.mp3', bindings='DEAL:7' WHERE id='call1'")
        call_records.upsert(self.con, [rec(1, client="Ромашка ООО")])
        r = self.row("call1")
        self.assertEqual((r["file"], r["bindings"], r["client"]), ("2026-10/call1.mp3", "DEAL:7", "Ромашка ООО"))

    def test_bindings_and_lookup_by_deal(self):
        call_records.upsert(self.con, [rec(1), rec(2), rec(3)])
        answers = {"101": [{"entityTypeId": 3, "entityId": 5}, {"entityTypeId": 2, "entityId": 12}],
                   "102": [{"entityTypeId": 2, "entityId": 120}], "103": Exception("not found")}
        call_records.resolve_bindings(self.con, FakeB24(lambda m, p: answers[p["activityId"]]))
        self.assertEqual(self.row("call1")["bindings"], "CONTACT:5,DEAL:12")
        self.assertIsNotNone(self.row("call3")["bindings_checked"])
        self.assertEqual([r["id"] for r in call_records.listing(self.con, {}, deal=12)], ["call1"])  # не DEAL:120

    def test_old_calls_bindings_checked_once(self):
        old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        call_records.upsert(self.con, [rec(1, day=old)])
        b24 = FakeB24(lambda m, p: [])
        call_records.resolve_bindings(self.con, b24)
        call_records.resolve_bindings(self.con, b24)
        self.assertEqual(len(b24.calls), 1)

    def test_download_from_disk_then_from_pbx_link(self):
        call_records.upsert(self.con, [rec(1), rec(2, record_file_id=None, record_url=self.audio.as_uri()),
                                       rec(3, record_file_id=None)])
        b24 = FakeB24(lambda m, p: {"NAME": "ttk711_out_7999.mp3", "DOWNLOAD_URL": self.audio.as_uri()})
        ok, failed = call_records.download_pending(self.con, b24, self.config)
        self.assertEqual((ok, failed), (2, 1))
        r = self.row("call1")
        self.assertEqual(r["file"], r["dt"][:7] + "/call1.mp3")
        self.assertEqual(pathlib.Path(call_records.file_path(self.config, r)).read_bytes(), self.audio.read_bytes())
        self.assertIsNotNone(self.row("call2")["file"])
        self.assertEqual(self.row("call3")["attempts"], 1)
        self.assertEqual(call_records.download_pending(self.con, b24, self.config), (0, 1))  # скачанные не повторяются

    def test_html_instead_of_audio_is_error(self):
        page = self.dir / "error.html"
        page.write_text("<html>нет доступа</html>", encoding="utf-8")
        call_records.upsert(self.con, [rec(1, record_file_id=None, record_url=page.as_uri())])
        self.assertEqual(call_records.download_pending(self.con, FakeB24(lambda m, p: {}), self.config), (0, 1))
        self.assertFalse((self.dir / "records").exists() and any((self.dir / "records").rglob("*.html")))

    def test_purge_by_keep_days(self):
        old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        cfg = {"call_records": {"dir": str(self.dir / "records"), "keep_days": 365}}
        call_records.upsert(self.con, [rec(1, day=old), rec(2)])
        b24 = FakeB24(lambda m, p: {"DOWNLOAD_URL": self.audio.as_uri()})
        self.assertEqual(call_records.download_pending(self.con, b24, cfg), (1, 0))  # старую уже не качаем
        self.con.execute("UPDATE call_records SET file='old/call1.mp3' WHERE id='call1'")
        (self.dir / "records" / "old").mkdir()
        (self.dir / "records" / "old" / "call1.mp3").write_bytes(b"x")
        self.assertEqual(call_records.purge_old(self.con, cfg), 1)
        self.assertFalse((self.dir / "records" / "old" / "call1.mp3").exists())
        self.assertIsNone(self.row("call1")["file"])
        self.assertIsNotNone(self.row("call2")["file"])


FAKE_WORKER = r"""
import json, sys
sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
for line in sys.stdin:
    path = line.strip()
    if path.endswith("bad.mp3"):
        print(json.dumps({"path": path, "error": "RuntimeError: битый файл"}), flush=True)
    else:
        print(json.dumps({"path": path, "duration": 61.0, "segments": [
            [0.5, 3.0, "вас приветствует компания транстехком"], [3.5, 6.0, "нажмите один для соединения с отделом продаж"],
            [9.0, 15.2, "добрый день по поводу коммерческого предложения"], [16.0, 20.0, "цена устраивает"]]},
            ensure_ascii=False), flush=True)
"""


class TranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        (self.dir / "2026-10").mkdir()
        for name in ("call1.mp3", "bad.mp3"):
            (self.dir / "2026-10" / name).write_bytes(b"x")
        self.worker = self.dir / "worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        call_records.upsert(self.con, [rec(1), rec(2), rec(3)])
        self.con.execute("UPDATE call_records SET file='2026-10/call1.mp3' WHERE id='call1'")
        self.con.execute("UPDATE call_records SET file='2026-10/bad.mp3' WHERE id='call2'")  # call3 не скачан
        self.config = {"call_records": {"dir": str(self.dir), "transcribe_log": str(self.dir / "err.log")}}

    def run_worker(self):
        return transcripts.run(self.con, self.config, str(self.dir), log=lambda *a: None, worker=str(self.worker))

    def test_transcribe_queue_drops_ivr_and_retries_errors(self):
        self.assertEqual(self.run_worker(), (1, 1))
        t = transcripts.get(self.con, "call1")
        self.assertEqual([s[2] for s in t["segments"]], ["добрый день по поводу коммерческого предложения", "цена устраивает"])
        self.assertEqual(t["text"], "добрый день по поводу коммерческого предложения цена устраивает")
        self.assertEqual(t["engine"], transcripts.ENGINE)
        self.assertEqual([r["id"] for r in transcripts.pending(self.con, 10)], ["call2"])  # готовый не повторяется
        self.run_worker()
        self.run_worker()
        self.assertEqual(transcripts.pending(self.con, 10), [])  # после MAX_ATTEMPTS ошибок — не повторять
        st = {c["id"]: c["transcript"] for c in call_records.listing(self.con, self.config)}
        self.assertEqual(st, {"call1": "ready", "call2": "error", "call3": None})

    def test_transcript_deleted_with_audio(self):
        self.run_worker()
        old = (dt.date.today() - dt.timedelta(days=100)).isoformat() + "T10:00:00"
        self.con.execute("UPDATE call_records SET dt=? WHERE id='call1'", (old,))
        call_records.purge_old(self.con, {"call_records": {"dir": str(self.dir), "keep_days": 90}})
        self.assertIsNone(transcripts.get(self.con, "call1"))

    def test_many_files_with_long_results_do_not_deadlock(self):
        # 200 файлов и по 50 КБ текста на каждый: каналы stdin/stdout переполняются, если список не подаётся параллельно
        big = self.dir / "big.py"
        big.write_text("import json, sys\nsys.stdin.reconfigure(encoding='utf-8')\n"
                       "for line in sys.stdin:\n"
                       "    print(json.dumps({'path': line.strip(), 'duration': 1.0, 'segments': [[0, 1, 'слово ' * 8000]]}),"
                       " flush=True)\n", encoding="utf-8")
        call_records.upsert(self.con, [rec(i) for i in range(10, 210)])
        for i in range(10, 210):
            self.con.execute("UPDATE call_records SET file=? WHERE id=?", (f"2026-10/long-path-name-for-call-{i:04d}.mp3", f"call{i}"))
        out = []
        th = threading.Thread(target=lambda: out.append(transcripts.run(
            self.con, self.config, str(self.dir), log=lambda *a: None, worker=str(big))), daemon=True)
        th.start()
        th.join(60)
        self.assertFalse(th.is_alive(), "расшифровка зависла")
        self.assertEqual(out[0][0], 200)

    def test_disabled(self):
        self.config["call_records"]["transcribe"] = False
        self.assertEqual(self.run_worker(), (0, 0))


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.b = Bitrix24({"webhook": "https://example.test/rest/1/x/"}, log=lambda *a: None)
        patcher = mock.patch("app.bitrix24.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_call_retries_network_errors(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"result": 1}'
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError("handshake"), resp]):
            self.assertEqual(self.b.call("voximplant.statistic.get")["result"], 1)
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("handshake")):
            self.assertRaises(B24Error, self.b.call, "voximplant.statistic.get")

    def test_batch_splits_by_50(self):
        sent = []

        def call(method, params=None):
            sent.append(params["cmd"])
            return {"result": {"result": {k: k for k in params["cmd"]}, "result_error": []}}
        with mock.patch.object(self.b, "call", call):
            res, err = self.b.batch({f"k{i}": ("disk.file.get", {"id": i}) for i in range(120)})
        self.assertEqual([len(c) for c in sent], [50, 50, 20])
        self.assertEqual(sent[0]["k7"], "disk.file.get?id=7")
        self.assertEqual((len(res), err), (120, {}))


if __name__ == "__main__":
    unittest.main()
