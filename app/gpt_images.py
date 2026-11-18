"""Validated image attachments and strictly local Windows OCR."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import warnings

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".gif", ".avif", ".heic", ".heif", ".svg"}
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "ICO": "image/x-icon",
         "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif", "AVIF": "image/avif", "HEIF": "image/heif"}
OUTPUTS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF", "ico": "ICO", "gif": "GIF", "pdf": "PDF"}


def normalize(raw):
    """Decode and re-encode pixel data, excluding EXIF and other source metadata."""
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Размер изображения должен быть от 1 байта до 10 МБ")
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        if raw.lstrip().startswith(b"<"):
            from .gpt_svg import render
            with Image.open(io.BytesIO(render(raw))) as rendered:
                return raw, {"mime": "image/svg+xml", "width": rendered.width, "height": rendered.height}
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                fmt = source.format
                if fmt not in MIMES or source.width * source.height > MAX_PIXELS:
                    raise ValueError("Поддерживаются обычные изображения до 20 миллионов пикселей")
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Прикрепите статичное изображение без анимации")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                pixels = Image.new(mode, oriented.size)
                pixels.paste(oriented.convert(mode))
                output = io.BytesIO()
                options = {"quality": 95, "subsampling": 0} if fmt == "JPEG" else {}
                if fmt == "ICO":
                    options = {"sizes": [pixels.size]}
                pixels.save(output, fmt, **options)
                clean = output.getvalue()
                if len(clean) > MAX_BYTES:
                    raise ValueError("После очистки изображение превышает 10 МБ. Уменьшите его размер")
                return clean, {"mime": MIMES[fmt], "width": pixels.width, "height": pixels.height}
    except ImportError:
        raise ValueError("На сервере отсутствует Pillow для проверки изображений") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("Не удалось прочитать изображение. Прикрепите корректный PNG, JPEG или WebP до 20 миллионов пикселей") from None


def rasterize(raw):
    """Return formats supported by vision and local OCR without external resources."""
    from PIL import Image
    clean, metadata = normalize(raw)
    if metadata["mime"] == "image/svg+xml":
        from .gpt_svg import render
        clean = render(clean)
    if metadata["mime"] not in ("image/png", "image/jpeg", "image/webp"):
        with Image.open(io.BytesIO(clean)) as source:
            output = io.BytesIO()
            source.convert("RGBA").save(output, "PNG")
            clean = output.getvalue()
        metadata = {**metadata, "mime": "image/png"}
    return clean, metadata


def resize_suffix(width, height):
    for value in (width, height):
        if value is not None and (type(value) is not int or not 1 <= value <= 8192):
            raise ValueError("Ширина и высота задаются целыми числами от 1 до 8192 пикселей")
    if width and height and width * height > MAX_PIXELS:
        raise ValueError("Размер превышает 20 миллионов пикселей")
    return f"_{width or 'auto'}x{height or 'auto'}" if width or height else ""


def convert(raw, target, width=None, height=None):
    """Resize local pixels with preserved proportions, padding a requested canvas."""
    from PIL import Image, ImageOps
    resize_suffix(width, height)
    clean, _metadata = rasterize(raw)
    with Image.open(io.BytesIO(clean)) as source:
        rgba = source.convert("RGBA")
        size = (width or round(source.width * height / source.height), height or round(source.height * width / source.width)) if width or height else source.size
        if min(size) < 1 or max(size) > 8192 or size[0] * size[1] > MAX_PIXELS:
            raise ValueError("Размер изображения превышает допустимый предел")
        rgba = ImageOps.contain(rgba, size, Image.Resampling.LANCZOS)
        pixels = Image.new("RGBA", size, "white")
        pixels.alpha_composite(rgba, ((size[0] - rgba.width) // 2, (size[1] - rgba.height) // 2))
        fmt = OUTPUTS[target]
        if fmt in ("JPEG", "PDF", "BMP"):
            pixels = pixels.convert("RGB")
        if fmt == "ICO" and max(size) > 256:
            raise ValueError("Для ICO укажите размер до 256 × 256 пикселей")
        output = io.BytesIO()
        options = {"quality": 95, "subsampling": 0} if fmt == "JPEG" else {}
        if fmt == "ICO":
            options = {"sizes": [size]}
        pixels.save(output, fmt, **options)
        raw = output.getvalue()
    if len(raw) > MAX_BYTES:
        raise ValueError("Преобразованный файл превышает 10 МБ")
    return raw


def local_ocr(raw):
    """Recognize a temporary image locally, never using a network OCR service."""
    if os.name != "nt":
        raise ValueError("Локальное распознавание документа требует Windows OCR. Прикрепите документ с текстовым слоем")
    image_path = None
    try:
        # Windows codecs need a consistent PNG irrespective of the uploaded format.
        from PIL import Image
        with Image.open(io.BytesIO(rasterize(raw)[0])) as image:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                image_path = temporary.name
                image.convert("RGB").save(temporary, "PNG")
        process = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(Path(__file__).with_name("gpt_image_ocr.ps1")), image_path],
            capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if process.returncode:
            raise ValueError("Windows OCR не смог распознать документ. Проверьте языки распознавания Windows или прикрепите файл с текстовым слоем")
        text = json.loads(process.stdout.decode("utf-8-sig"))["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 250000:
            raise ValueError("В изображении не найден читаемый текст. Прикрепите более чёткий скан или документ с текстовым слоем")
        return text
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError, KeyError):
        raise ValueError("Локальное распознавание документа недоступно. Прикрепите документ с текстовым слоем или повторите позже") from None
    finally:
        if image_path:
            Path(image_path).unlink(missing_ok=True)
