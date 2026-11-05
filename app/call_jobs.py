"""Persistent, bounded manual call jobs. Cancellation finishes the current call."""
import datetime as dt
import json
import threading
import uuid

from .common import db_lock
from . import call_analysis, call_records, transcripts


TERMINAL = {"completed", "cancelled", "failed", "blocked", "interrupted"}


def timestamp():
    return dt.datetime.now().isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, con, config, workers=None):
        self.con, self.config = con, config
        self.workers = max(1, min(8, int(workers or config.get("call_records", {}).get("analysis_workers", 3))))
        self.active, self.call_locks = {}, {}
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.scheduler = None
        with db_lock():
            con.execute("UPDATE call_processing_jobs SET status='interrupted', finished=?, "
                        "message='Прервано перезапуском сервиса; создайте новую задачу для продолжения' "
                        "WHERE status IN ('running','cancelling')", (timestamp(),))
            con.commit()

    def get(self, jid):
        with db_lock():
            row = self.con.execute("SELECT * FROM call_processing_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("Задача не найдена")
            result = dict(row)
            result.pop("ids", None)
            return result

    def listing(self):
        with db_lock():
            rows = self.con.execute("SELECT id FROM call_processing_jobs WHERE status IN ('queued','running','cancelling') "
                                    "OR id IN (SELECT id FROM call_processing_jobs WHERE status NOT IN "
                                    "('queued','running','cancelling') ORDER BY created DESC,rowid DESC LIMIT 100) "
                                    "ORDER BY created DESC,rowid DESC").fetchall()
            jobs = [self.get(r["id"]) for r in rows]
        return {"jobs": jobs, "workers": self.workers,
                "running": any(j["status"] in ("running", "cancelling") for j in jobs),
                "pending": any(j["status"] == "queued" for j in jobs),
                "timezone": dt.datetime.now().astimezone().strftime("UTC%z")}

    def create(self, ids, force=False, model=call_analysis.MODEL, effort=call_analysis.EFFORT,
               run_at=None, author=""):
        if model not in call_analysis.MODELS or effort not in call_analysis.EFFORTS:
            raise ValueError("Неизвестная модель или уровень усилий")
        if not isinstance(ids, (list, tuple)) or not ids or len(ids) > 10000:
            raise ValueError("Выберите от 1 до 10 000 звонков")
        ids = list(dict.fromkeys(str(rid) for rid in ids))
        scheduled = None
        if run_at not in (None, ""):
            try:
                if not isinstance(run_at, str) or "T" not in run_at:
                    raise ValueError()
                scheduled = dt.datetime.fromisoformat(run_at)
                if scheduled.tzinfo or scheduled <= dt.datetime.now():
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError("Укажите будущие дату и время запуска по времени сервиса") from None
        now, jid = timestamp(), uuid.uuid4().hex
        with db_lock():
            if not force:
                ids = list(call_analysis.todo(self.con, ids))
            if not ids:
                raise ValueError("Все выбранные звонки уже обработаны")
            count = self.con.execute("SELECT COUNT(*) FROM call_processing_jobs WHERE status NOT IN "
                                     "('completed','cancelled','failed','blocked','interrupted')").fetchone()[0]
            if count >= 100:
                raise ValueError("В очереди уже 100 задач; дождитесь завершения или отмените ненужные")
            self.con.execute("INSERT INTO call_processing_jobs "
                             "(id,ids,force,model,effort,author,created,run_at,status,total,message) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (jid, json.dumps(ids), int(force), model, effort, author, now,
                              scheduled.isoformat(timespec="seconds") if scheduled else now,
                              "queued", len(ids), "Ожидает указанного времени" if scheduled else "В очереди"))
            self.con.commit()
        self.wake.set()
        return self.get(jid)

    def update(self, jid, **fields):
        with db_lock():
            self.con.execute("UPDATE call_processing_jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                             (*fields.values(), jid))
            self.con.commit()

    def cancel(self, jid):
        with db_lock():
            row = self.get(jid)
            if row["status"] in TERMINAL:
                return row
            active = self.active.get(jid)
            if active:
                active[0].set()
                self.update(jid, status="cancelling", message="Отмена: текущий звонок завершится, следующие не начнутся")
            else:
                self.update(jid, status="cancelled", finished=timestamp(), message="Отменено")
        self.wake.set()
        return self.get(jid)

    def start(self):
        if self.scheduler is not None:
            return
        def schedule():
            while not self.stopped.is_set():
                self.wake.clear()
                try:
                    self.tick()
                except Exception as exc:
                    print(f"Очередь обработки звонков: {type(exc).__name__}: {exc}", flush=True)
                self.wake.wait(1)
        self.scheduler = threading.Thread(target=schedule, daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        with db_lock():
            active = list(self.active.values())
            for event, _thread in active:
                event.set()
        if self.scheduler:
            self.scheduler.join(2)
        for _event, thread in active:
            thread.join(3)

    def tick(self, now=None):
        with db_lock():
            if self.stopped.is_set():
                return
            slots = self.workers - len(self.active)
            if slots <= 0:
                return
            due = (now or dt.datetime.now()).isoformat(timespec="seconds")
            rows = self.con.execute("SELECT * FROM call_processing_jobs WHERE status='queued' AND run_at<=? "
                                    "ORDER BY run_at,created,rowid LIMIT ?", (due, slots)).fetchall()
            for row in rows:
                job, event = dict(row), threading.Event()
                self.update(job["id"], status="running", started=timestamp(), message="Начинаем обработку")
                worker = threading.Thread(target=self.work, args=(job, event), daemon=True)
                self.active[job["id"]] = (event, worker)
                worker.start()

    def process_call(self, rid, force, cancel):
        with db_lock():
            plan = call_analysis.todo(self.con, [rid])
            if not force and rid not in plan:
                return "skipped"
            row = self.con.execute("SELECT engine FROM call_transcripts WHERE id=? AND segments IS NOT NULL",
                                   (rid,)).fetchone()
            needs_transcript = not row or row["engine"] != transcripts.ENGINE
        if needs_transcript:
            while not cancel.is_set():
                if call_records._running.acquire(timeout=.2):
                    try:
                        if cancel.is_set():
                            return "cancelled"
                        transcripts.run(self.con, self.config, call_records.records_dir(self.config), print, ids=[rid])
                    finally:
                        call_records._running.release()
                    break
            if cancel.is_set():
                return "cancelled"
        with db_lock():
            row = self.con.execute("SELECT c.*,t.segments,t.text FROM call_records c JOIN call_transcripts t ON t.id=c.id "
                                   "WHERE c.id=? AND t.engine=? AND t.segments IS NOT NULL", (rid, transcripts.ENGINE)).fetchone()
        if not row:
            return "failed"
        if cancel.is_set():
            return "cancelled"
        titles = {e["login"]: e.get("title") for e in self.config.get("employees", [])}
        return call_analysis.analyze_row(self.con, self.config, dict(row), titles,
                                         model=self.current.model, effort=self.current.effort)

    def work(self, job, cancel):
        jid, done, failed, skipped = job["id"], 0, 0, 0
        state, message = "completed", "Готово"
        try:
            self.current.model, self.current.effort = job["model"], job["effort"]
            for index, rid in enumerate(json.loads(job["ids"]), 1):
                if cancel.is_set():
                    break
                with db_lock():
                    lock = self.call_locks.setdefault(rid, threading.Lock())
                acquired = False
                try:
                    self.update(jid, current=rid, message=f"Обработка {index} из {job['total']} (при занятости — ожидание)")
                    while not cancel.is_set():
                        if lock.acquire(timeout=.2):
                            acquired = True
                            break
                    if cancel.is_set():
                        break
                    result = self.process_call(rid, bool(job["force"]), cancel)
                    if result == "limit":
                        state, message = "blocked", "Лимит подписки Codex; создайте новую задачу позже"
                        break
                    done += result == "ok"
                    failed += result == "failed"
                    skipped += result == "skipped"
                    self.update(jid, done=done, failed=failed, skipped=skipped)
                finally:
                    if acquired:
                        lock.release()
            if cancel.is_set():
                state, message = "cancelled", "Отменено; результаты завершённых звонков сохранены"
        except Exception as exc:
            state, message = "failed", f"Ошибка: {type(exc).__name__}: {exc}"[:1000]
        finally:
            with db_lock():
                # Cancel may arrive after the last loop check but before final state is saved.
                if cancel.is_set():
                    state, message = "cancelled", "Отменено; результаты завершённых звонков сохранены"
                self.update(jid, status=state, message=message, current=None, finished=timestamp())
                self.active.pop(jid, None)
                if not self.active:
                    self.call_locks.clear()
            self.wake.set()

    current = threading.local()
