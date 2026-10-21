"""Записи телефонных разговоров: звонки с записью (таблица call_records) и аудио в папке записей.

Сведения о звонках приходят при синхронизации (Bitrix24.fetch → upsert). Аудио скачивается в фоне после неё:
копия на Диске Б24, которую телефония кладёт к звонку, а если её нет — по ссылке на АТС. За один проход —
не больше max_per_run файлов: первая выгрузка за полгода растягивается на несколько синхронизаций
и не задерживает обновление KPI. Там же звонки получают привязки своего дела CRM (сделки, лиды, контакты)."""
import datetime as dt
import os
import threading
import time
import traceback
import urllib.request

from .common import ROOT, db_lock

# dir — папка записей (относительно папки дашборда или абсолютный путь, в т.ч. сетевой);
# keep_days — сколько дней хранить аудио (0 — бессрочно); download_days — за сколько последних дней
# скачивать (0 — за весь период синхронизации); max_per_run — файлов за один проход
DEFAULTS = {"dir": "data/records", "keep_days": 0, "download_days": 0, "max_per_run": 300}
META = ["call_id", "emp", "dt", "direction", "duration", "phone", "crm_entity", "client", "crm_activity_id",
        "record_file_id", "record_url"]
TYPE_NAMES = {1: "LEAD", 2: "DEAL", 3: "CONTACT", 4: "COMPANY"}
RECHECK_DAYS = 3  # привязки свежих звонков перепроверяются: сделку по клиенту могут завести после звонка
MAX_ATTEMPTS = 5

_running = threading.Lock()
_status = {"started": None, "finished": None, "message": ""}


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def settings(config):
    return {**DEFAULTS, **(config.get("call_records") or {})}


def records_dir(config):
    d = settings(config)["dir"]
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def file_path(config, row):
    """Полный путь к аудио записи или None, если оно не скачано или уже удалено по сроку хранения."""
    return os.path.join(records_dir(config), row["file"]) if row.get("file") else None


# ---------- сведения о звонках

def upsert(con, rows, commit=True):
    """Добавить новые звонки и обновить сведения о прежних; скачанный файл и привязки не трогаются."""
    cols = ["id"] + META + ["updated"]
    q = (f"INSERT INTO call_records ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
         f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in META + ['updated'])}")
    now = _now()
    with db_lock():
        con.executemany(q, [[r.get(c) for c in cols[:-1]] + [now] for r in rows])
        if commit:
            con.commit()


def get(con, record_id):
    r = con.execute("SELECT * FROM call_records WHERE id=?", (str(record_id),)).fetchone()
    return dict(r) if r else None


def deals_of(row):
    return [int(k[5:]) for k in (row.get("bindings") or "").split(",") if k.startswith("DEAL:") and k[5:].isdigit()]


# ---------- права и журнал прослушиваний

LISTEN_REPEAT_MINUTES = 10  # повторные запросы одной записи в эти минуты (перемотка, повтор) — одно прослушивание


def audible_logins(config, me):
    """Чьи записи может слушать пользователь: None — любые (администратор); иначе список логинов.
    Свои — если не запрещено call_records.listen_own; с правом listen_dept — ещё всех сотрудников своего отдела."""
    if me.get("role") == "manager":
        return None
    out = [me["login"]] if settings(config).get("listen_own", True) else []
    dep = me.get("department")
    if me.get("listen_dept") and dep:
        out += [e["login"] for e in config.get("employees", []) if e.get("department") == dep and e["login"] != me["login"]]
    return out


def can_listen(config, me, row):
    allowed = audible_logins(config, me)
    return allowed is None or row.get("emp") in allowed


def log_listen(con, login, row, ip, result="ok"):
    """Записать прослушивание. Возвращает True, если запись добавлена (повтор в пределах LISTEN_REPEAT_MINUTES — нет)."""
    now = dt.datetime.now()
    since = (now - dt.timedelta(minutes=LISTEN_REPEAT_MINUTES)).isoformat(timespec="seconds")
    with db_lock():
        if result in ("ok", "read") and con.execute(
                "SELECT 1 FROM listen_log WHERE login=? AND record=? AND result=? AND ts>=?",
                (login, row["id"], result, since)).fetchone():
            return False
        con.execute("INSERT INTO listen_log (ts, login, record, emp, ip, result) VALUES (?,?,?,?,?,?)",
                    (now.isoformat(timespec="seconds"), login, row["id"], row.get("emp"), ip, result))
        con.commit()
    return True


def listing(con, config, emps=None, deal=None, date_from=None, date_to=None, with_listens=False):
    """Звонки с записью для раздела «Звонки»: сотрудников emps (None — всех) за период или по сделке (deal — ID сделки Б24)."""
    q, args = "SELECT * FROM call_records WHERE 1=1", []
    if emps is not None:
        q += f" AND emp IN ({','.join('?' * len(emps)) or 'NULL'})"
        args += list(emps)
    if deal is not None:
        q += " AND ','||bindings||',' LIKE ?"
        args.append(f"%,DEAL:{int(deal)},%")
    if date_from:
        q += " AND dt >= ?"
        args.append(str(date_from))
    if date_to:
        q += " AND dt <= ?"
        args.append(str(date_to) + "T23:59:59")
    rows = [dict(r) for r in con.execute(q + " ORDER BY dt DESC LIMIT 5000", args)]
    tr = {}
    if rows:
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for t in con.execute(f"SELECT id, error, attempts, segments IS NOT NULL ready FROM call_transcripts "
                                 f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                tr[t["id"]] = "ready" if t["ready"] else "error" if t["attempts"] >= 3 else "pending"
    listens = {}
    if with_listens and rows:
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in con.execute(f"SELECT record, COUNT(*) n FROM listen_log WHERE result='ok' AND record IN "
                                 f"({','.join('?' * len(chunk))}) GROUP BY record", chunk):
                listens[r["record"]] = r["n"]
    keep = settings(config)["keep_days"]
    out = []
    for r in rows:
        if r["file"]:
            audio = "ready"
        elif r["purged"] or (keep and r["dt"] < (dt.date.today() - dt.timedelta(days=keep)).isoformat()):
            audio = "purged"
        elif (r["attempts"] or 0) >= MAX_ATTEMPTS:
            audio = "error"
        else:
            audio = "pending"
        item = {k: r[k] for k in ("id", "emp", "dt", "direction", "duration", "phone", "client")}
        item.update(audio=audio, deals=deals_of(r),
                    transcript=tr.get(r["id"]) or ("pending" if audio == "ready" else None))
        if with_listens:
            item["listens"] = listens.get(r["id"], 0)
        out.append(item)
    return out


def listen_journal(con, date_from, date_to, login=None, limit=2000):
    """Журнал прослушиваний за период (новые первыми) со сведениями о звонке."""
    q = ("SELECT l.ts, l.login, l.record, l.emp, l.ip, l.result, c.dt AS call_dt, c.client, c.direction, c.duration "
         "FROM listen_log l LEFT JOIN call_records c ON c.id = l.record WHERE l.ts >= ? AND l.ts <= ?")
    args = [str(date_from), str(date_to) + "T23:59:59"]
    if login:
        q += " AND l.login=?"
        args.append(login)
    return [dict(r) for r in con.execute(q + " ORDER BY l.ts DESC LIMIT ?", args + [limit])]


def resolve_bindings(con, b24):
    """Привязки дела звонка (crm.activity.binding.list): звонок в статистике привязан к контакту или компании,
    а к сделкам — только через своё дело. Звонки старше RECHECK_DAYS проверяются один раз."""
    since = (dt.datetime.now() - dt.timedelta(days=RECHECK_DAYS)).isoformat(timespec="seconds")
    rows = con.execute("SELECT id, crm_activity_id FROM call_records WHERE crm_activity_id IS NOT NULL "
                       "AND (bindings_checked IS NULL OR dt >= ?)", (since,)).fetchall()
    if not rows:
        return 0
    results, errors = b24.batch({r["id"]: ("crm.activity.binding.list", {"activityId": r["crm_activity_id"]})
                                 for r in rows})
    now, n = _now(), 0
    with db_lock():
        for r in rows:
            if r["id"] in errors:  # дело удалено — привязок нет, повторно не спрашиваем
                con.execute("UPDATE call_records SET bindings=NULL, bindings_checked=? WHERE id=?", (now, r["id"]))
                continue
            if r["id"] not in results:
                continue
            keys = []
            for b in results[r["id"]] or []:
                k = f"{TYPE_NAMES.get(int(b['entityTypeId']), b['entityTypeId'])}:{b['entityId']}"
                if k not in keys:
                    keys.append(k)
            con.execute("UPDATE call_records SET bindings=?, bindings_checked=? WHERE id=?",
                        (",".join(keys) or None, now, r["id"]))
            n += 1
        con.commit()
    return n


# ---------- аудио

def _download(url, path):
    """Скачать файл через временный .part. Возвращает размер; страница ошибки вместо аудио — исключение."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" in ctype or "json" in ctype or ctype.startswith("text/"):
            raise ValueError(f"вместо записи пришло {ctype}")
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    size = os.path.getsize(tmp)
    if not size:
        os.remove(tmp)
        raise ValueError("пустой файл")
    os.replace(tmp, path)
    return size


def download_pending(con, b24, config, log=print):
    """Скачать ещё не скачанные записи, новые первыми. Возвращает (скачано, ошибок)."""
    s = settings(config)
    q, args = "SELECT * FROM call_records WHERE file IS NULL AND purged IS NULL AND attempts < ?", [MAX_ATTEMPTS]
    days = [d for d in (s["download_days"], s["keep_days"]) if d]
    if days:  # не качать то, что всё равно удалится по сроку хранения
        q += " AND dt >= ?"
        args.append((dt.date.today() - dt.timedelta(days=min(days))).isoformat())
    rows = [dict(r) for r in con.execute(q + " ORDER BY dt DESC LIMIT ?", args + [int(s["max_per_run"])])]
    if not rows:
        return 0, 0
    # ссылка на скачивание с Диска Б24 временная — берём её непосредственно перед скачиванием
    disk, _ = b24.batch({r["id"]: ("disk.file.get", {"id": r["record_file_id"]}) for r in rows if r["record_file_id"]})
    base, ok, failed = records_dir(config), 0, 0
    for r in rows:
        f = disk.get(r["id"]) or {}
        ext = os.path.splitext(f.get("NAME") or "")[1].lower() or ".mp3"
        rel = f"{r['dt'][:7]}/{r['id']}{ext}"  # по месяцам; номер телефона в имя файла не попадает
        size, err = None, "нет ссылки на запись"
        for url in (f.get("DOWNLOAD_URL"), r["record_url"]):
            if not url:
                continue
            try:
                size, err = _download(url, os.path.join(base, rel)), None
                break
            except Exception as e:  # noqa
                err = f"{type(e).__name__}: {e}"[:300]
        with db_lock():
            if err is None:
                con.execute("UPDATE call_records SET file=?, file_size=?, downloaded=?, error=NULL WHERE id=?",
                            (rel, size, _now(), r["id"]))
                ok += 1
            else:
                con.execute("UPDATE call_records SET attempts=attempts+1, error=? WHERE id=?", (err, r["id"]))
                failed += 1
            con.commit()
        time.sleep(0.2)
    return ok, failed


def purge_old(con, config):
    """Удалить аудио и расшифровку старше keep_days (сведения о звонке остаются, purged — когда удалено)."""
    keep = settings(config)["keep_days"]
    if not keep:
        return 0
    cutoff = (dt.date.today() - dt.timedelta(days=keep)).isoformat()
    rows = con.execute("SELECT id, file FROM call_records WHERE file IS NOT NULL AND dt < ?", (cutoff,)).fetchall()
    for r in rows:
        try:
            os.remove(os.path.join(records_dir(config), r["file"]))
        except FileNotFoundError:
            pass
        with db_lock():
            con.execute("UPDATE call_records SET file=NULL, purged=? WHERE id=?", (_now(), r["id"]))
            con.execute("DELETE FROM call_transcripts WHERE id=?", (r["id"],))
            con.commit()
    return len(rows)


def run(con, b24, config, log=print):
    if not _running.acquire(blocking=False):
        return None
    _status.update(started=_now(), finished=None, message="")
    msg = []
    try:
        purged = purge_old(con, config)
        if purged:
            msg.append(f"удалено по сроку хранения: {purged}")
        msg.append(f"привязки проверены: {resolve_bindings(con, b24)}")
        ok, failed = download_pending(con, b24, config, log)
        msg.append(f"скачано: {ok}" + (f", ошибок: {failed}" if failed else ""))
        _status["message"] = "Записи звонков: " + "; ".join(msg) + "; идёт расшифровка…"
        from . import transcripts
        ok, failed = transcripts.run(con, config, records_dir(config), log)
        if ok or failed:
            msg.append(f"расшифровано: {ok}" + (f", ошибок: {failed}" if failed else ""))
        left = con.execute("SELECT COUNT(*) FROM call_records WHERE file IS NULL AND purged IS NULL AND attempts < ?",
                           (MAX_ATTEMPTS,)).fetchone()[0]
        if left:
            msg.append(f"в очереди: {left}")
    except Exception as e:  # noqa
        msg.append(f"ошибка: {e}")
        traceback.print_exc()
    finally:
        _status.update(finished=_now(), message="Записи звонков: " + "; ".join(msg))
        log(_status["message"])
        _running.release()
    return _status["message"]


def start_background(con, config):
    """Скачать записи в фоне (своё подключение к Б24 — журнал синхронизации к этому моменту уже закрыт)."""
    if _running.locked():
        return
    from .bitrix24 import Bitrix24
    b24 = Bitrix24(config["bitrix24"], log=print)
    threading.Thread(target=run, args=(con, b24, config), daemon=True).start()


def status():
    return dict(_status, running=_running.locked())
