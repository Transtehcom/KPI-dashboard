import datetime as dt
import threading
import time
import unittest
from unittest import mock

from app.common import connect
from app.call_jobs import JobQueue


class CallJobsTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(":memory:")
        self.queue=JobQueue(self.con,{},workers=2)
        self.addCleanup(self.con.close)
        self.addCleanup(self.queue.stop)

    def wait_status(self,jid,status):
        for _ in range(200):
            row=self.queue.get(jid)
            if row["status"] in status:return row
            time.sleep(.01)
        self.fail(str(self.queue.get(jid)))

    def test_fast_is_persisted_and_forwarded_to_worker(self):
        seen=[]
        def process(*args):
            seen.append(self.queue.current.config["codex_fast"])
            return "ok"
        self.queue.process_call=process
        job=self.queue.create(["fast"],force=True,fast=True)
        self.assertEqual(job["fast"],1)
        self.queue.tick()
        self.wait_status(job["id"],{"completed"})
        self.assertEqual(seen,[True])

    def test_two_jobs_run_in_parallel_and_third_waits(self):
        gate=threading.Event(); entered=[]
        self.addCleanup(gate.set)
        def process(rid,force,cancel):
            entered.append(rid); gate.wait(2); return "ok"
        self.queue.process_call=process
        jobs=[self.queue.create([str(n)],force=True) for n in range(3)]
        self.queue.tick()
        self.wait_status(jobs[0]["id"],{"running"}); self.wait_status(jobs[1]["id"],{"running"})
        self.assertEqual(self.queue.get(jobs[2]["id"])["status"],"queued")
        for _ in range(100):
            if len(entered)==2:break
            time.sleep(.01)
        self.assertEqual(len(entered),2)
        gate.set()
        for j in jobs[:2]:self.wait_status(j["id"],{"completed"})
        self.queue.tick(); self.wait_status(jobs[2]["id"],{"completed"})

    def test_cancel_queued_and_running_stops_next_call(self):
        entered=threading.Event(); release=threading.Event(); calls=[]
        self.addCleanup(release.set)
        def process(rid,force,cancel):
            calls.append(rid); entered.set(); release.wait(2); return "ok"
        self.queue.process_call=process
        j=self.queue.create(["a","b"],force=True); other=self.queue.create(["c"],force=True)
        self.queue.cancel(other["id"]); self.queue.tick()
        self.assertTrue(entered.wait(1)); self.queue.cancel(j["id"])
        release.set(); self.wait_status(j["id"],{"cancelled"})
        self.assertEqual(calls,["a"])
        self.assertEqual(self.queue.get(other["id"])["status"],"cancelled")

    def test_schedule_validation_and_due_dispatch(self):
        future=(dt.datetime.now()+dt.timedelta(days=1)).replace(microsecond=0)
        self.queue.process_call=lambda *args:"ok"
        j=self.queue.create(["a"],force=True,run_at=future.isoformat())
        self.queue.tick(); self.assertEqual(self.queue.get(j["id"])["status"],"queued")
        self.queue.tick(now=future+dt.timedelta(seconds=1)); self.wait_status(j["id"],{"completed"})
        for value in ("bad", "2020-01-01T12:00", "2026-01-01", "2026-11-06T12:00Z"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.queue.create(["a"],force=True,run_at=value)

    def test_restart_keeps_scheduled_and_interrupts_running(self):
        j=self.queue.create(["a"],force=True)
        self.con.execute("UPDATE call_processing_jobs SET status='running' WHERE id=?",(j["id"],));self.con.commit()
        queued=self.queue.create(["b"],force=True)
        restarted=JobQueue(self.con,{})
        self.assertEqual(restarted.get(j["id"])["status"],"interrupted")
        self.assertEqual(restarted.get(queued["id"])["status"],"queued")

    def test_same_call_never_runs_concurrently(self):
        gate=threading.Event(); entered=threading.Event(); count=[0,0]
        self.addCleanup(gate.set)
        def process(*args):
            count[0]+=1; count[1]=max(count); entered.set(); gate.wait(1);count[0]-=1; return "ok"
        self.queue.process_call=process
        jobs=[self.queue.create(["a"],force=True) for _ in range(2)]
        self.queue.tick();self.assertTrue(entered.wait(1));time.sleep(.03);gate.set()
        for j in jobs:self.wait_status(j["id"],{"completed"})
        self.assertEqual(count[1],1)

    def test_limit_and_exception_end_job(self):
        for result,status in (("limit","blocked"),("failed","failed")):
            self.queue.process_call=lambda *args:result
            j=self.queue.create(["a"],force=True);self.queue.tick()
            row=self.wait_status(j["id"],{status})
            self.assertEqual(row["failed"],int(result=="failed"))

    def test_old_scheduled_job_remains_visible_when_history_is_full(self):
        future=(dt.datetime.now()+dt.timedelta(days=30)).isoformat(timespec="seconds")
        scheduled=self.queue.create(["a"],force=True,run_at=future)
        for _ in range(101):
            j=self.queue.create(["b"],force=True)
            self.queue.update(j["id"],status="completed")
        state=self.queue.listing()
        self.assertTrue(state["pending"])
        self.assertEqual(len(state["jobs"]),101)
        self.assertIn(scheduled["id"],[j["id"] for j in state["jobs"]])
        self.assertEqual(self.queue.cancel(scheduled["id"])["status"],"cancelled")

    def test_exception_releases_capacity_for_next_job(self):
        self.queue.workers=1
        def fail(*args):raise RuntimeError("test failure")
        self.queue.process_call=fail
        first=self.queue.create(["a"],force=True);second=self.queue.create(["b"],force=True)
        self.queue.tick();self.wait_status(first["id"],{"failed"})
        self.queue.process_call=lambda *args:"ok"
        self.queue.tick();self.wait_status(second["id"],{"completed"})

    def test_cancel_while_waiting_for_background_transcription(self):
        from app import call_records,transcripts
        call_records.upsert(self.con,[{"id":"a","emp":"pm","dt":"2026-11-05T10:00:00"}])
        acquired=call_records._running.acquire(timeout=1)
        self.assertTrue(acquired)
        try:
            with mock.patch.object(transcripts,"run") as transcribe:
                j=self.queue.create(["a"],force=True);self.queue.tick()
                self.wait_status(j["id"],{"running"})
                self.queue.cancel(j["id"])
                self.wait_status(j["id"],{"cancelled"})
                transcribe.assert_not_called()
        finally:
            call_records._running.release()

    def test_real_processor_serializes_transcription_between_jobs(self):
        from app import call_records,transcripts,call_analysis
        call_records.upsert(self.con,[{"id":rid,"emp":"pm","dt":"2026-11-05T10:00:00"} for rid in ("a","b")])
        count=[0,0]
        def transcribe(con,config,path,log,ids):
            count[0]+=1;count[1]=max(count);time.sleep(.03)
            from app.common import db_lock
            with db_lock():
                con.execute("INSERT INTO call_transcripts (id,segments,text,engine) VALUES (?,'[]','test',?)",(ids[0],transcripts.ENGINE));con.commit()
            count[0]-=1
        with mock.patch.object(transcripts,"run",side_effect=transcribe),mock.patch.object(call_analysis,"analyze_row",return_value="ok"):
            jobs=[self.queue.create([rid],force=True) for rid in ("a","b")]
            self.queue.tick()
            for j in jobs:self.wait_status(j["id"],{"completed"})
        self.assertEqual(count[1],1)

    def test_failed_call_stores_reason_and_is_not_reported_successful(self):
        def fail(*args):
            self.queue.current.error="Превышено время ожидания разбора: 900 секунд"
            return "failed"
        self.queue.process_call=fail
        job=self.queue.create(["a"],force=True);self.queue.tick()
        row=self.wait_status(job["id"],{"failed"})
        self.assertEqual(row["errors"][0]["id"],"a")
        self.assertIn("900",row["errors"][0]["message"])
        self.assertNotEqual(row["message"],"Готово")

    def test_partial_success_has_distinct_status(self):
        self.queue.process_call=lambda rid,*args:"ok" if rid=="a" else "failed"
        job=self.queue.create(["a","b"],force=True);self.queue.tick()
        row=self.wait_status(job["id"],{"completed_with_errors"})
        self.assertEqual((row["done"],row["failed"]),(1,1))

    def test_legacy_timeout_job_is_migrated_with_readable_error_snapshot(self):
        job=self.queue.create(["a"],force=True)
        self.queue.update(job["id"],status="completed",done=0,failed=1,errors=None,
                          started="2026-11-05T16:00:00",finished="2026-11-05T16:10:00")
        self.con.execute("INSERT INTO call_analysis (id,error,created) VALUES (?,?,?)",
                         ("a","TimeoutExpired: Command ['codex'] timed out after 300 seconds","2026-11-05T16:05:00"))
        self.con.commit()
        restarted=JobQueue(self.con,{})
        row=restarted.get(job["id"])
        self.assertEqual(row["status"],"failed")
        self.assertIn("300 секунд",row["errors"][0]["message"])
        self.assertNotIn("Command",row["errors"][0]["message"])
        self.con.execute("UPDATE call_analysis SET error=NULL WHERE id='a'");self.con.commit()
        self.assertIn("300 секунд",restarted.get(job["id"])["errors"][0]["message"])
