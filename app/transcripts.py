"""Расшифровка записей звонков на этом компьютере (таблица call_transcripts).

Распознавание — call-analytics/transcribe.py (GigaAM v3 с пунктуацией через sherpa-onnx, быстрый режим): отдельный
процесс с пониженным приоритетом, чтобы дашборд не тормозил и модель не держалась в его памяти.
Запускается из фонового прохода call_records.run после скачивания записей, не больше per_run записей за раз,
от новых звонков к старым; тексты прежней модели (ENGINE сменился) делаются заново в том же порядке.
Голосовое меню АТС («нажмите 1 для соединения…») вырезается по предложениям."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading

from .common import ROOT, db_lock

WORKER = os.path.join(ROOT, "call-analytics", "transcribe.py")
ENGINE = "gigaam-v3-punct"
MAX_ATTEMPTS = 3
SILENCE_TIMEOUT = 30 * 60  # нет ни одного результата полчаса — процесс распознавания считается зависшим
# фразы голосового меню; дополнить можно в config.json → call_records.ivr_phrases
IVR_PHRASES = ["вас приветствует компания", "для соединения с", "наберите внутренний номер", "оставайтесь на линии",
               "первый свободный сотрудник", "для повторного прослушивания", "ваш звонок очень важен",
               "все операторы заняты", "разговор может быть записан", "продолжая разговор, вы соглашаетесь",
               "соглашаетесь с обработкой персональных данных",
               # меню и автоответчики компаний, куда звонят менеджеры
               "вы позвонили в", "вы обратились в", "разговоры записываются", "наберите добавочный",
               "добавочный номер сотрудника", "дождитесь ответа", "позиция в очереди", "в тональном режиме"]
DEFAULTS = {"transcribe": True, "transcribe_per_run": 200, "transcribe_threads": 4}


def settings(config):
    return {**DEFAULTS, "ivr_phrases": IVR_PHRASES, **(config.get("call_records") or {})}


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def drop_ivr(segments, phrases):
    """Убрать голосовое меню: предложения, где есть одна из phrases (без регистра и «ё»); фрагмент, от которого
    ничего не осталось, — целиком. Живая речь в том же фрагменте («…оставайтесь на линии. Алло!») сохраняется."""
    norm = lambda s: re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()
    ph = [norm(p) for p in phrases if p]
    out = []
    for s in segments:
        sentences = [x for x in re.split(r"(?<=[.!?…])\s+", s[2]) if x.strip()]
        keep = [x for x in sentences if not any(p in norm(x) for p in ph)]
        if keep:
            out.append([s[0], s[1], " ".join(keep)])
    return out


def plain_text(segments):
    return " ".join(s[2] for s in segments)


def get(con, record_id):
    r = con.execute("SELECT * FROM call_transcripts WHERE id=?", (str(record_id),)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["segments"] = json.loads(r["segments"] or "[]")
    return r


def pending(con, limit, ids=None):
    """Очередь: не расшифрованные, ошибки и тексты прежней модели (попыток < MAX_ATTEMPTS) — строго от новых звонков
    к старым: свежие звонки смотрят чаще, и разбор (только по текстам текущей модели) идёт в том же порядке.
    ids — ручная обработка выбранных звонков: попытки не ограничиваются."""
    if ids is not None:
        ids = list(ids)
        return [dict(r) for r in con.execute(
            "SELECT c.id, c.file FROM call_records c LEFT JOIN call_transcripts t ON t.id = c.id "
            f"WHERE c.file IS NOT NULL AND c.id IN ({','.join('?' * len(ids)) or 'NULL'}) "
            "AND (t.id IS NULL OR t.error IS NOT NULL OR t.engine <> ?) ORDER BY c.dt DESC", (*ids, ENGINE))]
    return [dict(r) for r in con.execute(
        "SELECT c.id, c.file FROM call_records c LEFT JOIN call_transcripts t ON t.id = c.id "
        "WHERE c.file IS NOT NULL AND (t.id IS NULL OR (t.attempts < ? AND (t.error IS NOT NULL OR t.engine <> ?))) "
        "ORDER BY c.dt DESC LIMIT ?", (MAX_ATTEMPTS, ENGINE, int(limit)))]


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


def run(con, config, records_dir, log=print, worker=None, ids=None, progress=None):
    """Расшифровать очередь (или выбранные звонки ids). Возвращает (готово, ошибок); progress(готово, ошибок) — после каждого."""
    s = settings(config)
    if not s["transcribe"] and ids is None:
        return 0, 0
    rows = pending(con, s["transcribe_per_run"], ids)
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
                if progress:
                    progress(ok, failed)
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
