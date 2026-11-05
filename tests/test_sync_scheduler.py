import datetime as dt
import unittest
from unittest import mock

from app import sync
from app.common import connect


class StopLoop(Exception):
    pass


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(':memory:')
        self.addCleanup(self.con.close)
        self.config={'mode':'live','sync':{'interval_minutes':60}}
        self.now=dt.datetime(2026,11,3,15,53)

    def record(self,started,finished,status='ok'):
        self.con.execute('INSERT INTO sync_log(started,finished,status,message) VALUES(?,?,?,?)',
                         (started,finished,status,'test'))
        self.con.commit()

    def first_cycle(self,busy=False):
        waits=[]
        def sleep(seconds):
            waits.append(seconds)
            raise StopLoop()
        def fake_import(con,config):
            self.record(self.now.isoformat(),self.now.isoformat())
            return {'status':'ok'}
        with mock.patch('app.sync.threading.Thread') as thread:
            sync.start_scheduler(self.con,self.config)
            loop=thread.call_args.kwargs['target']
        with mock.patch('app.sync.dt.datetime',wraps=dt.datetime) as clock, \
             mock.patch('time.sleep',side_effect=sleep), \
             mock.patch('app.sync.is_running',return_value=busy), \
             mock.patch('app.sync.run',side_effect=fake_import):
            clock.now.return_value=self.now
            with self.assertRaises(StopLoop):
                loop()
        return waits

    def test_restart_immediately_catches_up_a_missed_hour(self):
        self.record('2026-11-03T12:09:26','2026-11-03T12:15:21')
        self.first_cycle()
        self.assertEqual(sync.last_sync(self.con)['started'],'2026-11-03T15:53:00')

    def test_no_extra_hour_is_added_to_import_duration(self):
        self.record('2026-11-03T14:50:00','2026-11-03T14:56:00')
        self.first_cycle()
        self.assertEqual(sync.last_sync(self.con)['started'],'2026-11-03T15:53:00')

    def test_recent_manual_run_is_respected_and_checked_within_30_seconds(self):
        self.record('2026-11-03T15:40:00','2026-11-03T15:46:00')
        waits=self.first_cycle()
        self.assertEqual(sync.last_sync(self.con)['started'],'2026-11-03T15:40:00')
        self.assertLessEqual(waits[0],30)

    def test_busy_run_is_not_started_again(self):
        self.record('2026-11-03T12:09:26','2026-11-03T12:15:21')
        waits=self.first_cycle(busy=True)
        self.assertEqual(sync.last_sync(self.con)['started'],'2026-11-03T12:09:26')
        self.assertLessEqual(waits[0],30)

    def test_failed_import_retries_in_five_minutes(self):
        self.record('2026-11-03T15:40:00','2026-11-03T15:47:00','error')
        self.first_cycle()
        self.assertEqual(sync.last_sync(self.con)['status'],'ok')

    def test_disabled_scheduler_has_no_background_thread(self):
        for config in ({'mode':'demo'}, {'mode':'live','sync':{'interval_minutes':0}}):
            with mock.patch('app.sync.threading.Thread') as thread:
                sync.start_scheduler(self.con,config)
            thread.assert_not_called()
