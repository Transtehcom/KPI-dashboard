"""Тесты записей звонков. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import call_records  # noqa: E402
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
