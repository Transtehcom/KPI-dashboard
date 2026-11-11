import datetime as dt
import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import call_records, daily_calls
from app.bitrix24 import Bitrix24
from app.call_jobs import JobQueue
from app.common import connect
from tests.test_call_records import rec


class DailyCallsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {'call_records': {'dir': self.directory.name}}

    def recording(self, identity, day, **values):
        row = rec(identity, day, **values)
        call_records.upsert(self.con, [row])
        filename = f'call{identity}.mp3'
        Path(self.directory.name, filename).write_bytes(b'audio')
        self.con.execute('UPDATE call_records SET file=? WHERE id=?', (filename, row['id']))
        self.con.commit()
        return row

    def test_enqueue_only_current_utc_plus_five_day_with_requested_model(self):
        self.recording(1, '2026-11-11')
        self.recording(2, '2026-11-12')
        self.recording(3, '2026-11-13')
        now = dt.datetime(2026, 11, 11, 21, tzinfo=dt.timezone.utc)
        result = daily_calls.enqueue(self.con, self.config, now=now)
        job = dict(self.con.execute('SELECT * FROM call_processing_jobs').fetchone())
        self.assertEqual(result['day'], '2026-11-12')
        self.assertEqual(json.loads(job['ids']), ['call2'])
        self.assertEqual((job['model'], job['effort'], job['fast'], job['force']),
                         ('gpt-6.1-sol', 'high', 1, 0))

    def test_repeated_enqueue_reuses_job_and_preserves_running_jobs(self):
        self.recording(1, '2026-11-11')
        queue = JobQueue(self.con, {})
        existing = queue.create(['other'], force=True)
        queue.update(existing['id'], status='running')
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        first = daily_calls.enqueue(self.con, self.config, now=now)
        second = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertEqual(first['job']['id'], second['job']['id'])
        self.assertEqual(queue.get(existing['id'])['status'], 'running')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM call_processing_jobs').fetchone()[0], 2)

    def test_unavailable_audio_is_reported_and_empty_day_creates_no_job(self):
        call_records.upsert(self.con, [rec(1, '2026-11-11')])
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        result = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertEqual(result['unavailable'], 1)
        self.assertIsNone(result['job'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM call_processing_jobs').fetchone()[0], 0)

    def test_retry_enqueues_newly_downloaded_audio_after_completed_job(self):
        self.recording(1, '2026-11-11')
        call_records.upsert(self.con, [rec(2, '2026-11-11')])
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        first = daily_calls.enqueue(self.con, self.config, now=now)
        self.con.execute("UPDATE call_processing_jobs SET status='completed' WHERE id=?", (first['job']['id'],))
        from app.call_analysis import PROMPT_VERSION
        self.con.execute('INSERT INTO call_analysis(id,result,version) VALUES(?,?,?)', ('call1', '{}', PROMPT_VERSION))
        self.con.commit()
        self.recording(2, '2026-11-11')
        second = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertNotEqual(first['job']['id'], second['job']['id'])
        self.assertEqual(json.loads(self.con.execute('SELECT ids FROM call_processing_jobs WHERE id=?',
                                                    (second['job']['id'],)).fetchone()[0]), ['call2'])

    def test_retry_resumes_interrupted_work_but_respects_explicit_cancellation(self):
        self.recording(1, '2026-11-11')
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        first = daily_calls.enqueue(self.con, self.config, now=now)
        self.con.execute("UPDATE call_processing_jobs SET status='interrupted' WHERE id=?", (first['job']['id'],))
        self.con.commit()
        resumed = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertNotEqual(first['job']['id'], resumed['job']['id'])
        self.con.execute("UPDATE call_processing_jobs SET status='cancelled' WHERE id=?", (resumed['job']['id'],))
        self.con.commit()
        cancelled = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertEqual(cancelled['job']['id'], resumed['job']['id'])
        self.assertEqual(cancelled['job']['status'], 'cancelled')

    def test_wait_processes_additional_ready_calls_after_reusing_running_job(self):
        self.recording(1, '2026-11-11')
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        daily_calls.enqueue(self.con, self.config, now=now)
        self.recording(2, '2026-11-11')
        reused = daily_calls.enqueue(self.con, self.config, now=now)
        self.assertTrue(reused['reused'])
        def finish_jobs(seconds):
            from app.call_analysis import PROMPT_VERSION
            for row in self.con.execute("SELECT id,ids FROM call_processing_jobs WHERE status='queued'").fetchall():
                for identity in json.loads(row['ids']):
                    self.con.execute('INSERT OR REPLACE INTO call_analysis(id,result,version) VALUES(?,?,?)',
                                     (identity, '{}', PROMPT_VERSION))
                self.con.execute("UPDATE call_processing_jobs SET status='completed' WHERE id=?", (row['id'],))
            self.con.commit()
        with mock.patch('app.daily_calls.time.sleep', side_effect=finish_jobs), mock.patch('builtins.print'):
            status = daily_calls.wait_for_jobs(self.con, self.config, reused, now=now)
        self.assertEqual(status, 0)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM call_processing_jobs').fetchone()[0], 2)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM call_analysis WHERE result IS NOT NULL').fetchone()[0], 2)

    def test_next_day_creates_a_separate_job_and_completed_calls_are_skipped(self):
        self.recording(1, '2026-11-11')
        self.recording(2, '2026-11-12')
        first = daily_calls.enqueue(self.con, self.config,
                                    now=dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE))
        second = daily_calls.enqueue(self.con, self.config,
                                     now=dt.datetime(2026, 11, 12, 17, 30, tzinfo=daily_calls.TIMEZONE))
        self.assertNotEqual(first['job']['id'], second['job']['id'])
        self.con.execute('DELETE FROM call_processing_jobs')
        from app.call_analysis import PROMPT_VERSION
        self.con.execute('INSERT INTO call_analysis(id,result,version) VALUES(?,?,?)',
                         ('call1', '{}', PROMPT_VERSION))
        self.con.commit()
        result = daily_calls.enqueue(self.con, self.config,
                                     now=dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE))
        self.assertIsNone(result['job'])
        self.assertEqual(result['already_processed'], 1)

    def test_daily_refresh_filters_source_time_and_downloads_only_its_recordings(self):
        self.recording(99, '2026-11-10')
        client = mock.Mock()
        client.fetch_recordings.return_value = [rec(1, '2026-11-11')]
        now = dt.datetime(2026, 11, 11, 17, 30, tzinfo=daily_calls.TIMEZONE)
        with mock.patch('app.daily_calls.call_records.download_pending', return_value=(1, 0)) as download:
            daily_calls.refresh(self.con, self.config, client, {56: 'pm'}, now=now)
        self.assertEqual(client.fetch_recordings.call_args.args,
                         ({56: 'pm'}, dt.date(2026, 11, 11)))
        self.assertEqual(download.call_args.kwargs['ids'], ['call1'])

    def test_parallel_downloaders_complete_without_sharing_a_temporary_file(self):
        barrier = threading.Barrier(2)
        class Response(io.BytesIO):
            headers = {'Content-Type': 'audio/mpeg'}
        replace = call_records.os.replace
        temporary_files = []
        def publish(source, target):
            temporary_files.append(source)
            barrier.wait(3)
            replace(source, target)
        path = str(Path(self.directory.name, 'recording.mp3'))
        results, errors = [], []
        def download():
            try:
                results.append(call_records._download('https://audio.example.test/a.mp3', path))
            except Exception as error:
                errors.append(error)
        with mock.patch('app.call_records.urllib.request.urlopen', side_effect=lambda *a, **k: Response(b'audio')), \
             mock.patch('app.call_records.os.replace', side_effect=publish):
            workers = [threading.Thread(target=download) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(results, [5, 5])
        self.assertEqual(len(set(temporary_files)), 2)
        self.assertEqual(Path(path).read_bytes(), b'audio')
        self.assertEqual(list(Path(self.directory.name).glob('*.part')), [])


class DailyCallSourceTest(unittest.TestCase):
    def test_source_uses_bounded_day_and_retains_client_and_recording_metadata(self):
        client = Bitrix24({'webhook': 'https://crm.example.test/rest/1/test/'}, log=lambda *args: None)
        rows = [
            {'ID': '1', 'PORTAL_USER_ID': '56', 'CALL_START_DATE': '2026-11-11T23:30:00+03:00',
             'CRM_ENTITY_TYPE': 'CONTACT', 'CRM_ENTITY_ID': '7', 'RECORD_FILE_ID': '8'},
            {'ID': '2', 'PORTAL_USER_ID': '56', 'CALL_START_DATE': '2026-11-12T11:30:00+05:00',
             'CRM_ENTITY_TYPE': 'CONTACT', 'CRM_ENTITY_ID': '7', 'RECORD_FILE_ID': '9'},
            {'ID': '3', 'PORTAL_USER_ID': '56', 'CALL_START_DATE': '2026-11-13T00:00:00+05:00',
             'RECORD_FILE_ID': '10'},
        ]
        client.list_all = mock.Mock(return_value=rows)
        contact = {'7': {'ID': '7', 'NAME': 'Иван', 'LAST_NAME': 'Тестов', 'COMPANY_ID': '42'}}
        client.by_ids = lambda method, identities, select: (contact if method == 'crm.contact.list'
                                                            else {'42': {'ID': '42', 'TITLE': 'Тестовая компания'}})
        result = client.fetch_recordings({56: 'pm'}, dt.date(2026, 11, 12))
        self.assertEqual([row['id'] for row in result], ['call1', 'call2'])
        self.assertEqual(result[0]['dt'], '2026-11-12T01:30:00')
        self.assertEqual(result[0]['client_ref'], 'COMPANY:42')
        self.assertEqual(result[0]['client'], 'Тестовая компания')
        filters = client.list_all.call_args.args[1]['FILTER']
        self.assertEqual(filters['>=CALL_START_DATE'], '2026-11-12T00:00:00+05:00')
        self.assertEqual(filters['<CALL_START_DATE'], '2026-11-13T00:00:00+05:00')
        client.by_ids = lambda method, identities, select: contact if method == 'crm.contact.list' else {}
        fallback = client.fetch_recordings({56: 'pm'}, dt.date(2026, 11, 12))
        self.assertEqual(fallback[0]['client'], 'Иван Тестов')
        self.assertEqual(fallback[0]['client_ref'], 'CONTACT:7')


if __name__ == '__main__':
    unittest.main()
