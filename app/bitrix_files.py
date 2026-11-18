"""Bounded chat-file downloads using links issued by the owning Bitrix24 bot."""
import http.client
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import gpt_documents, gpt_images
from .crm_links import portal_origin


# Official file/CDN examples are documented in files/how-to-download-files.
CDN_HOSTS = {"cdn-ru.bitrix24.ru", "cdn.bitrix24.com"}


class BitrixFileError(ValueError):
    """Safe attachment error suitable for displaying without signed URLs."""


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        return None
    number = int(value)
    return number if number <= 9223372036854775807 else None


def _safe_name(value, fallback="file"):
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .") or fallback
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "_" + name
    if len(name) > 180:
        suffix = os.path.splitext(name)[1][:20]
        name = name[:180 - len(suffix)] + suffix
    return name


def message_files(data):
    """Extract numeric file references, including canonical Message.params.FILE_ID."""
    if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
        return []
    message = data["message"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    references = {}
    for source in (params, message, data):
        for key in ("FILE_ID", "FILES", "files", "fileIds"):
            items = source.get(key, [])
            if isinstance(items, dict):
                if any(name in items for name in ("id", "ID", "fileId")):
                    items = [items]
                else:
                    items = [dict(value, id=value.get("id", identity)) if isinstance(value, dict) else value
                             for identity, value in items.items()]
            if not isinstance(items, (list, tuple)):
                items = [items]
            for item in items:
                metadata = item if isinstance(item, dict) else {}
                identity = _id(metadata.get("id", metadata.get("ID", metadata.get("fileId"))) if metadata else item)
                if not identity:
                    continue
                reference = references.setdefault(identity, {"id": identity})
                name = metadata.get("name", metadata.get("NAME"))
                if isinstance(name, str) and name:
                    reference["name"] = _safe_name(name)
                size = metadata.get("size", metadata.get("SIZE"))
                if not isinstance(size, bool) and re.fullmatch(r"[0-9]{1,19}", str(size)):
                    reference["size"] = int(size)
    return list(references.values())


def _public_socket(address, timeout, source_address=None):
    answers = socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
        raise BitrixFileError("Недопустимый адрес сервера файлов Битрикс24.")
    for family, kind, protocol, _, sockaddr in answers:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError:
            connection.close()
    raise BitrixFileError("Не удалось подключиться к серверу файлов Битрикс24.")


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_socket


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_url(url, origin):
    try:
        if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\s\x00-\x1f\x7f]", url):
            raise ValueError
        parsed, portal = urllib.parse.urlsplit(url), urllib.parse.urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or re.fullmatch(r"[0-9.]+", parsed.hostname) or ":" in parsed.hostname):
            raise ValueError
        same_portal = parsed.hostname == portal.hostname and (parsed.port or 443) == (portal.port or 443)
        official_cdn = parsed.hostname in CDN_HOSTS and (parsed.port or 443) == 443
        if not same_portal and not official_cdn:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BitrixFileError("Недопустимая ссылка на файл Битрикс24.") from None


def _file_extension(prefix, content_type):
    if prefix.startswith(b"%PDF-"):
        return ".pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return ".webp"
    if prefix.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if prefix.startswith(b"BM"):
        return ".bmp"
    if prefix.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type, "")


class BitrixFiles:
    def __init__(self, api, config):
        self.api, self.config = api, config

    def download(self, bot_id, dialog_id, file_ref):
        try:
            bot_id = _id(bot_id)
            file_id = _id(file_ref.get("id")) if isinstance(file_ref, dict) else None
            origin = portal_origin(self.config)
            if not bot_id or not file_id or not origin or not re.fullmatch(r"(?:chat)?[1-9][0-9]*", str(dialog_id)):
                raise BitrixFileError("Некорректный идентификатор вложения Битрикс24.")
            result = self.api.call("imbot.v2.File.download", {"botId": bot_id, "fileId": file_id})
            url = result.get("downloadUrl") if isinstance(result, dict) else None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _PublicHTTPSHandler())
            for _ in range(4):
                _validate_url(url, origin)
                request = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ttkGPT/1.0)", "Accept": "*/*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7", "Referer": origin + "/",
                })
                try:
                    response = opener.open(request, timeout=15)
                except urllib.error.HTTPError as error:
                    location = error.headers.get("Location")
                    redirect = error.code in (301, 302, 303, 307, 308)
                    error.close()
                    if not redirect or not location:
                        raise BitrixFileError("Не удалось скачать вложение Битрикс24.") from None
                    url = urllib.parse.urljoin(url, location)
                    continue
                with response:
                    content_type = response.headers.get_content_type()
                    if content_type in ("text/html", "application/json", "application/problem+json"):
                        raise BitrixFileError("Битрикс24 вернул ответ без содержимого файла.")
                    name = _safe_name(response.headers.get_filename() or result.get("name")
                                      or result.get("filename") or file_ref.get("name"), f"file_{file_id}")
                    prefix = response.read(64)
                    detected = _file_extension(prefix, content_type)
                    extension = detected or os.path.splitext(name)[1].lower()
                    limit = gpt_images.MAX_BYTES if extension in gpt_images.FORMATS else gpt_documents.MAX_BYTES
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > limit:
                        raise BitrixFileError("Размер вложения превышает допустимый предел.")
                    raw = prefix + response.read(limit + 1 - len(prefix))
                if not raw or len(raw) > limit:
                    raise BitrixFileError("Размер вложения превышает допустимый предел.")
                if not os.path.splitext(name)[1]:
                    name += detected or ".bin"
                return _safe_name(name), raw
            raise BitrixFileError("Слишком много перенаправлений при скачивании вложения.")
        except BitrixFileError:
            raise
        except Exception:
            raise BitrixFileError("Не удалось скачать вложение Битрикс24.") from None
