#!/usr/bin/env python3
"""Локальная расшифровка телефонных звонков (русский язык).

Модели: GigaAM v3 с пунктуацией и заглавными буквами (Сбер, русская речь) + Whisper turbo (сверка).
Всё работает офлайн после первой загрузки моделей (~0,8 ГБ с Whisper, ~170 МБ для --fast; один раз).
Сравнение на звонках ТТК (21.10.2026): v3 точнее v2, меньше обрывков, 3,7 с на минуту звонка на i3-12100;
T-one (телефония 8 кГц) — без пунктуации и с бо́льшим числом ошибок.

Установка:  pip install sherpa-onnx soundfile numpy scipy
Запуск:     python transcribe.py звонок.mp3 [ещё.mp3 ...] [--fast] [--threads=4]
            --fast  только GigaAM (в 6-7 раз быстрее Whisper; пунктуацию ставит сама GigaAM v3)
Результат:  звонок.txt рядом с аудиофайлом

Для дашборда (app/transcripts.py): python transcribe.py --jsonl --fast
            пути к файлам — по строке в stdin; на каждый файл — строка JSON в stdout:
            {"path": …, "duration": секунды, "segments": [[начало, конец, текст], …]} или {"path": …, "error": …}
"""
import json, sys, tarfile, urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import sherpa_onnx as so
from scipy.signal import resample_poly

MODELS = Path.home() / ".cache" / "call-transcribe"
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
GIGA = "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16"
WHISPER = "sherpa-onnx-whisper-turbo"
SR = 16000


def fetch(name, archive=True):
    target = MODELS / name
    if target.exists():
        return target
    MODELS.mkdir(parents=True, exist_ok=True)
    url = BASE + name + (".tar.bz2" if archive else "")
    tmp = MODELS / (name + ".part")
    print(f"Скачиваю {name} (один раз)...", file=sys.stderr)
    urllib.request.urlretrieve(url, tmp)
    if archive:
        with tarfile.open(tmp, "r:bz2") as t:
            t.extractall(MODELS, filter="data")
        tmp.unlink()
    else:
        tmp.rename(target)
    return target


def load_audio(path):
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)  # стерео -> моно
    if sr != SR:
        g = np.gcd(sr, SR)
        a = resample_poly(a, SR // g, sr // g).astype("float32")
    return a


PAD = 0.25  # запас по краям фрагмента: детектор речи обрезает первые и последние слоги


def segments(audio):
    """Фрагменты речи [(начало в секундах, отсчёты)] — с запасом PAD по краям из исходной записи."""
    vc = so.VadModelConfig()
    vc.silero_vad.model = str(fetch("silero_vad.onnx", archive=False))
    vc.silero_vad.threshold = 0.35  # телефонная речь тихая: при 0.4 терялись короткие реплики
    vc.silero_vad.min_silence_duration = 0.5
    vc.silero_vad.min_speech_duration = 0.15
    vc.silero_vad.max_speech_duration = 25
    vc.sample_rate = SR
    vad = so.VoiceActivityDetector(vc, buffer_size_in_seconds=600)
    spans = []
    for i in range(0, len(audio), 512):
        vad.accept_waveform(audio[i:i + 512])
        while not vad.empty():
            spans.append((vad.front.start, vad.front.start + len(vad.front.samples)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        spans.append((vad.front.start, vad.front.start + len(vad.front.samples)))
        vad.pop()
    p = int(PAD * SR)
    out = []
    for s, e in spans:
        s, e = max(0, s - p), min(len(audio), e + p)
        out.append((s / SR, audio[s:e]))
    return out


def recognizers(fast, threads=4):
    g = fetch(GIGA)
    recs = [("G", so.OfflineRecognizer.from_transducer(
        encoder=str(g / "encoder.int8.onnx"), decoder=str(g / "decoder.onnx"),
        joiner=str(g / "joiner.onnx"), tokens=str(g / "tokens.txt"),
        model_type="nemo_transducer", num_threads=threads))]
    if not fast:
        w = fetch(WHISPER)
        recs.append(("W", so.OfflineRecognizer.from_whisper(
            encoder=str(w / "turbo-encoder.int8.onnx"),
            decoder=str(w / "turbo-decoder.int8.onnx"),
            tokens=str(w / "turbo-tokens.txt"), language="ru", num_threads=threads)))
    return recs


def ts(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def transcribe(path, recs):
    """(длительность, [(начало, конец, {метка модели: текст})]) по фрагментам речи."""
    audio = load_audio(path)
    out = []
    for start, smp in segments(audio):
        texts = {}
        for tag, r in recs:
            s = r.create_stream()
            s.accept_waveform(SR, smp)
            r.decode_stream(s)
            texts[tag] = s.result.text.strip()
        out.append((start, start + len(smp) / SR, texts))
    return len(audio) / SR, out


def jsonl(recs):
    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        try:
            dur, segs = transcribe(Path(path), recs)
            res = {"path": path, "duration": round(dur, 1),
                   "segments": [[round(a, 1), round(b, 1), x["G"]] for a, b, x in segs if x["G"]]}
        except Exception as e:  # noqa
            res = {"path": path, "error": f"{type(e).__name__}: {e}"[:300]}
        print(json.dumps(res, ensure_ascii=False), flush=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fast = "--fast" in sys.argv
    threads = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--threads=")), 4)
    if "--jsonl" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        return jsonl(recognizers(fast, threads))
    if not args:
        sys.exit(__doc__)
    recs = recognizers(fast, threads)
    for f in map(Path, args):
        dur, segs = transcribe(f, recs)
        lines = [f"# {f.name} | длительность {ts(dur)}",
                 "# G = GigaAM (точнее по словам), W = Whisper (пунктуация)"]
        for start, end, texts in segs:
            lines.append(f"[{ts(start)}-{ts(end)}]")
            for tag, text in texts.items():
                lines.append(f"  {tag}: {text}")
        out = f.with_suffix(".txt")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Готово: {out}")


if __name__ == "__main__":
    main()
