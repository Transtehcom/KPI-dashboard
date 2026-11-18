"""Verified Bitrix24 application OAuth and administrator-initiated bot activation."""
import datetime as dt
import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from .common import db_lock
from .crm_links import portal_origin


INSTALL_PATH = "/bitrix24/ttkgpt/install"
MAIN_PATH = "/bitrix24/ttkgpt/"
ACTIVATE_PATH = "/bitrix24/ttkgpt/activate"
APP_INFO_URL = "https://oauth.bitrix24.tech/rest/app.info/"
TOKEN_URL = "https://oauth.bitrix24.tech/oauth/token/"
_oauth_lock = threading.RLock()


class BitrixAppError(RuntimeError):
    """Public exception whose message contains no remote response or credentials."""

    def __init__(self, message, *, retryable=False, ambiguous=False):
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous


class _Denied(Exception):
    pass


class _Unavailable(Exception):
    retryable = True
    ambiguous = False


class _Transport(_Unavailable):
    retryable = False
    ambiguous = True


class _Expired(_Denied):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url, values, json_body=False, limit=65536):
    body = json.dumps(values).encode() if json_body else urllib.parse.urlencode(values).encode()
    content_type = "application/json" if json_body else "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=12) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise _Transport()
        result = json.loads(body)
    except urllib.error.HTTPError as error:
        try:
            error_data = json.loads(error.read(limit + 1))
        except (OSError, ValueError):
            error_data = {}
        error.close()
        if isinstance(error_data, dict) and error_data.get("error") == "expired_token":
            raise _Expired() from None
        if error.code == 429 or (isinstance(error_data, dict) and error_data.get("error") in
                                ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT")):
            raise _Unavailable() from None
        if 400 <= error.code < 500 and error.code != 408:
            raise _Denied() from None
        raise _Transport() from None
    except (OSError, ValueError):
        raise _Transport() from None
    if isinstance(result, dict) and result.get("error") == "expired_token":
        raise _Expired()
    if isinstance(result, dict) and result.get("error") in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
        raise _Unavailable()
    if not isinstance(result, dict) or "error" in result:
        raise _Denied()
    return result


def _post(url, token):
    return _request(url, {"auth": token}).get("result")


def _field(fields, name, event_name):
    value = fields.get(name) or fields.get(f"auth[{event_name}]")
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _Denied()
    return value


def _user_id(value):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise _Denied()
    identity = int(value)
    if identity > 9223372036854775807:
        raise _Denied()
    return identity


def _document(message, finish=False, content=""):
    script = ""
    if finish:
        script = ('<script src="https://api.bitrix24.tech/api/v1/"></script>'
                  '<script>BX24.init(function(){BX24.installFinish();});</script>')
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>ttkGPT — подключение Битрикс24</title>"
            "<style>body{font:16px/1.6 system-ui,sans-serif;color:#21314b;background:#f3f6fb;"
            "max-width:640px;margin:48px auto;padding:32px}h1{font-size:30px;margin:0 0 20px}"
            "section{background:white;border:1px solid #dce4ef;border-radius:16px;padding:24px;margin-top:24px}"
            "button,a.button{display:inline-block;background:#255bc5;color:white;border:0;border-radius:9px;"
            "padding:12px 20px;font:inherit;text-decoration:none;cursor:pointer}"
            "input[type=password]{font:inherit;width:100%;box-sizing:border-box;padding:10px;"
            "border:1px solid #c6d1e0;border-radius:8px;margin-top:8px}"
            "@media(max-width:640px){body{margin:0;padding:24px}}</style></head><body>"
            "<h1>ttkGPT</h1><p>" + html.escape(message) + "</p>" + content + script + "</body></html>")


class BitrixApp:
    def __init__(self, con, config, bot_factory=None):
        self.con = con
        self.config = config
        self.bot_factory = bot_factory
        with db_lock():
            self.con.execute("""CREATE TABLE IF NOT EXISTS bitrix_app_installation (
                id INTEGER PRIMARY KEY CHECK(id=1), client_id TEXT NOT NULL,
                member_id TEXT NOT NULL, portal TEXT NOT NULL, user_id INTEGER NOT NULL,
                access_token TEXT NOT NULL, refresh_token TEXT NOT NULL,
                application_token TEXT NOT NULL, expires_at TEXT NOT NULL,
                scopes TEXT NOT NULL, installed_at TEXT NOT NULL)""")
            self.con.execute("""CREATE TABLE IF NOT EXISTS bitrix_app_activation_tickets (
                ticket_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, member_id TEXT NOT NULL,
                user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)""")
            self.con.execute("""CREATE TABLE IF NOT EXISTS bitrix_app_credentials (
                client_id TEXT PRIMARY KEY, client_secret TEXT NOT NULL)""")
            self.con.commit()

    def _installed(self):
        with db_lock():
            row = self.con.execute("SELECT * FROM bitrix_app_installation WHERE id=1").fetchone()
        return dict(row) if row is not None else None

    def _verify(self, fields, client_id, origin):
        token = _field(fields, "AUTH_ID", "access_token")
        member = _field(fields, "member_id", "member_id")
        domain = _field(fields, "DOMAIN", "domain")
        if domain.lower() != urllib.parse.urlsplit(origin).netloc:
            raise _Denied()
        result = _post(APP_INFO_URL, token)
        if not isinstance(result, dict) or result.get("client_id") != client_id:
            raise _Denied()
        install = result.get("install")
        if not isinstance(install, dict) or (
            install.get("member_id") != member
            or install.get("domain") != urllib.parse.urlsplit(origin).netloc
            or install.get("client_endpoint") != origin + "/rest/"
        ):
            raise _Denied()
        try:
            expires = dt.datetime.fromisoformat(result["expires"])
            valid = expires.tzinfo is not None and expires > dt.datetime.now(dt.timezone.utc)
        except (KeyError, TypeError, ValueError):
            raise _Denied() from None
        if not valid or not isinstance(install.get("scope"), str):
            raise _Denied()
        user_id = _user_id(result.get("user_id"))
        profile = _post(origin + "/rest/profile.json", token)
        if not isinstance(profile, dict) or _user_id(profile.get("ID")) != user_id:
            raise _Denied()
        return result, profile, token, member

    @staticmethod
    def _same_installation(stored, client_id, member, origin):
        if stored and (stored["client_id"], stored["member_id"], stored["portal"]) != (client_id, member, origin):
            raise _Denied()

    def _client_secret(self, client_id):
        configured = self.config.get("bitrix_app", {}).get("client_secret")
        if configured:
            return configured
        with db_lock():
            row = self.con.execute("SELECT client_secret FROM bitrix_app_credentials WHERE client_id=?", (client_id,)).fetchone()
        return row["client_secret"] if row else None

    def _access_token(self, rejected_token=None):
        with _oauth_lock:
            stored = self._installed()
            origin = portal_origin(self.config)
            client = self.config.get("bitrix_app", {})
            if not stored or not origin or not origin.startswith("https://"):
                raise _Denied()
            self._same_installation(stored, client.get("client_id"), stored["member_id"], origin)
            now = dt.datetime.now(dt.timezone.utc)
            try:
                expires = dt.datetime.fromisoformat(stored["expires_at"])
                fresh = expires > now + dt.timedelta(seconds=30)
            except (TypeError, ValueError):
                fresh = False
            if fresh and stored["access_token"] != rejected_token:
                return stored["access_token"]
            secret = self._client_secret(client["client_id"])
            if not secret or not stored["refresh_token"]:
                raise _Denied()
            renewed = _request(TOKEN_URL, {
                "grant_type": "refresh_token", "client_id": client["client_id"],
                "client_secret": secret, "refresh_token": stored["refresh_token"],
            })
            if (renewed.get("member_id") != stored["member_id"]
                    or renewed.get("client_endpoint") != origin + "/rest/"
                    or renewed.get("client_id", client["client_id"]) != client["client_id"]
                    or _user_id(renewed.get("user_id", stored["user_id"])) != stored["user_id"]):
                raise _Denied()
            token = _field(renewed, "access_token", "access_token")
            refresh = _field(renewed, "refresh_token", "refresh_token")
            try:
                expires = (dt.datetime.fromtimestamp(int(renewed["expires"]), dt.timezone.utc)
                           if "expires" in renewed else now + dt.timedelta(seconds=int(renewed["expires_in"])))
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise _Denied() from None
            if expires <= now:
                raise _Denied()
            scopes = renewed.get("scope", stored["scopes"])
            if not isinstance(scopes, str):
                raise _Denied()
            with db_lock():
                updated = self.con.execute("""UPDATE bitrix_app_installation SET
                    access_token=?,refresh_token=?,expires_at=?,scopes=?
                    WHERE id=1 AND client_id=? AND member_id=? AND access_token=? AND refresh_token=?""",
                    (token, refresh, expires.isoformat(), scopes, stored["client_id"], stored["member_id"],
                     stored["access_token"], stored["refresh_token"]))
                self.con.commit()
                if updated.rowcount != 1:
                    raise _Unavailable()
            return token

    def call(self, method, params=None):
        """Call a REST method with the installed OAuth identity and return its result."""
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", method):
            raise BitrixAppError("Некорректное имя метода Битрикс24.")
        if params is not None and not isinstance(params, dict):
            raise BitrixAppError("Некорректные параметры метода Битрикс24.")
        before_request = True
        try:
            token = self._access_token()
            url = portal_origin(self.config) + "/rest/" + method + ".json"
            values = {**(params or {}), "auth": token}
            before_request = False
            try:
                return _request(url, values, json_body=True, limit=4 * 1024 * 1024).get("result")
            except _Expired:
                before_request = True
                values["auth"] = self._access_token(rejected_token=token)
                before_request = False
                return _request(url, values, json_body=True, limit=4 * 1024 * 1024).get("result")
        except (_Denied, _Unavailable) as error:
            ambiguous = getattr(error, "ambiguous", False)
            retryable = getattr(error, "retryable", False) or (before_request and ambiguous)
            raise BitrixAppError("Не удалось выполнить запрос приложения к Битрикс24.",
                                 retryable=retryable, ambiguous=ambiguous and not before_request) from None
        except (TypeError, ValueError):
            raise BitrixAppError("Не удалось выполнить запрос приложения к Битрикс24.") from None

    def _issue_ticket(self, stored, user_id):
        ticket = secrets.token_urlsafe(32)
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        with db_lock():
            self.con.execute("DELETE FROM bitrix_app_activation_tickets WHERE expires_at<=?", (now,))
            self.con.execute("INSERT INTO bitrix_app_activation_tickets VALUES (?,?,?,?,?)",
                (hashlib.sha256(ticket.encode()).hexdigest(), stored["client_id"], stored["member_id"], user_id, now + 300))
            self.con.commit()
        return ticket

    def _consume_ticket(self, fields, client_id, origin):
        ticket = fields.get("ticket")
        if not isinstance(ticket, str) or not 20 <= len(ticket) <= 256:
            raise _Denied()
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with db_lock():
            row = self.con.execute("SELECT * FROM bitrix_app_activation_tickets WHERE ticket_hash=?", (digest,)).fetchone()
            if not row or row["expires_at"] <= dt.datetime.now(dt.timezone.utc).timestamp():
                raise _Denied()
            stored = self._installed()
            if not stored or row["client_id"] != client_id:
                raise _Denied()
            self._same_installation(stored, client_id, row["member_id"], origin)
            deleted = self.con.execute("DELETE FROM bitrix_app_activation_tickets WHERE ticket_hash=?", (digest,))
            self.con.commit()
            if deleted.rowcount != 1:
                raise _Denied()

    @staticmethod
    def _bot_panel(status, origin, ticket=None, need_secret=False):
        bot_id = status.get("bot_id") if isinstance(status, dict) else None
        if bot_id:
            bot_id = _user_id(bot_id)
            link = html.escape(f"{origin}/online/?IM_DIALOG={bot_id}", quote=True)
            return (f'<section><p>Чат-бот подключён. ID {bot_id}.</p>'
                    f'<a class="button" href="{link}" target="_blank" rel="noopener">Открыть чат</a></section>')
        if ticket:
            password = ("<p>Скопируйте ключ приложения из карточки приложения Битрикс24.</p>"
                        '<label>Ключ приложения (client_secret)<br>'
                        '<input type="password" name="client_secret" autocomplete="new-password" required></label><br><br>'
                        if need_secret else "")
            return ('<section><p>Подключите ttkGPT, чтобы сотрудники могли писать ему в Битрикс24.</p>'
                    f'<form method="post" action="{ACTIVATE_PATH}">'
                    f'<input type="hidden" name="ticket" value="{html.escape(ticket, quote=True)}">'
                    + password + '<button type="submit">Активировать чат-бота</button></form></section>')
        return '<section><p>Активация чат-бота ожидается. Подключение выполняет администратор Битрикс24.</p></section>'

    def _main_page(self, profile, stored, origin):
        name = " ".join(str(profile.get(key) or "") for key in ("NAME", "LAST_NAME")).strip()
        message = f"Подключён пользователь {name or profile['ID']}."
        if not self.bot_factory:
            return 200, _document(message + " Активация чат-бота ожидается.")
        try:
            status = self.bot_factory().status()
        except Exception:
            raise _Unavailable() from None
        ticket = None
        if profile.get("ADMIN") is True and not (status or {}).get("bot_id"):
            ticket = self._issue_ticket(stored, _user_id(profile["ID"]))
        return 200, _document(message, content=self._bot_panel(
            status, origin, ticket, need_secret=not self._client_secret(stored["client_id"])))

    def _activate_page(self, fields, client_id, origin):
        self._consume_ticket(fields, client_id, origin)
        with _oauth_lock:
            supplied = None
            if not self._client_secret(client_id):
                supplied = fields.get("client_secret")
                if not isinstance(supplied, str) or not 1 <= len(supplied.strip()) <= 4096:
                    raise _Denied()
                supplied = supplied.strip()
                with db_lock():
                    self.con.execute("INSERT OR REPLACE INTO bitrix_app_credentials VALUES (?,?)", (client_id, supplied))
                    self.con.commit()
            active_secret = self._client_secret(client_id)
            try:
                self._access_token(rejected_token=self._installed()["access_token"])
            except (_Denied, _Unavailable) as error:
                if supplied is not None or isinstance(error, _Denied):
                    with db_lock():
                        self.con.execute("DELETE FROM bitrix_app_credentials WHERE client_id=? AND client_secret=?", (client_id, active_secret))
                        self.con.commit()
                raise
        try:
            if not self.bot_factory:
                raise _Unavailable()
            status = self.bot_factory().activate()
            panel = self._bot_panel(status, origin)
        except Exception:
            raise _Unavailable() from None
        return 200, _document("Подключение чат-бота выполнено.", content=panel)

    def page(self, method, path, fields):
        if path not in (INSTALL_PATH, MAIN_PATH, ACTIVATE_PATH):
            return 404, _document("Страница не найдена.")
        if method not in ("GET", "POST"):
            return 405, _document("Метод запроса не поддерживается.")
        client_id = self.config.get("bitrix_app", {}).get("client_id")
        origin = portal_origin(self.config)
        if not client_id:
            return 200, _document(
                "Сохраните приложение в Битрикс24, чтобы получить client_id. "
                "Оставьте включённой опцию «Приложение само завершает установку». "
                "Сохраните client_id в настройках сервера и повторно откройте страницу установки."
            )
        if not origin or not origin.startswith("https://"):
            return 503, _document("Для подключения требуется настроенный HTTPS-адрес портала Битрикс24.")
        if method == "GET":
            message = ("Подключение сохранено. Откройте приложение из Битрикс24."
                       if self._installed() else "Откройте страницу установки приложения из Битрикс24.")
            return 200, _document(message)
        try:
            if path == ACTIVATE_PATH:
                return self._activate_page(fields, client_id, origin)
            stored = self._installed()
            result, profile, token, member = self._verify(fields, client_id, origin)
            self._same_installation(stored, client_id, member, origin)
            app_token = _field(fields, "APPLICATION_TOKEN", "application_token")
            if path == MAIN_PATH:
                if not stored:
                    return 409, _document("Завершите первоначальную установку приложения в Битрикс24.")
                if not hmac.compare_digest(app_token.encode(), stored["application_token"].encode()):
                    raise _Denied()
                return self._main_page(profile, stored, origin)
            if profile.get("ADMIN") is not True:
                raise _Denied()
            refresh = fields.get("REFRESH_ID") or fields.get("auth[refresh_token]") or ""
            if not isinstance(refresh, str) or len(refresh) > 4096:
                raise _Denied()
            with db_lock():
                self._same_installation(self._installed(), client_id, member, origin)
                self.con.execute("""INSERT OR REPLACE INTO bitrix_app_installation
                    (id,client_id,member_id,portal,user_id,access_token,refresh_token,
                     application_token,expires_at,scopes,installed_at) VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
                    (client_id, member, origin, _user_id(profile["ID"]), token, refresh, app_token,
                     result["expires"], result["install"]["scope"], dt.datetime.now(dt.timezone.utc).isoformat()))
                self.con.commit()
            return 200, _document("Приложение подключено. Активация чат-бота ожидается.", finish=True)
        except _Denied:
            return 403, _document("Данные подключения не прошли проверку. Повторно откройте приложение из Битрикс24.")
        except _Unavailable:
            return 503, _document("Сервис проверки Битрикс24 временно недоступен. Повторите подключение.")
