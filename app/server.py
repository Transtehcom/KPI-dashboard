"""Веб-сервер дашборда: авторизация, API, статика."""
import datetime as dt
import copy
import gzip
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
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .crm_links import employee_url, portal_origin
from . import call_feedback, call_recheck, operations, operations_views
from . import tender_followups, daily_calls

from . import money, onec_money, onec_suppliers, sales_panel, b24_calendar, call_analysis, call_records, employees, manual, metrics, sync, transcripts, workload
from .common import (ROOT, connect, current_month, db_lock, load_config, load_rules, month_add, month_bounds,
                     weeks_of_month)
from .engine import CRITERIA_TITLES, Engine
from .onec import OneC
from .funnel import DATE_FIELDS as FUNNEL_DATE_FIELDS, KINDS as FUNNEL_KINDS, deal_funnel

WEB = os.path.join(ROOT, "web")
PAGE_HEAD = (b'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
             b'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             b'<link rel="icon" type="image/png" href="/assets/favicon.png"><link rel="icon" href="/assets/favicon.ico" sizes="any">'
             b'<link rel="apple-touch-icon" href="/assets/icon-180.png"></head><body>')
ASSET_TYPES = {".woff2": "font/woff2", ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
               ".js": "text/javascript; charset=utf-8"}
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PLAN_METRICS = {"gross_profit", "revenue"}
# защита от подбора пароля: не больше LOGIN_MAX_FAILS неудачных попыток за LOGIN_WINDOW секунд
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW = 15 * 60


class BadRequest(Exception):
    pass


@dataclass
class JsonResponse:
    data: object
    code: int = 200
    cookie: str | None = None


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
        self.money_busy = threading.Lock()  # идёт загрузка остатков и планов платежей из 1С («Деньги»)
        self.suppliers_busy = threading.Lock()  # идёт загрузка закупок из 1С («Поставщики»)

    def gpt_service(self):
        from .gpt_assistant import GPTAssistant
        with self.lock:
            if not hasattr(self,"_gpt"):
                self._gpt=GPTAssistant(self.con,self.config,self.rules)
            return self._gpt

    def processing_jobs(self):
        from .call_jobs import JobQueue
        with self.lock:
            if not hasattr(self, "_call_jobs"):
                self._call_jobs = JobQueue(self.con, self.config)
            return self._call_jobs

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

    def refresh_money(self):
        """Загрузить остатки и планы платежей из 1С в фоне; при ошибке прежние суммы остаются, причина видна на странице."""
        if not self.money_busy.acquire(blocking=False):
            return False

        def work():
            try:
                data = onec_money.fetch(OneC(self.config["onec"], log=lambda *a: None))
                with db_lock():
                    onec_money.store(self.con, data)
                    self.con.commit()
            except Exception as e:  # noqa
                with db_lock():
                    onec_money.store_error(self.con, f"{type(e).__name__}: {e}")
                    self.con.commit()
            finally:
                self.money_busy.release()
        threading.Thread(target=work, daemon=True).start()
        return True

    def refresh_suppliers(self):
        """Загрузить закупки из 1С в фоне; при ошибке прежние данные остаются, причина видна на странице."""
        if not self.suppliers_busy.acquire(blocking=False):
            return False

        def work():
            try:
                lines, quotes = onec_suppliers.fetch_all(OneC(self.config["onec"], log=lambda *a: None))
                with db_lock():
                    onec_suppliers.store(self.con, lines)
                    onec_suppliers.store_quotes(self.con, quotes)
                    self.con.commit()
            except Exception as e:  # noqa
                with db_lock():
                    onec_suppliers.store_error(self.con, f"{type(e).__name__}: {e}")
                    self.con.commit()
            finally:
                self.suppliers_busy.release()
        threading.Thread(target=work, daemon=True).start()
        return True

    def sales_team(self):  # сотрудники отдела ОП для панели «Метрики → Отдел продаж»
        return {r[0]: r[1] for r in self.con.execute("SELECT login, name FROM employees WHERE department = 'ОП' ORDER BY sort")}

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
            "me": {"login": me["login"], "name": me["name"], "url": employee_url(self.config,me["login"]), "role": me.get("role", "employee")},
            "employees": [{"login": e["login"], "name": e["name"], "position": e.get("position"),
                           "position_title": e.get("title") or self.rules["positions"].get(e.get("position"), {}).get("title"),
                           "department_title": self.department_title(e), "url": employee_url(self.config,e["login"])}
                          for e in emps],
            "positions": {k: {"title": v["title"], "department": v["department"], "salary": v["salary"],
                              "kpi_bonus": v["kpi_bonus"], "doc": v.get("doc"),
                              "draft": bool(v.get("draft")), "setup_issues": v.get("setup_issues", []),
                              "items": [{x: i.get(x) for x in ("n", "title", "weight", "period", "target_text", "description")}
                                        for i in Engine.items_for(v, cm)]}  # регламент текущего месяца
                          for k, v in self.rules["positions"].items()},
            "months": [month_add(cm, -k) for k in range(0, 12)],
            "current_month": cm,
            "mode": self.config.get("mode", "demo"),
            "payout_mode": self.rules.get("payout", {}).get("mode", "binary"),
            "criteria_titles": CRITERIA_TITLES,
            "last_sync": last,
            "sync_running": sync.is_running(),
            "operations_enabled": bool(self.config.get('operations',{}).get('enabled')),
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

    def calls(self, me, is_mgr, q, date_from, date_to):
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
        if deal is not None:  # по сделке — все звонки, без периода
            date_from = date_to = None
        rows = call_records.listing(self.con, self.config, emps=emps, deal=deal, date_from=date_from, date_to=date_to,
                                    with_listens=is_mgr, feedback_pending=is_mgr and q.get("feedback") == "pending")
        names = {e["login"]: e["name"] for e in self.config["employees"]}
        feedback = call_feedback.listing(self.con, [r["id"] for r in rows], author=None if is_mgr else me["login"])
        rechecks = call_recheck.listing(self.con, [r["id"] for r in rows], author=None if is_mgr else me["login"])
        for r in rows:
            r["emp_name"] = names.get(r["emp"], r["emp"])
            r["can_listen"] = call_records.can_listen(self.config, me, r)
            r["can_rate"] = not is_mgr and r["emp"] == me["login"] and r["can_listen"] and bool(r["analysis"])
            r["feedback"] = feedback.get(r["id"], [])
            for item in r["feedback"]:
                item["author_name"] = names.get(item["author"], item["author"])
                item["rechecks"] = [review for review in rechecks.get(r["id"], []) if review["author"] == item["author"]]
        # кого можно выбрать в фильтре: администратору — сотрудников с ID в Б24 (их звонки выгружаются), руководителю отдела — свой отдел
        choices = [e for e in self.config["employees"] if (e.get("b24_user_id") if allowed is None else e["login"] in allowed)]
        return {"calls": rows, "login": login, "deal": deal, "date_from": date_from, "date_to": date_to,
                "employees": [{"login": e["login"], "name": e["name"]} for e in choices],
                "keep_from": call_records.keep_from(self.config),
                "portal": b24_calendar._portal(self.config.get("bitrix24", {})),
                "model_options": call_analysis.model_options(is_mgr),
                "feedback_criteria": [{"key": key, "label": label} for key, label in call_feedback.CRITERIA.items()],
                "download": call_records.status() if is_mgr else None}

    def employee(self, login, month):
        return self.cached_report(("employee", login, month), lambda: self.build_employee(login, month))

    def overview(self, month):
        return self.cached_report(("overview", month), lambda: self.engine.overview(month))

    def cached_report(self, key, calculate):
        with db_lock():
            version = (int(time.monotonic() // 30), dt.date.today(), self.con.total_changes,
                       self.con.execute("PRAGMA data_version").fetchone()[0])
            if version != getattr(self, "_report_version", None):
                self._report_cache = {}
                self._report_version = version
            if key not in self._report_cache:
                if len(self._report_cache) >= 64:
                    self._report_cache.clear()
                self._report_cache[key] = calculate()
            return copy.deepcopy(self._report_cache[key])

    def build_employee(self, login, month):
        r = self.engine.compute(login, month)
        first, last = month_bounds(month)
        entry_minimums = {rule["code"]: rule["params"]["threshold"]
                          for rule in self.engine.items_for(self.rules["positions"][r["position"]], month)
                          if rule["code"] in ("deferred_payment", "competitive_purchases")}
        for it in r["items"]:
            if it["code"] == "receivables_overdue" and self.engine.has_1c_receivables():
                pass  # сроки и оплаты пришли из 1С — ручной ввод не нужен
            elif it["code"] in manual.ENTRY:
                it["entry"] = manual.entry(self.con, it["code"], login, first, last,
                                           minimum_amount_novat=entry_minimums.get(it["code"]))
            if it["code"] == "tender_receivables" or it.get("uses_penalties"):
                it["penalties"] = manual.penalties_block(self.con, login, month)
                it["penalties"]["source_known"] = bool(it.get("penalties_source_known"))
        r["trend"] = self.engine.trend(login, month)
        r["weeks"] = weeks_of_month(month)
        return r


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "KPIDashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- ответы
    def encode_body(self, body):
        self.send_header("Vary", "Accept-Encoding")
        if len(body) < 1024:
            return body
        accepted = {}
        for entry in self.headers.get("Accept-Encoding", "").lower().split(","):
            parts = entry.strip().split(";")
            quality = 1.0
            for parameter in parts[1:]:
                if parameter.strip().startswith("q="):
                    try:
                        quality = float(parameter.strip()[2:])
                    except ValueError:
                        quality = 0.0
            accepted[parts[0]] = quality
        if accepted.get("gzip", accepted.get("*", 0)) <= 0:
            return body
        compressed = gzip.compress(body, compresslevel=5, mtime=0)
        if len(compressed) >= len(body):
            return body
        self.send_header("Content-Encoding", "gzip")
        return compressed

    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        body = self.encode_body(body)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            body = f.read()
        if path.endswith(".html"):
            body = PAGE_HEAD + body + b"</body></html>"
        etag = 'W/"' + hashlib.sha256(body).hexdigest() + '"'
        conditional = [tag.strip() for tag in self.headers.get("If-None-Match", "").split(",")]
        if etag in conditional or "*" in conditional:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-cache" if path.endswith(".html") else "public, max-age=86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        body = self.encode_body(body)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if path.endswith(".html"):  # после обновления дашборда браузер не должен показывать старую страницу
            self.send_header("Cache-Control", "no-cache")
        elif os.path.dirname(path).endswith("assets"):
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_call_audio(self, me, record_id):
        """Аудио записи звонка с проверкой прав; каждое прослушивание и каждый отказ — в журнал (listen_log).
        Поддерживает Range: браузер запрашивает файл частями при перемотке."""
        app = self.app
        with app.lock:
            row = call_records.get(app.con, record_id)
            if not row:
                return JsonResponse({"error": "Запись не найдена"}, 404)
            if not call_records.can_listen(app.config, me, row):
                call_records.log_listen(app.con, me["login"], row, self.client_ip(), "denied")
                return JsonResponse({"error": "Нет прав на прослушивание этой записи"}, 403)
            path = call_records.file_path(app.config, row)
            if not path or not os.path.isfile(path):
                return JsonResponse({"error": "Запись ещё не скачана или удалена по сроку хранения"}, 404)
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
        if self.path.split("?", 1)[0] == "/api/gpt-upload" and not 0 < n <= 14000000:
            raise BadRequest("Размер запроса загрузки превышает лимит 10 МБ для изображения")
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
            if u.path.startswith("/assets/"):  # шрифты и значок: только файлы из web/assets с разрешёнными расширениями
                name = os.path.basename(u.path)
                ctype = ASSET_TYPES.get(os.path.splitext(name)[1].lower())
                path = os.path.join(WEB, "assets", name)
                if ctype and os.path.isfile(path):
                    return self.send_file(path, ctype)
            self.send_response(404)
            self.end_headers()
            return
        self.api("GET", u.path, {k: v[0] for k, v in parse_qs(u.query).items()})

    def do_POST(self):
        u = urlparse(self.path)
        self.api("POST", u.path, {k: v[0] for k, v in parse_qs(u.query).items()})

    def operations_response(self, method, path, query, body, me):
        if me.get('role') != 'manager':
            return JsonResponse({'error':'Доступно только руководителю'},403)
        app = self.app
        today = dt.date.today()
        first = self.date_param(query.get('date_from')) or today.isoformat()
        last = self.date_param(query.get('date_to')) or today.isoformat()
        if first > last:
            raise BadRequest('Начало периода позже его конца')
        cutoff = min(dt.datetime.combine(dt.date.fromisoformat(last),dt.time.max),dt.datetime.now())
        with app.lock:
            if path == '/api/operations' and method == 'GET':
                kind = query.get('kind') or 'sales'
                if kind not in ('sales','supply'):
                    raise BadRequest('Неизвестная панель')
                employee = query.get('emp') or 'Все'
                valid_employees = {e['login'] for e in app.config.get('employees',[])}
                if employee != 'Все' and employee not in valid_employees:
                    raise BadRequest('Сотрудник не найден')
                channel = query.get('channel') or 'Все'
                if channel not in ('Все','Тендеры','Прямые'):
                    raise BadRequest('Неизвестный канал')
                revision = tuple(app.con.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0] for table in
                                 ('operations_snapshots','operations_source_history','operations_item_scope','operations_price_approval','operations_deadline_approval'))
                key = (kind,first,last,employee,channel,revision,cutoff.strftime('%Y-%m-%dT%H:%M'))
                cache = getattr(app,'_operations_cache',{})
                if key not in cache:
                    cache = {key:operations_views.build(app.con,app.config,kind,first,last,employee,channel)}
                    app._operations_cache = cache
                return JsonResponse(cache[key])
            identity = str(body.get('request_id') or query.get('request_id') or query.get('id') or '')
            entity = operations.get_entity(app.con,'onec',identity,cutoff) or operations.get_entity(app.con,'bitrix',identity,cutoff)
            if entity is None:
                return JsonResponse({'error':'Документ не найден в выбранном срезе'},404)
            if path == '/api/operations/document' and method == 'GET':
                document = operations_views.document_detail(app.con,app.config,identity,cutoff)
                versions = [row for row in operations.versions(app.con,entity.get('_source') or 'onec',cutoff) if row['id']==identity]
                history = [{'observed_at':row['_observed_at'],'status':row.get('status'),'due':row.get('due'),'owner':row.get('owner')} for row in versions]
                events = [event for event in operations.events(app.con,entity.get('_source') or 'onec',cutoff) if event.get('entity_id')==identity]
                return JsonResponse({'document':document,'history':history,'events':events})
            if path == '/api/operations/deadline-approval' and method == 'POST':
                try:
                    operations_views.approve_deadline(app.con,identity,body.get('due'),body.get('reason'),me['login'])
                except ValueError as exc:
                    raise BadRequest(str(exc)) from None
                app.con.commit()
                return JsonResponse({'ok':True})
            item_id = str(body.get('item_id') or query.get('item_id') or '')
            item = next((row for row in entity.get('items') or [] if row.get('id')==item_id),None)
            if item is None:
                return JsonResponse({'error':'Позиция документа не найдена'},404)
            if path == '/api/operations/prices' and method == 'GET':
                history = operations_views.price_history(app.con,{**item,'request_id':identity},cutoff)
                return JsonResponse({'history':history})
            try:
                if path == '/api/operations/item-scope' and method == 'POST':
                    operations_views.set_item_scope(app.con,identity,item_id,body.get('included'),body.get('reason'),me['login'])
                elif path == '/api/operations/price-approval' and method == 'POST':
                    supplier = str(body.get('supplier_id') or '')
                    if not any(str(offer.get('supplier_id'))==supplier for offer in item.get('offers') or []):
                        # A prior supplier offer may be approved for a new request.
                        candidates = operations_views.price_history(app.con,{**item,'request_id':identity},cutoff).get('offers',[])
                        if not any(str(offer.get('supplier_id'))==supplier for offer in candidates):
                            return JsonResponse({'error':'Предложение поставщика не найдено'},404)
                    operations_views.approve_price(app.con,identity,item_id,supplier,me['login'])
                else:
                    return JsonResponse({'error':'Маршрут не найден'},404)
            except ValueError as exc:
                raise BadRequest(str(exc)) from None
            app.con.commit()
            return JsonResponse({'ok':True})

    def api(self, method, path, q):
        response = self.api_response(method, path, q)
        if response is not None:
            # Socket writes can wait on a slow client. Release database locks first.
            self.send_json(response.data, response.code, response.cookie)

    def api_response(self, method, path, q):
        app = self.app
        try:
            b = self.body() if method == "POST" else {}
            if path == "/api/login" and method == "POST":
                login = str(b.get("login") or "").strip().lower()
                key = (self.client_ip(), login)
                with app.lock:
                    if app.login_blocked(key):
                        return JsonResponse({"error": "Слишком много неудачных попыток входа. Попробуйте через 15 минут"}, 429)
                    ok = app.check_password(login, str(b.get("password") or ""))
                    if not ok:
                        app.login_failed(key)
                        return JsonResponse({"error": "Неверный логин или пароль"}, 401)
                    app.login_fails.pop(key, None)
                    tok = app.new_session(login)
                secure = "; Secure" if app.config.get("server", {}).get("secure_cookie") else ""
                return JsonResponse({"ok": True}, cookie=f"kpi_session={tok}; HttpOnly; SameSite=Lax; Path=/{secure}")
            if path == "/api/logout" and method == "POST":
                with db_lock():
                    app.con.execute("DELETE FROM sessions WHERE token=?", (self.token(),))
                    app.con.commit()
                return JsonResponse({"ok": True}, cookie="kpi_session=; Max-Age=0; Path=/")

            # соединение с базой одно на все потоки: без блокировки параллельные запросы мешают друг другу
            with app.lock:
                me = app.session_user(self.token())
            if not me:
                return JsonResponse({"error": "Требуется вход"}, 401)
            is_mgr = me.get("role") == "manager"
            month = self.month_param(q["month"]) if q.get("month") else current_month()

            if path == '/api/operations' or path.startswith('/api/operations/'):
                return self.operations_response(method,path,q,b,me)

            if path == "/api/calendar":  # расписание из Битрикс24 на день — запрос в Б24 без общей блокировки базы
                return JsonResponse(app.calendar(me, is_mgr, q))
            if path == "/api/call-audio":  # аудио отдаётся долго — без общей блокировки базы
                return self.send_call_audio(me, q.get("id") or "")

            if path in ("/api/gpt-upload", "/api/gpt-document", "/api/gpt-document-delete", "/api/gpt-document-protection"):
                service = app.gpt_service()
                try:
                    if path == "/api/gpt-upload" and method == "POST":
                        return JsonResponse(service.document_upload(me["login"], str(b.get("id") or ""), b.get("name"), b.get("content"), protected=b.get("protected", False)))
                    if path == "/api/gpt-document-protection" and method == "POST":
                        return JsonResponse(service.document_protection(me["login"], str(b.get("id") or ""), str(b.get("document") or ""), b.get("protected")))
                    if path == "/api/gpt-document" and method == "GET":
                        return JsonResponse(service.document_download(me["login"], q.get("id") or "", q.get("document") or ""))
                    if path == "/api/gpt-document-delete" and method == "POST":
                        return JsonResponse(service.document_delete(me["login"], str(b.get("id") or ""), str(b.get("document") or "")))
                except ValueError as exc:
                    raise BadRequest(str(exc))
                return JsonResponse({"error": "Метод недоступен"}, 405)

            with app.lock:
                if path.startswith("/api/gpt-"):
                    service=app.gpt_service()
                    try:
                        if path == "/api/gpt-chats" and method == "GET":
                            return JsonResponse(service.chats(me["login"]))
                        if path == "/api/gpt-chat" and method == "GET":
                            return JsonResponse(service.chat(me["login"],q.get("id") or ""))
                        if path == "/api/gpt-preview" and method == "GET":
                            return JsonResponse(service.preview(me["login"],q.get("id") or "",int(q.get("message") or 0)))
                        if path == "/api/gpt-new" and method == "POST":
                            return JsonResponse(service.create_chat(me["login"]))
                        if path == "/api/gpt-manage" and method == "POST":
                            return JsonResponse(service.manage_chat(me["login"], str(b.get("id") or ""), b.get("action"), b.get("value")))
                        if path == "/api/gpt-send" and method == "POST":
                            return JsonResponse(service.submit(me["login"],str(b.get("id") or ""),b.get("text"),
                                b.get("model") or "gpt-6-luna",b.get("effort") or "medium",b.get("month"),fast=b.get("fast",False)))
                        if path == "/api/gpt-cancel" and method == "POST":
                            service.cancel(me["login"],str(b.get("id") or ""))
                            return JsonResponse({"ok":True})
                    except ValueError as exc:
                        raise BadRequest(str(exc))
                    return JsonResponse({"error":"Маршрут GPT не найден"},404)
                if path == "/api/bootstrap":
                    return JsonResponse(app.bootstrap(me))
                if path == "/api/overview":
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    return JsonResponse(app.overview(month))
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
                    # отделы через запятую; «_none» — ответственные без отдела (нет в разделе «Сотрудники»)
                    deps = [("" if x == "_none" else x) for x in q["department"].split(",") if x] if q.get("department") else None
                    mode = q.get("mode") or "date"
                    if mode not in ("date", "fact"):
                        raise BadRequest("Неизвестный вид воронок")
                    from .crm_directions import selected_directions
                    try:
                        directions = selected_directions(q.get('directions'))
                    except ValueError as exc:
                        raise BadRequest(str(exc)) from None
                    return JsonResponse(deal_funnel(app.con, app.config, logins, q.get("category"), kind,
                                                      date_from, date_to, date_field, deps, mode, directions))
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
                    return JsonResponse({"date_from": date_from, "date_to": date_to, "min_call": app.engine.min_call,
                                           "people": people})
                if path == "/api/metrics":  # «Метрики»: где сейчас находится работа отдела — только руководителю
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    if q.get("kind") not in ("supply", "logistics", "sales"):
                        raise BadRequest("Неизвестный раздел метрик")
                    today_ = dt.date.today()
                    date_from = self.date_param(q.get("date_from")) or today_.replace(day=1).isoformat()
                    date_to = self.date_param(q.get("date_to")) or today_.isoformat()
                    if date_from > date_to:
                        raise BadRequest("Начало периода позже его конца")
                    names = {e["login"]: e["name"] for e in app.config["employees"]}
                    if q.get("kind") == "sales":
                        return JsonResponse(sales_panel.build(app.con, app.sales_team(), channel=q.get("channel") or "Все",
                                                                emp=q.get("emp") or "Все",
                                                                portal=portal_origin(app.config)))
                    if q.get("kind") == "logistics":
                        # очередь — по заявкам окна синхронизации: более старые строки в базе не обновляются
                        since = month_bounds(month_add(current_month(), -app.config.get("sync", {}).get("months_back", 6)))[0]
                        return JsonResponse(metrics.logistics(app.con, names, date_from, date_to, since.isoformat(), today_))
                    return JsonResponse(metrics.supply(app.con, names, date_from, date_to, today=today_,
                                                         params=app.config.get("metrics", {}).get("supply")))
                if path == "/api/kpi-records" and method == "GET":
                    login = q.get("login") or me["login"]
                    if not is_mgr and login != me["login"]:
                        return JsonResponse({"error": "Нет доступа к дашборду другого сотрудника"}, 403)
                    if not app.engine.employee(login) or not app.engine.employee(login).get("position"):
                        return JsonResponse({"error": "Сотрудник не найден"}, 404)
                    try:
                        return JsonResponse(app.engine.activity_audit(login, month, q.get("code"), q.get("kind")))
                    except ValueError as e:
                        raise BadRequest(str(e))
                if path == "/api/employee":
                    login = q.get("login") or me["login"]
                    if not is_mgr and login != me["login"]:
                        return JsonResponse({"error": "Нет доступа к дашборду другого сотрудника"}, 403)
                    if not app.engine.employee(login) or not app.engine.employee(login).get("position"):
                        return JsonResponse({"error": "Сотрудник не найден"}, 404)
                    return JsonResponse(app.employee(login, month))
                if path == "/api/calls":  # звонки с записью: сотрудник — свои, администратор — любого или по сделке
                    # период «с … по …» (ГГГГ-ММ-ДД, любая граница может быть пустой); all=1 — всё время; без них — месяц
                    date_from, date_to = self.date_param(q.get("date_from")), self.date_param(q.get("date_to"))
                    if not (date_from or date_to) and not q.get("all"):
                        first, last = month_bounds(month)
                        date_from, date_to = first.isoformat(), last.isoformat()
                    if date_from and date_to and date_from > date_to:
                        raise BadRequest("Начало периода позже его конца")
                    return JsonResponse(app.calls(me, is_mgr, q, date_from, date_to))
                if path == "/api/calls-job":  # ход ручной обработки звонков (кнопка «Обработать…» в разделе «Звонки»)
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    return JsonResponse({**app.processing_jobs().listing(), "busy": call_records.status()["running"],
                                         "auto": daily_calls.get_settings(app.con)})
                if path == "/api/calls-auto" and method == "GET":
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только администратору"}, 403)
                    return JsonResponse(daily_calls.get_settings(app.con))
                if path == "/api/call-transcript":  # расшифровка — те же права, что на прослушивание, и запись в журнал
                    row = call_records.get(app.con, q.get("id") or "")
                    if not row:
                        return JsonResponse({"error": "Запись не найдена"}, 404)
                    if not call_records.can_listen(app.config, me, row):
                        call_records.log_listen(app.con, me["login"], row, self.client_ip(), "denied")
                        return JsonResponse({"error": "Нет прав на эту расшифровку"}, 403)
                    tr = transcripts.get(app.con, row["id"])
                    if not tr or not tr.get("engine"):  # нет строки или только неудачные попытки
                        return JsonResponse({"error": "Расшифровки ещё нет"}, 404)
                    call_records.log_listen(app.con, me["login"], row, self.client_ip(), "read")
                    an = call_analysis.get(app.con, row["id"])
                    sents = call_analysis.sentences(tr["segments"])
                    roles = an.pop("roles", "") if an else ""
                    if len(roles) != len(sents):  # разбор сделан по другой расшифровке — роли не подходят
                        roles = ""
                    return JsonResponse({"id": row["id"], "segments": tr["segments"], "duration": tr["duration"],
                                           "sentences": [[t, roles[i].replace("-", "") if roles else "", s]
                                                         for i, (t, s) in enumerate(sents)],
                                           "analysis": an})
                if path == "/api/listen-log":
                    if not is_mgr:
                        return JsonResponse({"error": "Журнал прослушиваний доступен только администратору"}, 403)
                    today_ = dt.date.today()
                    date_from = self.date_param(q.get("date_from")) or (today_ - dt.timedelta(days=30)).isoformat()
                    date_to = self.date_param(q.get("date_to")) or today_.isoformat()
                    names = {e["login"]: e["name"] for e in app.config["employees"]}
                    rows = call_records.listen_journal(app.con, date_from, date_to, q.get("login") or None, config=app.config)
                    for r in rows:
                        r["name"], r["emp_name"] = names.get(r["login"], r["login"]), names.get(r["emp"], r["emp"])
                    return JsonResponse({"date_from": date_from, "date_to": date_to, "rows": rows})
                if path == "/api/staff":
                    if not is_mgr:
                        if method != "GET":
                            return JsonResponse({"error": "Доступно только администратору"}, 403)
                        return JsonResponse(employees.directory(app.con, app.rules, app.config))
                    if method == "POST":
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
                    listing = employees.listing(app.con, app.rules)
                    for employee in listing["employees"]:
                        employee["url"] = employee_url(app.config, employee["login"])
                    return JsonResponse(listing)
                if path == "/api/call-feedback" and method == "POST":
                    row = call_records.get(app.con, str(b.get("id") or ""))
                    if not row:
                        return JsonResponse({"error": "Запись не найдена"}, 404)
                    if is_mgr or row["emp"] != me["login"] or not call_records.can_listen(app.config, me, row):
                        return JsonResponse({"error": "Оценивать можно только разбор своих звонков с доступом к прослушиванию"}, 403)
                    if not app.con.execute("SELECT 1 FROM call_analysis WHERE id=? AND result IS NOT NULL", (row["id"],)).fetchone():
                        raise BadRequest("Дождитесь разбора звонка перед оценкой")
                    try:
                        return JsonResponse({"feedback": call_feedback.save(app.con, row["id"], me["login"], b)})
                    except ValueError as e:
                        raise BadRequest(str(e))
                if method == "POST" and not is_mgr:
                    return JsonResponse({"error": "Изменять данные может только руководитель"}, 403)
                if path == "/api/calls-auto" and method == "POST":
                    try:
                        return JsonResponse(daily_calls.set_settings(app.con, b, me["login"]))
                    except daily_calls.SettingsConflict as exc:
                        return JsonResponse({"error": str(exc)}, 409)
                    except ValueError as exc:
                        raise BadRequest(str(exc))
                if path == "/api/tender-followup-moves" and method == "POST":
                    with db_lock():
                        try:
                            tender_followups.set_moves(app.con, str(b.get("id") or ""), b.get("value"), me["login"])
                            app.con.commit()
                        except ValueError as exc:
                            app.con.rollback()
                            raise BadRequest(str(exc))
                    return JsonResponse({"ok": True})
                if path == "/api/client-approval" and method == "POST":
                    emp = self.employee_param(b.get("emp"))
                    month = self.month_param(b.get("month"))
                    if app.engine.employee(emp).get("position") != "tender_lead":
                        raise BadRequest("Согласование активных клиентов доступно для ведущего тендерного специалиста")
                    try:
                        manual.set_client_approval(app.con,emp,month,str(b.get("ref") or ""),b.get("value"),me["login"])
                        app.con.commit()
                    except ValueError as e:
                        raise BadRequest(str(e))
                    return JsonResponse({"ok":True})
                if path == "/api/lead-approval" and method == "POST":
                    emp = self.employee_param(b.get("emp"))
                    month = self.month_param(b.get("month"))
                    position = app.rules["positions"].get(app.engine.employee(emp).get("position"), {})
                    if not position or not any(i["code"] == "qualified_leads" for i in app.engine.items_for(position,month)):
                        raise BadRequest("Согласование доступно для KPI качества квалифицированных лидов")
                    try:
                        manual.set_lead_approval(app.con,emp,month,str(b.get("ref") or ""),b.get("value"),me["login"])
                        app.con.commit()
                    except ValueError as e:
                        raise BadRequest(str(e))
                    return JsonResponse({"ok":True})
                if path == "/api/call-feedback-reprocess" and method == "POST":
                    record = str(b.get("id") or "")
                    if not call_records.get(app.con, record):
                        return JsonResponse({"error": "Звонок не найден"}, 404)
                    if type(b.get("revision")) is not int:
                        raise BadRequest("Укажите версию замечания")
                    feedback = call_feedback.get(app.con, record, str(b.get("author") or ""))
                    if not feedback:
                        return JsonResponse({"error": "Замечание не найдено"}, 404)
                    if feedback["revision"] != b["revision"]:
                        return JsonResponse({"error": "Сотрудник изменил замечание; обновите список"}, 409)
                    try:
                        job = app.processing_jobs().create([record], force=True, feedback=feedback, author=me["login"],
                            model=b.get("model") or call_analysis.MODEL, effort=b.get("effort") or call_analysis.EFFORT,
                            run_at=b.get("run_at"), fast=b.get("fast",False))
                    except call_feedback.FeedbackConflict as exc:
                        return JsonResponse({"error": str(exc)}, 409)
                    except ValueError as exc:
                        raise BadRequest(str(exc))
                    return JsonResponse({"ok": True, "job": job, "message": "Создана двухэтапная проверка замечания"})
                if path == "/api/call-feedback-review" and method == "POST":
                    try:
                        result = call_feedback.resolve(app.con, str(b.get("id") or ""), str(b.get("author") or ""),
                                                       b.get("revision"), b.get("note", ""), me["login"])
                        return JsonResponse({"feedback": result})
                    except call_feedback.FeedbackConflict as e:
                        return JsonResponse({"error": str(e)}, 409)
                    except ValueError as e:
                        raise BadRequest(str(e))
                if path == "/api/month-score" and method == "POST":
                    emp, month = self.employee_param(b.get("emp")), self.month_param(b.get("month"))
                    if app.engine.employee(emp).get("position") != "supply":
                        raise BadRequest("Оценка за месяц доступна для отдела снабжения")
                    try:
                        with db_lock():
                            manual.set_month_score(app.con,emp,month,b.get("criterion"),b.get("score"),b.get("comment"),me["login"])
                            app.con.commit()
                    except (ValueError,TypeError) as exc:
                        raise BadRequest(str(exc)) from None
                    return JsonResponse({"ok":True})
                if path == "/api/score" and method == "POST":
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
                    return JsonResponse({"ok": True})
                if path == "/api/manual" and method == "POST":
                    with db_lock():
                        try:
                            emp = manual.set_value(app.con, b.get("table"), str(b.get("ref") or ""), b.get("field"),
                                                   b.get("value"), me["login"])
                            self.employee_param(emp)  # запись должна относиться к сотруднику из дашборда
                        except (ValueError, KeyError, BadRequest) as e:
                            app.con.rollback()
                            raise BadRequest(f"Не сохранено: {e}")
                        app.con.commit()
                    return JsonResponse({"ok": True})
                if path == "/api/penalty" and method == "POST":
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
                    return JsonResponse({"ok": True})
                if path == "/api/plan" and method == "POST":
                    metric = b.get("metric", "gross_profit")
                    if metric not in PLAN_METRICS:
                        raise BadRequest("Неизвестный показатель плана")
                    row = (self.employee_param(b.get("emp")), self.month_param(b.get("month")), metric,
                           self.number_param(b.get("value")))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?)", row)
                        app.con.commit()
                    return JsonResponse({"ok": True})
                if path == "/api/suppliers" and method == "GET":  # вкладка «Поставщики»: закупки и цены из 1С — только руководителю
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    today_ = dt.date.today()
                    date_from = self.date_param(q.get("date_from")) or today_.replace(month=1, day=1).isoformat()
                    date_to = self.date_param(q.get("date_to")) or today_.isoformat()
                    if q.get("refresh") or onec_suppliers.is_stale(app.con):  # открытие вкладки обновляет данные из 1С в фоне, если им больше часа
                        app.refresh_suppliers()
                    res = {"period": [date_from, date_to], "meta": onec_suppliers.meta(app.con), "refreshing": app.suppliers_busy.locked()}
                    if q.get("mode") == "requests":  # «Заявки поставщикам»: предложения из заявок в отдел снабжения
                        res["mode"] = "requests"
                        key = onec_suppliers.quote_find_partner(app.con, q.get("partner"), q.get("inn"))
                        if (q.get("partner") or q.get("inn")) and not key:
                            res["not_found"] = True
                        if key:
                            res.update(onec_suppliers.build_quotes(app.con, key, date_from, date_to, q.get("item") or None))
                        else:
                            res["list"] = onec_suppliers.quote_suppliers(app.con, date_from, date_to, q.get("not_bought") == "1", q.get("search"))
                        return JsonResponse(res)
                    key = onec_suppliers.find_partner(app.con, q.get("partner"), q.get("inn"))
                    if (q.get("partner") or q.get("inn")) and not key:
                        res["not_found"] = True  # поставщика нет в закупках 1С: страница покажет список, а не пустую карточку
                    if key:
                        res.update(onec_suppliers.build(app.con, key, date_from, date_to, q.get("item") or None))
                    else:
                        res["list"] = onec_suppliers.suppliers(app.con, date_from, date_to, q.get("search"))
                    return JsonResponse(res)
                if path == "/api/workload" and method == "GET":  # Temporarily admin-only.
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только администратору"},403)
                    portal = urlparse(portal_origin(app.config) or "").netloc
                    return JsonResponse(workload.build(app.con, app.config, uids=None,
                                                         portal=portal or None, dept_titles=app.rules.get("departments")))
                if path == "/api/money" and method == "GET":  # вкладка «Деньги» — только руководителю
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    st = money.settings(app.con)
                    stale = not st["synced_at"] or dt.datetime.fromisoformat(st["synced_at"]) < dt.datetime.now() - dt.timedelta(minutes=15)
                    if stale:  # открытие вкладки обновляет данные из 1С в фоне, если им больше 15 минут
                        app.refresh_money()
                    try:
                        data = money.build(app.con, date_from=self.date_param(q.get("date_from")),
                                           date_to=self.date_param(q.get("date_to")))
                    except ValueError as e:
                        raise BadRequest(str(e))
                    return JsonResponse({**data, "refreshing": app.money_busy.locked()})
                if path == "/api/money" and method == "POST":
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    with db_lock():
                        try:
                            act = b.get("action")
                            if act == "settings":
                                money.save_settings(app.con, b)
                            elif act == "item":
                                money.save_item(app.con, b, me["login"])
                            elif act == "delete":
                                money.delete_item(app.con, b.get("id"))
                            elif act == "override":
                                money.save_override(app.con, b.get("id"), b.get("plan"), b.get("approval"))
                            elif act == "refresh":
                                app.refresh_money()
                            elif act == "expect":
                                money.save_expect(app.con, b.get("id"), b.get("expected"))
                            else:
                                raise ValueError("неизвестное действие")
                        except (ValueError, KeyError, TypeError) as e:
                            app.con.rollback()
                            raise BadRequest(f"Не сохранено: {e}")
                        app.con.commit()
                    return JsonResponse({"ok": True})
                if path in ("/api/sales-object", "/api/sales-action") and method == "POST":  # реестр и действия панели ОП
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    obj = path == "/api/sales-object"
                    with db_lock():
                        try:
                            if b.get("action") == "delete":
                                (sales_panel.delete_object if obj else sales_panel.delete_action)(app.con, str(b.get("id") or ""))
                            elif obj:
                                sales_panel.save_object(app.con, b, app.sales_team(), me["login"])
                            else:
                                sales_panel.save_action(app.con, b, me["login"])
                        except (ValueError, KeyError) as e:
                            app.con.rollback()
                            raise BadRequest(f"Не сохранено: {e}")
                        app.con.commit()
                    return JsonResponse({"ok": True})
                if path == "/api/calls-job-cancel" and method == "POST":
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    try:
                        row = app.processing_jobs().cancel(str(b.get("id") or ""))
                    except ValueError as exc:
                        raise BadRequest(str(exc))
                    return JsonResponse({"ok": True, "job": row})
                if path == "/api/calls-process" and method == "POST":  # обработать звонок / «всё» / последние N из текущего списка
                    if not is_mgr:
                        return JsonResponse({"error": "Доступно только руководителю"}, 403)
                    if "reprocess" in b and type(b["reprocess"]) is not bool:
                        raise BadRequest("Выберите, обрабатывать ли готовые разборы повторно")
                    batch = not bool(b.get("id"))
                    skip_processed = False
                    if b.get("id"):
                        if not call_records.get(app.con, str(b["id"])):
                            return JsonResponse({"error": "Звонок не найден"}, 404)
                        ids, force = [str(b["id"])], True  # один звонок — всегда заново
                    elif b.get("ids"):
                        if not isinstance(b["ids"], list) or len(b["ids"]) > 10000:
                            raise BadRequest("Выберите от 1 до 10 000 звонков")
                        ids = list(dict.fromkeys(str(i) for i in b["ids"]))
                        if not all(call_records.get(app.con, i) for i in ids):
                            return JsonResponse({"error": "Звонок не найден"}, 404)
                        force = True
                    else:
                        mode = b.get("mode")
                        if mode not in ("all", "last", "range"):
                            raise BadRequest("Ожидается mode: all, last или range")
                        date_from, date_to = self.date_param(q.get("date_from")), self.date_param(q.get("date_to"))
                        if mode == "range":  # любой период из меню, независимо от периода и сделки на странице
                            date_from, date_to = self.date_param(b.get("date_from")), self.date_param(b.get("date_to"))
                            if not (date_from and date_to):
                                raise BadRequest("Укажите период")
                            q = {**q, "deal": None}
                        elif not (date_from or date_to) and not q.get("all"):
                            first, last = month_bounds(month)
                            date_from, date_to = first.isoformat(), last.isoformat()
                        with app.lock:
                            rows = app.calls(me, is_mgr, q, date_from, date_to)["calls"]
                        rows = [r for r in rows if r["audio"] == "ready"]  # от новых к старым
                        if mode == "last":
                            n = int(b.get("n") or 0)
                            if n not in (20, 50, 100):
                                raise BadRequest("Ожидается 20, 50 или 100")
                            rows = rows[:n]
                        ids, force = [r["id"] for r in rows], False
                    model, effort = b.get("model") or call_analysis.MODEL, b.get("effort") or call_analysis.EFFORT
                    try:
                        call_analysis.validate_selection(model, effort)
                        call_analysis.validate_fast(b.get("fast", False))
                    except ValueError as exc:
                        raise BadRequest(str(exc)) from None
                    if batch:
                        with app.lock:
                            pending = call_analysis.todo(app.con, ids, accept_old=True)
                        processed = len(ids) - len(pending)
                        if processed and "reprocess" not in b:
                            return JsonResponse({"needs_choice": True, "ids": ids, "total": len(ids),
                                                 "processed": processed, "pending": len(pending)})
                        force = b.get("reprocess", False)
                        skip_processed = not force
                        if skip_processed:
                            ids = list(pending)
                        if not ids:
                            return JsonResponse({"ok": True, "job": None, "message": "Выбранные звонки уже обработаны"})
                    try:
                        row = app.processing_jobs().create(ids, force, model, effort,
                                                           run_at=b.get("run_at"), author=me["login"], fast=b.get("fast",False),
                                                           skip_processed=skip_processed)
                    except ValueError as exc:
                        raise BadRequest(str(exc))
                    return JsonResponse({"ok": True, "job": row, "message": "Задача создана: " + str(row["total"]) + " звонков"})
                if path == "/api/call-secretary" and method == "POST":  # отметка руководителя «секретарь» к звонку
                    if not call_records.get(app.con, str(b.get("id") or "")):
                        return JsonResponse({"error": "Звонок не найден"}, 404)
                    v = b.get("value")
                    if v not in (None, 0, 1, True, False):
                        raise BadRequest("Ожидается 1, 0 или null")
                    call_analysis.set_secretary(app.con, b["id"], None if v is None else int(v), me["login"])
                    return JsonResponse({"ok": True})
                if path == "/api/baseline" and method == "POST":
                    if b.get("metric", "conversion_last_year") != "conversion_last_year":
                        raise BadRequest("Неизвестный показатель")
                    row = (self.employee_param(b.get("emp")), "conversion_last_year", self.number_param(b.get("value"), 0, 1))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", row)
                        app.con.commit()
                    return JsonResponse({"ok": True})
            if path == "/api/sync" and method == "POST":
                return JsonResponse(sync.start_background(app.con, app.config))
            if path == "/api/sync":  # ход синхронизации; когда закончилась — итог последней
                with app.lock:
                    last = sync.last_sync(app.con)
                return JsonResponse({**sync.progress(), "last_sync": last})
            return JsonResponse({"error": "Не найдено"}, 404)
        except BadRequest as e:
            return JsonResponse({"error": str(e)}, 400)
        except Exception as e:  # noqa
            traceback.print_exc()
            return JsonResponse({"error": f"Ошибка сервера: {e}"}, 500)


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
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8080))
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Не удалось занять порт {port}: {e}. Возможно, дашборд уже запущен в другом окне "
              f"или порт занят другой программой — поменяйте server.port в config.json.")
        return
    app.gpt_service()  # восстановить прерванные чаты только после успешной привязки порта
    app.processing_jobs().start()
    sync.start_scheduler(app.con, cfg)  # только процесс, успешно занявший порт, запускает автообновления
    url = f"http://localhost:{port}"
    print(f"Дашборд KPI работает: {url}  (Ctrl+C или закрытие окна — остановить)")
    if cfg.get("server", {}).get("open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()
