"""Persistent, bounded manual call jobs. Cancellation finishes the current call."""
import datetime as dt
import json
import re
import threading
import uuid

from .common import db_lock
from . import call_analysis, call_records, transcripts


TERMINAL = {"completed", "completed_with_errors", "cancelled", "failed", "blocked", "interrupted"}


def error_message(error):
    text = str(error or "Не удалось обработать звонок; причина не сохранена")
    timeout = re.search(r"timed out after ([\d.]+) seconds", text)
    if "TimeoutExpired" in text and timeout:
        return f"Превышено время ожидания разбора: {timeout[1]} секунд. Задачу можно повторить."
    return text[:1000]


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
            con.execute("UPDATE call_feedback_rechecks SET status='interrupted',finished=? WHERE status IN ('queued','running') "
                        "AND id IN (SELECT id FROM call_processing_jobs WHERE status='interrupted')", (timestamp(),))
            con.commit()
            self.recover_errors()

    def recover_errors(self):
        """Snapshot still-available errors from jobs created before per-job diagnostics existed."""
        rows = self.con.execute("SELECT * FROM call_processing_jobs WHERE errors IS NULL AND failed>0").fetchall()
        for job in rows:
            ids, errors, found = json.loads(job["ids"]), [], {}
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                ph = ",".join("?" for _ in chunk)
                for table in ("call_transcripts", "call_analysis"):
                    for row in self.con.execute(
                            f"SELECT id,error FROM {table} WHERE id IN ({ph}) AND error IS NOT NULL "
                            "AND created>=? AND created<=?",
                            (*chunk, job["started"] or job["created"], job["finished"] or timestamp())):
                        found[row["id"]] = error_message(row["error"])
                if len(found) >= 50:
                    break
            errors = [{"id": rid, "message": found[rid]} for rid in ids if rid in found][:50]
            state, message = job["status"], job["message"]
            if state == "completed":
                state = "completed_with_errors" if job["done"] or job["skipped"] else "failed"
                message = f"Обработано {job['done']}, ошибок {job['failed']}"
            self.update(job["id"], errors=json.dumps(errors, ensure_ascii=False), status=state, message=message)

    def get(self, jid):
        with db_lock():
            row = self.con.execute("SELECT * FROM call_processing_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("Задача не найдена")
            result = dict(row)
            result.pop("ids", None)
            result["errors"] = json.loads(result.get("errors") or "[]")
            result["feedback_recheck"] = bool(self.con.execute("SELECT 1 FROM call_feedback_rechecks WHERE id=?", (jid,)).fetchone())
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
               run_at=None, author="", feedback=None):
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
            if feedback is not None:
                from . import call_feedback
                if len(ids) != 1 or feedback.get("record") != ids[0]:
                    raise ValueError("Выберите один звонок для повторной проверки замечания")
                current = call_feedback.get(self.con, ids[0], feedback.get("author"))
                if not current or current["revision"] != feedback.get("revision"):
                    raise call_feedback.FeedbackConflict("Оценка изменилась; обновите замечание")
                if current["status"] != "pending":
                    raise ValueError("Нет незакрытого замечания для повторной проверки")
                feedback, force = current, True
            if not force:
                ids = list(call_analysis.todo(self.con, ids))
            if not ids:
                raise ValueError("Все выбранные звонки уже обработаны")
            count = self.con.execute("SELECT COUNT(*) FROM call_processing_jobs WHERE status IN "
                                     "('queued','running','cancelling')").fetchone()[0]
            if count >= 100:
                raise ValueError("В очереди уже 100 задач; дождитесь завершения или отмените ненужные")
            self.con.execute("INSERT INTO call_processing_jobs "
                             "(id,ids,force,model,effort,author,created,run_at,status,total,message,errors) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]')",
                             (jid, json.dumps(ids), int(force), model, effort, author, now,
                              scheduled.isoformat(timespec="seconds") if scheduled else now,
                              "queued", len(ids), "Ожидает указанного времени" if scheduled else "В очереди"))
            if feedback is not None:
                from . import call_recheck
                call_recheck.enqueue(self.con, jid, ids[0], feedback, author, now)
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
                self.con.execute("UPDATE call_feedback_rechecks SET status='cancelled',finished=? WHERE id=? AND status='queued'", (timestamp(), jid))
                self.con.commit()
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
            with db_lock():
                transcript = self.con.execute("SELECT error FROM call_transcripts WHERE id=?", (rid,)).fetchone()
            self.current.error = "Расшифровка: " + error_message(transcript["error"] if transcript else "Нет расшифровки текущей модели")
            return "failed"
        if cancel.is_set():
            return "cancelled"
        titles = {e["login"]: e.get("title") for e in self.config.get("employees", [])}
        from . import call_recheck
        with db_lock():
            review = self.con.execute("SELECT 1 FROM call_feedback_rechecks WHERE id=? AND record=?",
                                      (self.current.job_id, rid)).fetchone()
        if review:
            result = call_recheck.analyze_row(self.con, self.config, dict(row), titles, self.current.job_id,
                                              model=self.current.model, effort=self.current.effort, cancel=cancel)
        else:
            result = call_analysis.analyze_row(self.con, self.config, dict(row), titles,
                                              model=self.current.model, effort=self.current.effort)
        if result == "failed":
            with db_lock():
                error = self.con.execute("SELECT error FROM call_analysis WHERE id=?", (rid,)).fetchone()
            self.current.error = error_message(error["error"] if error else None)
        return result

    def work(self, job, cancel):
        jid, done, failed, skipped = job["id"], 0, 0, 0
        state, message = "completed", "Готово"
        errors = []
        try:
            self.current.model, self.current.effort, self.current.job_id = job["model"], job["effort"], jid
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
                    self.current.error = None
                    result = self.process_call(rid, bool(job["force"]), cancel)
                    if result == "limit":
                        state, message = "blocked", "Лимит подписки Codex; создайте новую задачу позже"
                        break
                    done += result == "ok"
                    failed += result == "failed"
                    skipped += result == "skipped"
                    if result == "failed" and len(errors) < 50:
                        errors.append({"id": rid, "message": error_message(self.current.error)})
                    self.update(jid, done=done, failed=failed, skipped=skipped,
                                errors=json.dumps(errors, ensure_ascii=False))
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
                elif state == "completed" and failed:
                    state = "completed_with_errors" if done or skipped else "failed"
                    message = f"Обработано {done}, ошибок {failed}"
                self.update(jid, status=state, message=message, current=None, finished=timestamp())
                self.con.execute("UPDATE call_feedback_rechecks SET status=?,error=?,finished=? WHERE id=? AND status IN ('queued','running')",
                                 (state, message, timestamp(), jid))
                self.con.commit()
                self.active.pop(jid, None)
                if not self.active:
                    self.call_locks.clear()
            self.wake.set()

    current = threading.local()
