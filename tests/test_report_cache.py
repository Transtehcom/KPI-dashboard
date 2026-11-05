import pathlib
import tempfile
import unittest
from unittest.mock import patch

from app.common import connect
from app.server import App


class ReportCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(pathlib.Path(self.tmp.name) / "reports.sqlite")
        self.app = App.__new__(App)
        self.app.con = connect(self.path)
        self.addCleanup(self.app.con.close)
        self.calls = 0

    def calculate(self):
        self.calls += 1
        return {"items": [{"value": self.calls}]}

    def test_reuses_calculation_without_sharing_mutable_results(self):
        with patch("app.server.time.monotonic",return_value=5):
            result = self.app.cached_report(("employee","pm","2026-11"),self.calculate)
            result["items"][0]["value"] = 999
            other = self.app.cached_report(("employee","pm","2026-11"),self.calculate)
        self.assertEqual(other["items"][0]["value"],1)
        self.assertEqual(self.calls,1)

    def test_local_writes_and_external_commits_invalidate_reports(self):
        with patch("app.server.time.monotonic",return_value=5):
            self.app.cached_report(("overview","2026-11"),self.calculate)
            self.app.con.execute("INSERT INTO dataset_state VALUES('test','2026-11-01','2026-11-05','one')")
            self.app.con.commit()
            self.app.cached_report(("overview","2026-11"),self.calculate)
            other = connect(self.path)
            try:
                other.execute("UPDATE dataset_state SET updated_at='two'")
                other.commit()
            finally:
                other.close()
            self.app.cached_report(("overview","2026-11"),self.calculate)
        self.assertEqual(self.calls,3)

    def test_time_dependent_reports_refresh_and_employee_keys_are_separate(self):
        with patch("app.server.time.monotonic",return_value=5):
            self.app.cached_report(("employee","pm","2026-11"),self.calculate)
            self.app.cached_report(("employee","lead","2026-11"),self.calculate)
        with patch("app.server.time.monotonic",return_value=40):
            self.app.cached_report(("employee","pm","2026-11"),self.calculate)
        self.assertEqual(self.calls,3)
