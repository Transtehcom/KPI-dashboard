"""Local decoding and recognition of short ttkGPT voice messages."""
import base64
import binascii
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import wave

from .transcripts import WORKER, plain_text

MAX_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 120
_slot = threading.BoundedSemaphore(1)
FORMATS = {'.ogg':'ogg', '.oga':'ogg', '.opus':'ogg', '.webm':'matroska',
           '.m4a':'mov', '.mp4':'mov', '.aac':'aac', '.mp3':'mp3', '.wav':'wav'}


def decode_upload(content):
    if not isinstance(content, str) or len(content) > 4*((MAX_BYTES+2)//3):
        raise ValueError('Аудиозапись превышает 10 МБ.')
    try:
        return base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Некорректная аудиозапись.') from None


def recognize(name, raw):
    fmt = FORMATS.get(Path(name).suffix.lower())
    if not fmt or not raw or len(raw) > MAX_BYTES:
        raise ValueError('Нужна аудиозапись до 10 МБ.')
    if not _slot.acquire(timeout=5):
        raise ValueError('Распознавание занято. Повторите через минуту.')
    try:
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'input'
            wav = Path(directory) / 'voice.wav'
            audio.write_bytes(raw)
            options = dict(check=True, capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin','-v','error',
                '-protocol_whitelist','file','-f',fmt,'-i',str(audio),'-map','0:a:0','-t',str(MAX_SECONDS+1),
                '-ac','1','-ar','16000','-f','s16le','pipe:1'], timeout=20, **options)
            if len(result.stdout) > 32000*MAX_SECONDS:
                raise ValueError('Голосовое сообщение должно быть не длиннее двух минут.')
            with wave.open(str(wav),'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed'))
                out.writeframes(result.stdout)
            encoded = subprocess.run([sys.executable, WORKER,'--jsonl','--fast','--threads=2'],
                input=(str(wav)+'\n').encode(),timeout=120, **options)
            response = json.loads(encoded.stdout)
            if response.get('error'):
                raise ValueError('Не удалось распознать голосовое сообщение.')
            text = plain_text(response['segments']).strip()
            if not text:
                raise ValueError('Не удалось разобрать речь. Попробуйте записать ещё раз.')
            return text
    except (ImportError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError('Не удалось обработать аудио локальным распознавателем.') from None
    finally:
        _slot.release()
