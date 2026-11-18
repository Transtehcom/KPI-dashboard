"""Verified bootstrap for the local Bitrix24 application, before bot activation."""
import datetime as dt
import hmac
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .common import db_lock
from .crm_links import portal_origin


INSTALL_PATH = "/bitrix24/ttkgpt/install"
MAIN_PATH = "/bitrix24/ttkgpt/"
APP_INFO_URL = "https://oauth.bitrix24.tech/rest/app.info/"


class _Denied(Exception):
    pass


class _Unavailable(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, token):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode({"auth": token}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=12) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise _Unavailable()
        result = json.loads(body)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (400, 401, 403):
            raise _Denied() from None
        raise _Unavailable() from None
    except (OSError, ValueError):
        raise _Unavailable() from None
    if not isinstance(result, dict) or "error" in result:
        raise _Denied()
    return result.get("result")


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


def _document(message, finish=False):
    script = ""
    if finish:
        script = ('<script src="https://api.bitrix24.tech/api/v1/"></script>'
                  '<script>BX24.init(function(){BX24.installFinish();});</script>')
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>ttkGPT — подключение Битрикс24</title></head><body>"
            "<h1>ttkGPT</h1><p>" + html.escape(message) + "</p>" + script + "</body></html>")


class BitrixApp:
    def __init__(self, con, config):
        self.con = con
        self.config = config
        with db_lock():
            self.con.execute("""CREATE TABLE IF NOT EXISTS bitrix_app_installation (
                id INTEGER PRIMARY KEY CHECK(id=1), client_id TEXT NOT NULL,
                member_id TEXT NOT NULL, portal TEXT NOT NULL, user_id INTEGER NOT NULL,
                access_token TEXT NOT NULL, refresh_token TEXT NOT NULL,
                application_token TEXT NOT NULL, expires_at TEXT NOT NULL,
                scopes TEXT NOT NULL, installed_at TEXT NOT NULL)""")
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

    def page(self, method, path, fields):
        if path not in (INSTALL_PATH, MAIN_PATH):
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
            stored = self._installed()
            result, profile, token, member = self._verify(fields, client_id, origin)
            self._same_installation(stored, client_id, member, origin)
            app_token = _field(fields, "APPLICATION_TOKEN", "application_token")
            if path == MAIN_PATH:
                if not stored:
                    return 409, _document("Завершите первоначальную установку приложения в Битрикс24.")
                if not hmac.compare_digest(app_token.encode(), stored["application_token"].encode()):
                    raise _Denied()
                name = " ".join(str(profile.get(key) or "") for key in ("NAME", "LAST_NAME")).strip()
                return 200, _document(f"Подключён пользователь {name or profile['ID']}. Активация чат-бота ожидается.")
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
