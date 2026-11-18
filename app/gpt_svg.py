"""Validate SVG attachments and render bounded PNG pixels in a local worker."""
import io
import math
import os
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser


MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DIMENSION = 8192
RENDER_TIMEOUT = 30
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
LENGTH = re.compile(r"([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|in|cm|mm|q|%)?", re.I)
UNITS = {"px": 1, "pt": 96 / 72, "pc": 16, "in": 96, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6}
WORKER = """
import sys
try:
    import resvg_py
    svg = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
    if len(svg) > 10 * 1024 * 1024:
        sys.exit(1)
    png = resvg_py.svg_to_bytes(svg_string=svg.decode('utf-8'),
                               width=int(sys.argv[1]), height=int(sys.argv[2]), dpi=96)
    if not 0 < len(png) <= 10 * 1024 * 1024:
        sys.exit(1)
    sys.stdout.buffer.write(png)
except Exception:
    sys.exit(1)
"""


class _SvgTreeBuilder(ElementTree.TreeBuilder):
    def pi(self, target, text):
        raise ValueError("Инструкции обработки XML в SVG недоступны")


def _length(value, reference):
    if value is None:
        return None
    match = LENGTH.fullmatch(value.strip())
    if not match:
        raise ValueError("SVG должен задавать корректные размеры в абсолютных единицах или viewBox")
    number = float(match[1])
    unit = (match[2] or "px").lower()
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Размеры SVG должны быть положительными и конечными")
    if unit == "%":
        if reference is None:
            raise ValueError("Процентные размеры SVG требуют viewBox")
        return number * reference / 100
    return number * UNITS[unit]


def _check_references(value):
    # Reject CSS escapes so encoded function names and resource paths cannot bypass checks.
    if "\\" in value:
        raise ValueError("Экранированные CSS-ссылки в SVG недоступны")
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    if "/*" in value or "*/" in value or re.search(r"@\s*import\b", value, re.I):
        raise ValueError("Внешние стили SVG недоступны")
    for reference in re.findall(r"\burl\s*\(([^)]*)\)", value, re.I):
        reference = reference.strip().strip("\"'").strip()
        if not re.fullmatch(r"#[\w.:-]+", reference):
            raise ValueError("Внешние ресурсы SVG недоступны")
    if re.search(r"\burl\s*\(", re.sub(r"\burl\s*\([^)]*\)", "", value, flags=re.I), re.I):
        raise ValueError("Незавершённые CSS-ссылки в SVG недоступны")


def _validated(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Размер SVG должен быть от 1 байта до 10 МБ")
    try:
        parser = DefusedXMLParser(target=_SvgTreeBuilder(), forbid_dtd=True,
                                  forbid_entities=True, forbid_external=True)
        parser.feed(raw)
        root = parser.close()
    except (ElementTree.ParseError, DefusedXmlException):
        raise ValueError("Прикрепите корректный SVG без DTD и XML-сущностей") from None
    if root.tag not in ("svg", "{" + SVG_NAMESPACE + "}svg"):
        raise ValueError("Корневой элемент изображения должен быть SVG")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "animate", "animatemotion", "animatetransform", "set"}:
            raise ValueError("SVG должен содержать статичное изображение без исполняемого содержимого")
        for key, value in element.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or key == "{http://www.w3.org/XML/1998/namespace}base":
                raise ValueError("Активные обработчики и базовые адреса SVG недоступны")
            if name in {"href", "src"} and not re.fullmatch(r"#[\w.:-]+", value.strip()):
                raise ValueError("Внешние ресурсы SVG недоступны")
            _check_references(value)
        if tag == "style":
            _check_references("".join(element.itertext()))
    viewbox = None
    if "viewBox" in root.attrib:
        try:
            viewbox = [float(part) for part in re.split(r"[\s,]+", root.attrib["viewBox"].strip())]
        except ValueError:
            raise ValueError("Некорректный viewBox SVG") from None
        if len(viewbox) != 4 or not all(math.isfinite(value) for value in viewbox) or min(viewbox[2:]) <= 0:
            raise ValueError("Некорректный viewBox SVG")
    width = _length(root.get("width"), viewbox[2] if viewbox else None)
    height = _length(root.get("height"), viewbox[3] if viewbox else None)
    if width is None or height is None:
        if viewbox is None:
            raise ValueError("SVG должен задавать ширину и высоту или viewBox")
        width = width or (height * viewbox[2] / viewbox[3] if height else viewbox[2])
        height = height or width * viewbox[3] / viewbox[2]
    if not all(math.isfinite(value) and 0 < value <= MAX_DIMENSION for value in (width, height)):
        raise ValueError("Размеры SVG не должны превышать 8192 пикселя")
    width, height = math.ceil(width), math.ceil(height)
    if width * height > MAX_PIXELS:
        raise ValueError("SVG не должен превышать 20 миллионов пикселей")
    return ElementTree.tostring(root, encoding="utf-8"), width, height


def render(raw):
    """Return validated PNG bytes without giving the renderer resource paths."""
    svg, width, height = _validated(raw)
    try:
        with tempfile.TemporaryDirectory(prefix="ttkgpt-svg-") as temporary:
            process = subprocess.run(
                [sys.executable, "-I", "-c", WORKER, str(width), str(height)],
                input=svg, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=temporary, timeout=RENDER_TIMEOUT, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except subprocess.TimeoutExpired:
        raise ValueError("Преобразование SVG превысило лимит 30 секунд") from None
    except OSError:
        raise ValueError("Не удалось запустить локальное преобразование SVG") from None
    if process.returncode or not 0 < len(process.stdout) <= MAX_BYTES:
        raise ValueError("Не удалось преобразовать SVG в PNG до 10 МБ")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(process.stdout)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ValueError("Некорректные размеры результата SVG")
            image.verify()
    except Exception:
        raise ValueError("Преобразование SVG вернуло некорректный PNG") from None
    return process.stdout
