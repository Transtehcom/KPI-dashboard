"""Веб-сервер дашборда: авторизация, API, статика."""
import datetime as dt
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import b24_calendar, call_records, employees, manual, sync, transcripts
from .common import (ROOT, connect, current_month, db_lock, load_config, load_rules, month_add, month_bounds,
                     weeks_of_month)
from .engine import CRITERIA_TITLES, Engine
from .funnel import DATE_FIELDS as FUNNEL_DATE_FIELDS, KINDS as FUNNEL_KINDS, deal_funnel

WEB = os.path.join(ROOT, "web")
PAGE_HEAD = (b'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
             b'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>')
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PLAN_METRICS = {"gross_profit", "revenue"}
# защита от подбора пароля: не больше LOGIN_MAX_FAILS неудачных попыток за LOGIN_WINDOW секунд
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW = 15 * 60


class BadRequest(Exception):
    pass


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 200_000).hex()
    return h, salt


def set_password(con, login, password):
    h, salt = hash_password(password)
    with db_lock():
        con.execute("INSERT OR REPLACE INTO users (login, pwd_hash, salt) VALUES (?,?,?)", (login, h, salt))
        con.commit()


class App:
    def __init__(self):
        self.config = load_config()
        self.rules = load_rules()
        self.con = connect()
        employees.apply(self.con, self.config, self.rules)  # сотрудники — из базы (раздел «Сотрудники»)
        self.engine = Engine(self.con, self.rules, self.config)
        # та же блокировка, под которой синхронизация пишет в базу: расчёт не увидит недописанные таблицы
        self.lock = db_lock()
        self.login_fails = {}  # (ip, login) -> [время неудачных попыток]

    def login_blocked(self, key):
        now = time.monotonic()
        fails = [t for t in self.login_fails.get(key, []) if now - t < LOGIN_WINDOW]
        self.login_fails[key] = fails
        return len(fails) >= LOGIN_MAX_FAILS

    def login_failed(self, key):
        self.login_fails.setdefault(key, []).append(time.monotonic())

    def user(self, login):
        return next((e for e in self.config["employees"] if e["login"] == login), None)

    def check_password(self, login, password):
        r = self.con.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        if not r or not self.user(login):
            return False
        h, _ = hash_password(password, r["salt"])
        return hmac.compare_digest(h, r["pwd_hash"])

    def session_user(self, token):
        if not token:
            return None
        r = self.con.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if not r or r["expires"] < dt.datetime.now().isoformat():
            return None
        return self.user(r["login"])

    def new_session(self, login):
        token = secrets.token_urlsafe(32)
        hours = self.config.get("server", {}).get("session_hours", 12)
        exp = (dt.datetime.now() + dt.timedelta(hours=hours)).isoformat()
        with db_lock():
            self.con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, login, exp))
            self.con.execute("DELETE FROM sessions WHERE expires < ?", (dt.datetime.now().isoformat(),))
            self.con.commit()
        return token

    # ---------- данные для интерфейса
    def department_title(self, e):
        """Отдел сотрудника из раздела «Сотрудники», иначе — отдел его KPI; полное название из kpi_rules.json."""
        dep = e.get("department") or self.rules["positions"].get(e.get("position"), {}).get("department")
        return self.rules.get("departments", {}).get(dep, dep) if dep else None

    def bootstrap(self, me):
        is_mgr = me.get("role") == "manager"
        # руководителю — все открытые сотрудники, в том числе без KPI (в меню они помечены «KPI не назначен»)
        emps = [e for e in self.config["employees"] if e.get("visible", e.get("role") != "manager")] if is_mgr else [me]
        cm = current_month()
        last = sync.last_sync(self.con)
        return {
            "me": {"login": me["login"], "name": me["name"], "role": me.get("role", "employee")},
            "employees": [{"login": e["login"], "name": e["name"], "position": e.get("position"),
                           "position_title": e.get("title") or self.rules["positions"].get(e.get("position"), {}).get("title"),
                           "department_title": self.department_title(e)}
                          for e in emps],
            "positions": {k: {"title": v["title"], "department": v["department"], "salary": v["salary"],
                              "kpi_bonus": v["kpi_bonus"], "doc": v.get("doc"),
                              "items": [{x: i.get(x) for x in ("n", "title", "weight", "period", "target_text", "description")}
                                        for i in v["items"]]}
                          for k, v in self.rules["positions"].items()},
            "months": [month_add(cm, -k) for k in range(0, 12)],
            "current_month": cm,
            "mode": self.config.get("mode", "demo"),
            "payout_mode": self.rules.get("payout", {}).get("mode", "binary"),
            "criteria_titles": CRITERIA_TITLES,
            "last_sync": last,
            "sync_running": sync.is_running(),
        }

    def calendar(self, me, is_mgr, q):
        """Расписание ответственных из «Воронок» на день: login — ключи через запятую (логин или «id:<ID в Б24>»).
        Сотрудник видит только своё расписание, руководитель — любого."""
        try:
            day = dt.date.fromisoformat(q.get("date") or dt.date.today().isoformat())
        except ValueError:
            raise BadRequest("Некорректная дата")
        cfg = self.config.get("bitrix24", {})
        if self.config.get("mode", "demo") == "demo" or not cfg.get("webhook") or "ВАШ-ПОРТАЛ" in cfg["webhook"]:
            return {"date": day.isoformat(), "unavailable": "Битрикс24 не подключён — календарь доступен в рабочем режиме"}
        by_login = {e["login"]: e.get("b24_user_id") for e in self.config["employees"]}
        own = me.get("b24_user_id")
        people = []
        for key in [k for k in (q.get("login") or "").split(",") if k][:6]:
            m = re.fullmatch(r"id:(\d+)", key)
            uid = int(m.group(1)) if m else by_login.get(key)
            if not uid:
                people.append({"key": key, "error": "Нет ID в Битрикс24 — укажите его в разделе «Сотрудники»"})
            elif not is_mgr and int(uid) != int(own or 0):
                people.append({"key": key, "error": "Расписание других сотрудников видит только руководитель"})
            else:
                try:
                    people.append({"key": key, **b24_calendar.day_schedule(cfg, int(uid), day)})
                except Exception as e:  # noqa
                    traceback.print_exc()
                    people.append({"key": key, "error": f"Битрикс24 не ответил: {e}"})
        return {"date": day.isoformat(), "people": people}

    def calls(self, me, is_mgr, q, month):
        """Раздел «Звонки»: сотрудник видит свои звонки, с правом listen_dept — звонки своего отдела,
        администратор — всех. login — один сотрудник из доступных, без него — все доступные."""
        allowed = call_records.audible_logins(self.config, me)
        if allowed is not None and me["login"] not in allowed:
            allowed = [me["login"]] + allowed  # свои звонки видны в списке, даже если слушать их нельзя
        login = q.get("login") or None
        if login and (not self.user(login) or (allowed is not None and login not in allowed)):
            raise BadRequest("Сотрудник не найден")
        emps = [login] if login else allowed
        deal = None
        if q.get("deal"):
            if not str(q["deal"]).strip().isdigit():
                raise BadRequest("Номер сделки — число (ID сделки в Битрикс24)")
            deal = int(q["deal"])
        first, last = month_bounds(month)
        rows = call_records.listing(self.con, self.config, emps=emps, deal=deal,
                                    date_from=None if deal is not None else first, date_to=None if deal is not None else last,
                                    with_listens=is_mgr)
        names = {e["login"]: e["name"] for e in self.config["employees"]}
        for r in rows:
            r["emp_name"] = names.get(r["emp"], r["emp"])
            r["can_listen"] = call_records.can_listen(self.config, me, r)
        # кого можно выбрать в фильтре: администратору — сотрудников с KPI, руководителю отдела — свой отдел
        choices = [e for e in self.config["employees"] if (e.get("position") if allowed is None else e["login"] in allowed)]
        return {"calls": rows, "login": login, "deal": deal, "month": month,
                "employees": [{"login": e["login"], "name": e["name"]} for e in choices],
                "keep_days": call_records.settings(self.config)["keep_days"],
                "portal": b24_calendar._portal(self.config.get("bitrix24", {})),
                "download": call_records.status() if is_mgr else None}

    def employee(self, login, month):
        r = self.engine.compute(login, month)
        first, last = month_bounds(month)
        for it in r["items"]:
            if it["code"] in manual.ENTRY:
                it["entry"] = manual.entry(self.con, it["code"], login, first, last)
            if it["code"] == "tender_receivables":
                it["penalties"] = manual.penalties_block(self.con, login, month)
        r["trend"] = self.engine.trend(login, month)
        r["weeks"] = weeks_of_month(month)
        return r


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "KPIDashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- ответы
    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            body = f.read()
        if path.endswith(".html"):
            body = PAGE_HEAD + body + b"</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if path.endswith(".html"):  # после обновления дашборда браузер не должен показывать старую страницу
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_call_audio(self, me, record_id):
        """Аудио записи звонка с проверкой прав; каждое прослушивание и каждый отказ — в журнал (listen_log).
        Поддерживает Range: браузер запрашивает файл частями при перемотке."""
        app = self.app
        with app.lock:
            row = call_records.get(app.con, record_id)
            if not row:
                return self.send_json({"error": "Запись не найдена"}, 404)
            if not call_records.can_listen(app.config, me, row):
                call_records.log_listen(app.con, me["login"], row, self.client_ip(), "denied")
                return self.send_json({"error": "Нет прав на прослушивание этой записи"}, 403)
            path = call_records.file_path(app.config, row)
            if not path or not os.path.isfile(path):
                return self.send_json({"error": "Запись ещё не скачана или удалена по сроку хранения"}, 404)
            rng = self.headers.get("Range") or ""
            if not rng or rng.replace(" ", "").startswith("bytes=0-"):  # начало воспроизведения, а не перемотка
                call_records.log_listen(app.con, me["login"], row, self.client_ip())
        size = os.path.getsize(path)
        start, end = 0, size - 1
        if rng:
            m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
            if not m or not (m.group(1) or m.group(2)):
                start = None
            elif m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
            else:  # bytes=-N — последние N байт
                start = max(0, size - int(m.group(2)))
            if start is None or start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, BrokenPipeError):
            pass  # браузер прервал загрузку (перемотка, пауза, закрытие страницы)

    def client_ip(self):
        """Адрес клиента. За обратным прокси (server.trust_proxy) все запросы приходят с 127.0.0.1,
        поэтому адрес берётся из последнего элемента X-Forwarded-For — его дописывает сам прокси."""
        ip = self.client_address[0]
        if self.app.config.get("server", {}).get("trust_proxy") and ip in ("127.0.0.1", "::1"):
            return (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip() or ip
        return ip

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["kpi_session"].value if "kpi_session" in c else None

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise BadRequest("Ожидается JSON")
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise BadRequest("Некорректный JSON")
        if not isinstance(b, dict):
            raise BadRequest("Некорректный JSON")
        return b

    # ---------- проверка параметров
    @staticmethod
    def month_param(v):
        if not isinstance(v, str) or not MONTH_RE.match(v):
            raise BadRequest("Месяц указывается в формате ГГГГ-ММ")
        return v

    @staticmethod
    def number_param(v, lo=0.0, hi=None):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise BadRequest("Значение должно быть числом")
        if not math.isfinite(x) or x < lo or (hi is not None and x > hi):
            raise BadRequest(f"Значение должно быть от {lo:g}" + (f" до {hi:g}" if hi is not None else ""))
        return x

    def employee_param(self, login):
        e = self.app.engine.employee(login) if isinstance(login, str) else None
        if not e or not e.get("position"):
            raise BadRequest("Сотрудник не найден")
        return login

    @staticmethod
    def date_param(v):
        """Необязательная дата ГГГГ-ММ-ДД из строки запроса."""
        if not v:
            return None
        try:
            return dt.date.fromisoformat(v).isoformat()
        except (TypeError, ValueError):
            raise BadRequest("Дата указывается в формате ГГГГ-ММ-ДД")

    @staticmethod
    def week_param(v):
        try:
            d = dt.date.fromisoformat(v)
        except (TypeError, ValueError):
            raise BadRequest("Неделя указывается датой понедельника ГГГГ-ММ-ДД")
        if d.weekday() != 0:
            raise BadRequest("Неделя указывается датой понедельника")
        return v

    # ---------- маршруты
    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            if u.path in ("/", "/index.html"):
                return self.send_file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")
            self.send_response(404)
            self.end_headers()
            return
        self.api("GET", u.path, {k: v[0] for k, v in parse_qs(u.query).items()})

    def do_POST(self):
        u = urlparse(self.path)
        self.api("POST", u.path, {})

    def api(self, method, path, q):
        app = self.app
        try:
            if path == "/api/login" and method == "POST":
                b = self.body()
                login = str(b.get("login") or "").strip().lower()
                key = (self.client_ip(), login)
                with app.lock:
                    if app.login_blocked(key):
                        return self.send_json({"error": "Слишком много неудачных попыток входа. Попробуйте через 15 минут"}, 429)
                    ok = app.check_password(login, str(b.get("password") or ""))
                    if not ok:
                        app.login_failed(key)
                        return self.send_json({"error": "Неверный логин или пароль"}, 401)
                    app.login_fails.pop(key, None)
                    tok = app.new_session(login)
                secure = "; Secure" if app.config.get("server", {}).get("secure_cookie") else ""
                return self.send_json({"ok": True}, cookie=f"kpi_session={tok}; HttpOnly; SameSite=Lax; Path=/{secure}")
            if path == "/api/logout" and method == "POST":
                with db_lock():
                    app.con.execute("DELETE FROM sessions WHERE token=?", (self.token(),))
                    app.con.commit()
                return self.send_json({"ok": True}, cookie="kpi_session=; Max-Age=0; Path=/")

            # соединение с базой одно на все потоки: без блокировки параллельные запросы мешают друг другу
            with app.lock:
                me = app.session_user(self.token())
            if not me:
                return self.send_json({"error": "Требуется вход"}, 401)
            is_mgr = me.get("role") == "manager"
            month = self.month_param(q["month"]) if q.get("month") else current_month()

            if path == "/api/calendar":  # расписание из Битрикс24 на день — запрос в Б24 без общей блокировки базы
                return self.send_json(app.calendar(me, is_mgr, q))
            if path == "/api/call-audio":  # аудио отдаётся долго — без общей блокировки базы
                return self.send_call_audio(me, q.get("id") or "")

            with app.lock:
                if path == "/api/bootstrap":
                    return self.send_json(app.bootstrap(me))
                if path == "/api/overview":
                    if not is_mgr:
                        return self.send_json({"error": "Доступно только руководителю"}, 403)
                    return self.send_json(app.engine.overview(month))
                if path == "/api/funnel":  # воронки сделок и лидов открыты всем, кто вошёл в дашборд
                    kind = q.get("kind") or "deals"
                    if kind not in FUNNEL_KINDS:
                        raise BadRequest("Неизвестная воронка")
                    date_field = q.get("date_field") or "created"
                    if date_field not in FUNNEL_DATE_FIELDS:
                        raise BadRequest("Неизвестная дата для отбора")
                    date_from, date_to = self.date_param(q.get("date_from")), self.date_param(q.get("date_to"))
                    if date_from and date_to and date_from > date_to:
                        raise BadRequest("Начало периода позже его конца")
                    logins = [x for x in (q.get("login") or "").split(",") if x]  # несколько ответственных через запятую
                    return self.send_json(deal_funnel(app.con, app.config, logins, q.get("category"), kind,
                                                      date_from, date_to, date_field))
                if path == "/api/call-funnel":  # воронка звонков ответственных из «Воронок»: сотрудник — только своя
                    date_from, date_to = self.date_param(q.get("date_from")), self.date_param(q.get("date_to"))
                    if not date_from and not date_to:  # период не выбран — текущий месяц, как в KPI
                        first, last = month_bounds(current_month())
                        date_from, date_to = first.isoformat(), last.isoformat()
                    date_from = date_from or "2000-01-01"
                    date_to = date_to or dt.date.today().isoformat()
                    if date_from > date_to:
                        raise BadRequest("Начало периода позже его конца")
                    people = []
                    for key in [k for k in (q.get("login") or "").split(",") if k][:20]:
                        if key.startswith("id:") or not app.engine.employee(key):
                            continue  # ответственный не из дашборда — звонки по нему не выгружаются
                        if not is_mgr and key != me["login"]:
                            people.append({"login": key, "error": "Звонки других сотрудников видит только руководитель"})
                            continue
                        people.append(app.engine.call_funnel(key, dt.date.fromisoformat(date_from),
                                                             dt.date.fromisoformat(date_to)))
                    return self.send_json({"date_from": date_from, "date_to": date_to, "min_call": app.engine.min_call,
                                           "people": people})
                if path == "/api/employee":
                    login = q.get("login") or me["login"]
                    if not is_mgr and login != me["login"]:
                        return self.send_json({"error": "Нет доступа к дашборду другого сотрудника"}, 403)
                    if not app.engine.employee(login) or not app.engine.employee(login).get("position"):
                        return self.send_json({"error": "Сотрудник не найден"}, 404)
                    return self.send_json(app.employee(login, month))
                if path == "/api/calls":  # звонки с записью: сотрудник — свои, администратор — любого или по сделке
                    return self.send_json(app.calls(me, is_mgr, q, month))
                if path == "/api/call-transcript":  # расшифровка — те же права, что на прослушивание, и запись в журнал
                    row = call_records.get(app.con, q.get("id") or "")
                    if not row:
                        return self.send_json({"error": "Запись не найдена"}, 404)
                    if not call_records.can_listen(app.config, me, row):
                        call_records.log_listen(app.con, me["login"], row, self.client_ip(), "denied")
                        return self.send_json({"error": "Нет прав на эту расшифровку"}, 403)
                    tr = transcripts.get(app.con, row["id"])
                    if not tr or not tr.get("engine"):  # нет строки или только неудачные попытки
                        return self.send_json({"error": "Расшифровки ещё нет"}, 404)
                    call_records.log_listen(app.con, me["login"], row, self.client_ip(), "read")
                    return self.send_json({"id": row["id"], "segments": tr["segments"], "duration": tr["duration"]})
                if path == "/api/listen-log":
                    if not is_mgr:
                        return self.send_json({"error": "Журнал прослушиваний доступен только администратору"}, 403)
                    today_ = dt.date.today()
                    date_from = self.date_param(q.get("date_from")) or (today_ - dt.timedelta(days=30)).isoformat()
                    date_to = self.date_param(q.get("date_to")) or today_.isoformat()
                    names = {e["login"]: e["name"] for e in app.config["employees"]}
                    rows = call_records.listen_journal(app.con, date_from, date_to, q.get("login") or None)
                    for r in rows:
                        r["name"], r["emp_name"] = names.get(r["login"], r["login"]), names.get(r["emp"], r["emp"])
                    return self.send_json({"date_from": date_from, "date_to": date_to, "rows": rows})
                if path == "/api/staff":  # раздел «Сотрудники»: только администраторы
                    if not is_mgr:
                        return self.send_json({"error": "Доступно только администратору"}, 403)
                    if method == "POST":
                        b = self.body()
                        try:
                            if b.get("action") == "delete":
                                employees.delete(app.con, str(b.get("login") or ""), me["login"])
                            elif b.get("action") in ("create", "update"):
                                employees.save(app.con, app.rules, b, me["login"], b["action"] == "create", hash_password)
                            else:
                                raise BadRequest("Неизвестное действие")
                        except employees.EmployeeError as e:
                            raise BadRequest(str(e))
                        employees.apply(app.con, app.config, app.rules)
                    return self.send_json(employees.listing(app.con, app.rules))
                if method == "POST" and not is_mgr:
                    return self.send_json({"error": "Изменять данные может только руководитель"}, 403)
                if path == "/api/score" and method == "POST":
                    b = self.body()
                    score = b.get("score")
                    if score is not None:
                        score = self.number_param(score, 0, 100)
                    if b.get("criterion") not in CRITERIA_TITLES:
                        raise BadRequest("Неизвестный критерий оценки")
                    self.employee_param(b.get("emp"))
                    self.week_param(b.get("week"))
                    with db_lock():
                        if score is None:
                            app.con.execute("DELETE FROM manual_scores WHERE emp=? AND week=? AND criterion=?",
                                            (b["emp"], b["week"], b["criterion"]))
                        else:
                            app.con.execute("INSERT OR REPLACE INTO manual_scores VALUES (?,?,?,?,?,?,datetime('now'))",
                                            (b["emp"], b["week"], b["criterion"], score, str(b.get("comment") or "")[:500],
                                             me["login"]))
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/manual" and method == "POST":
                    b = self.body()
                    with db_lock():
                        try:
                            emp = manual.set_value(app.con, b.get("table"), str(b.get("ref") or ""), b.get("field"),
                                                   b.get("value"), me["login"])
                            self.employee_param(emp)  # запись должна относиться к сотруднику из дашборда
                        except (ValueError, KeyError, BadRequest) as e:
                            app.con.rollback()
                            raise BadRequest(f"Не сохранено: {e}")
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/penalty" and method == "POST":
                    b = self.body()
                    emp, month = self.employee_param(b.get("emp")), self.month_param(b.get("month"))
                    with db_lock():
                        try:
                            if b.get("action") == "add":
                                manual.add_penalty(app.con, emp, b.get("dt"), b.get("amount"), b.get("reason"), me["login"])
                            elif b.get("action") == "delete":
                                manual.delete_penalty(app.con, emp, int(b.get("id") or 0))
                            elif b.get("action") == "check":
                                manual.set_penalty_check(app.con, emp, month, bool(b.get("checked")), me["login"])
                            else:
                                raise ValueError("неизвестное действие")
                        except ValueError as e:
                            app.con.rollback()
                            raise BadRequest(f"Не сохранено: {e}")
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/plan" and method == "POST":
                    b = self.body()
                    metric = b.get("metric", "gross_profit")
                    if metric not in PLAN_METRICS:
                        raise BadRequest("Неизвестный показатель плана")
                    row = (self.employee_param(b.get("emp")), self.month_param(b.get("month")), metric,
                           self.number_param(b.get("value")))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?)", row)
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/baseline" and method == "POST":
                    b = self.body()
                    if b.get("metric", "conversion_last_year") != "conversion_last_year":
                        raise BadRequest("Неизвестный показатель")
                    row = (self.employee_param(b.get("emp")), "conversion_last_year", self.number_param(b.get("value"), 0, 1))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", row)
                        app.con.commit()
                    return self.send_json({"ok": True})
            if path == "/api/sync" and method == "POST":
                return self.send_json(sync.start_background(app.con, app.config))
            if path == "/api/sync":  # ход синхронизации; когда закончилась — итог последней
                with app.lock:
                    last = sync.last_sync(app.con)
                return self.send_json({**sync.progress(), "last_sync": last})
            return self.send_json({"error": "Не найдено"}, 404)
        except BadRequest as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            traceback.print_exc()
            return self.send_json({"error": f"Ошибка сервера: {e}"}, 500)


def serve():
    app = App()
    Handler.app = app
    cfg = app.config
    # демо-база, созданная до появления воронок сделок и лидов, пересоздаётся
    demo_outdated = cfg.get("mode", "demo") == "demo" and not app.con.execute(
        "SELECT COUNT(*) FROM crm_deals WHERE entity = 'lead'").fetchone()[0]
    if not sync.last_sync(app.con) or demo_outdated:
        print("Первая синхронизация…")
        print(sync.run(app.con, cfg)["message"])
    if cfg.get("mode", "demo") == "demo":
        have = {r["login"] for r in app.con.execute("SELECT login FROM users")}
        for e in cfg["employees"]:
            if e["login"] not in have:
                set_password(app.con, e["login"], "demo")
        print("Демо-режим: у всех пользователей без пароля установлен пароль demo")
    sync.start_scheduler(app.con, cfg)
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8080))
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Не удалось занять порт {port}: {e}. Возможно, дашборд уже запущен в другом окне "
              f"или порт занят другой программой — поменяйте server.port в config.json.")
        return
    url = f"http://localhost:{port}"
    print(f"Дашборд KPI работает: {url}  (Ctrl+C или закрытие окна — остановить)")
    if cfg.get("server", {}).get("open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()
