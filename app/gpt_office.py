"""Bounded local Office conversion without uploading document contents."""
import ctypes
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import zipfile
from xml.etree import ElementTree
from pathlib import Path, PurePath
from html.parser import HTMLParser


MAX_BYTES = 3 * 1024 * 1024
CONVERSION_TIMEOUT = 90
NATIVE = {
    ".doc": ".docx", ".docx": ".docx", ".rtf": ".docx", ".odt": ".docx",
    ".html": ".docx", ".htm": ".docx",
    ".xls": ".xlsx", ".xlsx": ".xlsx", ".ods": ".xlsx",
    ".ppt": ".pptx", ".pptx": ".pptx", ".odp": ".pptx",
}
NORMALIZED_FORMATS = NATIVE
APPLICATIONS = {".docx": "Word", ".xlsx": "Excel", ".pptx": "PowerPoint"}
EXECUTABLES = {"Word": "WINWORD.EXE", "Excel": "EXCEL.EXE", "PowerPoint": "POWERPNT.EXE"}
_office_lock = threading.Lock()


class _HTMLText(HTMLParser):
    """Extract visible HTML text without resolving any resource URLs."""
    HIDDEN = {"script", "style", "head", "noscript", "template"}
    BLOCKS = {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []
        self.characters = 0

    def add(self, text):
        self.characters += len(text)
        if self.characters > 250000:
            raise ValueError("HTML превышает лимит 250 000 символов")
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden.append(tag)
        if not self.hidden and tag in self.BLOCKS:
            self.add("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self.BLOCKS:
            self.add("\n")
        elif tag in {"td", "th"}:
            self.add("\t")

    def handle_data(self, data):
        if not self.hidden:
            self.add(re.sub(r"\s+", " ", data))


def _html_docx(raw, max_bytes):
    from docx import Document
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    if "\x00" in text:
        raise ValueError("HTML не является текстовым файлом")
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    document = Document()
    for paragraph in "".join(parser.parts).splitlines():
        if paragraph.strip():
            document.add_paragraph(paragraph.strip())
    output = io.BytesIO()
    document.save(output)
    result = output.getvalue()
    if len(result) > max_bytes:
        raise ValueError("Преобразованный документ превышает допустимый размер файла")
    return result


def discover():
    """Return installed executables, without starting an Office application."""
    found = {}
    if os.name == "nt":
        import winreg
        for application, executable in EXECUTABLES.items():
            key_name = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                            path = winreg.QueryValueEx(key, "")[0].strip('"')
                        if Path(path).is_file():
                            found[application] = path
                    except OSError:
                        continue
    libreoffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not libreoffice and os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / "LibreOffice" / "program" / "soffice.exe"
                if candidate.is_file():
                    libreoffice = str(candidate)
                    break
    if libreoffice:
        found["LibreOffice"] = libreoffice
    return found


def _validate_input(raw, suffix, max_bytes):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
        raise ValueError("Пустой документ или превышен допустимый размер файла")
    if suffix in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 1500 or sum(entry.file_size for entry in entries) > 30 * 1024 * 1024:
                    raise ValueError("Распакованный документ слишком велик")
                if any(entry.flag_bits & 1 for entry in entries):
                    raise ValueError("Зашифрованные документы не поддерживаются")
        except zipfile.BadZipFile:
            raise ValueError("Файл не является корректным офисным документом") from None


def normalize(name, raw, max_bytes=MAX_BYTES):
    """Normalize legacy and OpenDocument files to editable DOCX, XLSX or PPTX."""
    suffix = PurePath(name).suffix.lower()
    if suffix not in NATIVE:
        raise ValueError("Неподдерживаемый офисный формат")
    native = NATIVE[suffix]
    if suffix == native:
        _validate_input(raw, suffix, max_bytes)
        return name, raw
    return str(PurePath(name).with_suffix(native)), convert(name, raw, native, max_bytes)


def _run(command):
    """Kill only this worker and its direct process tree on timeout."""
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, **options) as process:
        try:
            return process.wait(timeout=CONVERSION_TIMEOUT)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
            process.wait(timeout=10)
            raise ValueError(f"Преобразование документа превысило лимит {CONVERSION_TIMEOUT} секунд") from None


def _cleanup_office(owner):
    """A COM server can outlive its worker. Check its recorded birth time before killing."""
    if os.name != "nt" or not owner.is_file():
        return
    try:
        record = json.loads(owner.read_text(encoding="utf-8-sig"))
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000 | 0x0001, False, int(record["pid"]))
        if not handle:
            return
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                if created == int(record["created"]):
                    kernel.TerminateProcess(handle, 1)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _office(source, output, owner, application):
    command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Sta",
               "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("gpt_office_worker.ps1")),
               "-Source", str(source), "-Output", str(output), "-Owner", str(owner),
               "-Application", application]
    with _office_lock:
        try:
            return _run(command)
        finally:
            _cleanup_office(owner)


def _conversion_copy(raw, suffix, directory):
    """Disable automatic external resources in the temporary OOXML conversion copy."""
    if suffix not in {".docx", ".xlsx", ".pptx"}:
        return raw
    output = io.BytesIO()
    disabled_target = (directory / "external-resource-disabled").as_uri()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as result:
            for entry in source.infolist():
                content = source.read(entry)
                if entry.filename.endswith(".rels"):
                    root = ElementTree.fromstring(content)
                    changed = False
                    for relationship in root:
                        if relationship.get("TargetMode") == "External" and not relationship.get("Type", "").endswith("/hyperlink"):
                            relationship.set("Target", disabled_target)
                            changed = True
                    if changed:
                        content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
                elif entry.filename == "xl/connections.xml" or entry.filename.startswith("xl/queryTables/"):
                    # Keep cached values, suppress refresh-on-open and background connections.
                    content = re.sub(rb'(\b(?:refreshOnLoad|backgroundRefresh|background)=)[\"\'](?:1|true)[\"\']',
                                     rb'\1"0"', content)
                result.writestr(entry, content)
    except (ElementTree.ParseError, zipfile.BadZipFile, RuntimeError):
        raise ValueError("Не удалось прочитать структуру офисного документа") from None
    return output.getvalue()


def _libreoffice(executable, source, output):
    profile = source.parent / "profile"
    profile.mkdir()
    # A private profile avoids another user's running LibreOffice instance.
    (profile / "user").mkdir()
    (profile / "user" / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
        '<item oor:path="/org.openoffice.Office.Calc/Content/Update">'
        '<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>'
        '<item oor:path="/org.openoffice.Office.Writer/Content/Update">'
        '<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>'
        '</oor:items>', encoding="utf-8")
    code = _run([executable, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo",
                 "--nodefault", "--norestore", "--convert-to", output.suffix[1:],
                 "--outdir", str(output.parent), str(source)])
    produced = output.parent / (source.stem + output.suffix)
    if code == 0 and produced.is_file():
        produced.replace(output)
    return code


def convert(name, raw, target, max_bytes=MAX_BYTES):
    """Convert an Office document to its native modern format or to PDF."""
    suffix = PurePath(name).suffix.lower()
    target = "." + str(target).lower().lstrip(".")
    if suffix not in NATIVE or target not in {NATIVE.get(suffix), ".pdf"}:
        raise ValueError("Этот офисный файл можно преобразовать в его современный формат или PDF")
    _validate_input(raw, suffix, max_bytes)
    if suffix in {".html", ".htm"}:
        normalized = _html_docx(raw, max_bytes)
        if target == ".docx":
            return normalized
        return convert("source.docx", normalized, target, max_bytes)
    if suffix == target:
        return raw
    backends = discover()
    application = APPLICATIONS[NATIVE[suffix]]
    if application not in backends and "LibreOffice" not in backends:
        raise ValueError(f"Для преобразования требуется установленный {application} или LibreOffice")
    with tempfile.TemporaryDirectory(prefix="ttkgpt-office-") as temporary:
        directory = Path(temporary)
        source = directory / ("source" + suffix)
        output = directory / ("converted" + target)
        source.write_bytes(_conversion_copy(raw, suffix, directory))
        try:
            code = _office(source, output, directory / "owner.json", application) if application in backends else 78
            if code in (73, 78) and "LibreOffice" in backends:
                code = _libreoffice(backends["LibreOffice"], source, output)
        except OSError:
            raise ValueError("Не удалось запустить локальное преобразование документа") from None
        if code == 73:
            raise ValueError(f"{application} занят открытым пользовательским окном. Повторите преобразование после его закрытия")
        if code or not output.is_file():
            raise ValueError("Не удалось преобразовать документ. Проверьте формат и отсутствие пароля")
        if not 0 < output.stat().st_size <= max_bytes:
            raise ValueError("Преобразованный документ превышает допустимый размер файла")
        result = output.read_bytes()
        if target == ".pdf":
            if not result.startswith(b"%PDF-"):
                raise ValueError("Преобразователь вернул некорректный PDF")
        else:
            _validate_input(result, target, max_bytes)
        return result
