"""Расшифровка записей звонков на этом компьютере (таблица call_transcripts).

Распознавание — call-analytics/transcribe.py (GigaAM v2 через sherpa-onnx, быстрый режим): отдельный процесс
с пониженным приоритетом, чтобы дашборд не тормозил и модель (~600 МБ) не держалась в его памяти.
Запускается из фонового прохода call_records.run после скачивания записей, не больше per_run записей за раз,
новые первыми. Голосовое меню АТС в начале входящих звонков («нажмите 1 для соединения…») в текст не попадает."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading

from .common import ROOT, db_lock

WORKER = os.path.join(ROOT, "call-analytics", "transcribe.py")
ENGINE = "gigaam-v2"
MAX_ATTEMPTS = 3
SILENCE_TIMEOUT = 30 * 60  # нет ни одного результата полчаса — процесс распознавания считается зависшим
# фразы голосового меню; дополнить можно в config.json → call_records.ivr_phrases
IVR_PHRASES = ["вас приветствует компания", "для соединения с", "наберите внутренний номер", "оставайтесь на линии",
               "первый свободный сотрудник", "для повторного прослушивания", "ваш звонок очень важен",
               "все операторы заняты", "разговор может быть записан"]
DEFAULTS = {"transcribe": True, "transcribe_per_run": 200, "transcribe_threads": 4}


def settings(config):
    return {**DEFAULTS, "ivr_phrases": IVR_PHRASES, **(config.get("call_records") or {})}


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def drop_ivr(segments, phrases):
    """Убрать фразы голосового меню: фрагменты, где есть одна из phrases (сравнение без регистра и «ё»)."""
    norm = lambda s: re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()
    ph = [norm(p) for p in phrases if p]
    return [s for s in segments if not any(p in norm(s[2]) for p in ph)]


def plain_text(segments):
    return " ".join(s[2] for s in segments)


def get(con, record_id):
    r = con.execute("SELECT * FROM call_transcripts WHERE id=?", (str(record_id),)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["segments"] = json.loads(r["segments"] or "[]")
    return r


def pending(con, limit):
    return [dict(r) for r in con.execute(
        "SELECT c.id, c.file FROM call_records c LEFT JOIN call_transcripts t ON t.id = c.id "
        "WHERE c.file IS NOT NULL AND (t.id IS NULL OR (t.error IS NOT NULL AND t.attempts < ?)) "
        "ORDER BY c.dt DESC LIMIT ?", (MAX_ATTEMPTS, int(limit)))]


def save(con, record_id, res, phrases, spent=None):
    with db_lock():
        if res.get("error"):
            con.execute("INSERT INTO call_transcripts (id, error, attempts, created) VALUES (?,?,1,?) "
                        "ON CONFLICT(id) DO UPDATE SET error=excluded.error, attempts=attempts+1, created=excluded.created",
                        (record_id, res["error"], _now()))
        else:
            segs = drop_ivr(res.get("segments") or [], phrases)
            con.execute("INSERT OR REPLACE INTO call_transcripts (id, segments, text, duration, engine, spent, created, "
                        "error, attempts) VALUES (?,?,?,?,?,?,?,NULL,0)",
                        (record_id, json.dumps(segs, ensure_ascii=False), plain_text(segs), res.get("duration"),
                         ENGINE, spent, _now()))
        con.commit()


def run(con, config, records_dir, log=print, worker=None):
    """Расшифровать очередь. Возвращает (готово, ошибок)."""
    s = settings(config)
    if not s["transcribe"]:
        return 0, 0
    rows = pending(con, s["transcribe_per_run"])
    if not rows:
        return 0, 0
    by_path = {os.path.normcase(os.path.join(records_dir, r["file"])): r["id"] for r in rows}
    cmd = [sys.executable, worker or WORKER, "--jsonl", "--fast", f"--threads={int(s['transcribe_threads'])}"]
    flags = 0x00004000 if os.name == "nt" else 0  # BELOW_NORMAL_PRIORITY_CLASS: дашборд отвечает без задержек
    err_path = s.get("transcribe_log") or os.path.join(ROOT, "data", "transcribe.err.log")
    os.makedirs(os.path.dirname(err_path), exist_ok=True)
    ok = failed = 0
    with open(err_path, "a", encoding="utf-8") as err:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                             encoding="utf-8", cwd=ROOT, creationflags=flags)
        timer = threading.Timer(SILENCE_TIMEOUT, p.kill)
        timer.start()

        def feed():
            # список — в отдельном потоке: процесс берёт файлы по одному и пишет результаты, и если ждать, пока он
            # примет весь список, оба процесса встанут (канал stdout переполнен, stdin не читается)
            try:
                for path in by_path:
                    p.stdin.write(path + "\n")
                    p.stdin.flush()
                p.stdin.close()
            except OSError:
                pass  # процесс распознавания завершился раньше — итог видно по его коду возврата

        threading.Thread(target=feed, daemon=True).start()
        try:
            started = dt.datetime.now()
            for line in p.stdout:
                timer.cancel()
                timer = threading.Timer(SILENCE_TIMEOUT, p.kill)
                timer.start()
                try:
                    res = json.loads(line)
                except ValueError:
                    continue
                rid = by_path.get(os.path.normcase(res.get("path") or ""))
                if not rid:
                    continue
                spent = round((dt.datetime.now() - started).total_seconds(), 1)
                started = dt.datetime.now()
                save(con, rid, res, s["ivr_phrases"], spent)
                if res.get("error"):
                    failed += 1
                else:
                    ok += 1
            p.wait()
        finally:
            timer.cancel()
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
    if p.returncode and not ok:
        log(f"Расшифровка: процесс завершился с кодом {p.returncode}, подробности в data/transcribe.err.log")
    return ok, failed
