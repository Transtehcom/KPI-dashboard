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
FORMATS = {".png", ".jpg", ".jpeg", ".webp"}
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def normalize(raw):
    """Decode and re-encode pixel data, excluding EXIF and other source metadata."""
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Размер изображения должен быть от 1 байта до 10 МБ")
    try:
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                fmt = source.format
                if fmt not in MIMES or source.width * source.height > MAX_PIXELS:
                    raise ValueError("Поддерживаются PNG, JPEG и WebP до 20 миллионов пикселей")
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Прикрепите статичное изображение без анимации")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                pixels = Image.new(mode, oriented.size)
                pixels.paste(oriented.convert(mode))
                output = io.BytesIO()
                pixels.save(output, fmt)
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


def local_ocr(raw):
    """Recognize a temporary image locally, never using a network OCR service."""
    if os.name != "nt":
        raise ValueError("Локальное распознавание документа требует Windows OCR. Прикрепите документ с текстовым слоем")
    image_path = None
    try:
        # Windows codecs need a consistent PNG irrespective of the uploaded format.
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
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
