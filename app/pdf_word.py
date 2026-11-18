"""Local, time-bounded PDF to editable Word conversion."""
import subprocess
import sys
import tempfile
from pathlib import Path


CONVERSION_TIMEOUT = 90
WORKER = """
import sys
try:
    from pdf2docx import Converter
except ImportError:
    sys.exit(78)
converter = Converter(sys.argv[1])
try:
    converter.convert(sys.argv[2], multi_processing=False, ignore_page_error=False)
finally:
    converter.close()
"""


def convert(raw, max_bytes):
    """Use a separate local process so failed or slow parsing cannot persist a partial file."""
    with tempfile.TemporaryDirectory(prefix="ttkgpt-pdf-word-") as temporary:
        source = Path(temporary) / "source.pdf"
        target = Path(temporary) / "converted.docx"
        source.write_bytes(raw)
        try:
            process = subprocess.run(
                [sys.executable, "-c", WORKER, str(source), str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=CONVERSION_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Преобразование PDF превысило лимит 90 секунд") from None
        except OSError:
            raise ValueError("Не удалось запустить локальное преобразование PDF в Word") from None
        if process.returncode == 78:
            raise ValueError("На сервере отсутствует pdf2docx или его зависимости для преобразования PDF в Word")
        if process.returncode or not target.is_file():
            raise ValueError("Не удалось преобразовать все страницы PDF в Word")
        if not 0 < target.stat().st_size <= max_bytes:
            raise ValueError("Преобразованный Word превышает лимит 3 МБ")
        return target.read_bytes()
