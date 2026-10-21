"""Разбор звонка через Claude Code CLI по подписке (claude -p): обезличенная расшифровка → JSON с предложением,
договорённостью, ценами, сроками, возражениями, тональностью, следующим шагом и ролью говорящего по предложениям.

Перед отправкой текст обезличивается (app/anonymize.py), в ответ возвращаются исходные значения. CLI вызывается
с собственной короткой инструкцией, без инструментов, MCP, настроек и сохранения сессий, из пустой папки —
служебная часть запроса ~1 тыс. токенов вместо ~39 тыс. (на подписке Pro это лимит, общий с обычной работой)."""
import json
import os
import re
import subprocess

from .anonymize import Pseudonymizer
from .common import ROOT

MODEL = "sonnet"
TIMEOUT = 300
CALL_TYPES = ["продажа", "тендер", "снабжение", "сервис", "внутренний", "недозвон", "автоответчик", "личный", "другое"]
OBJECTIONS = ["цена", "сроки", "нет потребности", "уже есть поставщик", "нет бюджета", "не ЛПР", "нужно согласование",
              "условия оплаты", "качество", "другое"]
TONES = ["позитивный", "нейтральный", "негативный"]

SYSTEM_PROMPT = f"""Ты анализируешь расшифровки телефонных звонков сотрудников компании «Транстехком» (ТТК) — поставщика
защитных сооружений и оборудования для промышленных предприятий. Расшифровка автоматическая: возможны ошибки в словах
(название нашей компании часто искажено: «Транстелеком», «Транстеком»), реплики собеседников не разделены.
Персональные данные заменены метками вида [ИМЯ_1], [ОРГ_2], [МЕСТО_1], [ТЕЛЕФОН_1] — используй метки как есть.
Каждое предложение расшифровки пронумеровано.

Определения (строго):
- Предложение (offer.made = true): сотрудник ТТК предложил собеседнику конкретный товар, услугу или решение ТТК
  (назвал, что именно, — с ценой или без), ИЛИ договорился выслать коммерческое предложение (КП).
  Не считается: общий рассказ о компании, только вопросы о потребности, предложение, которое делают сотруднику ТТК
  (например, поставщик — нашему снабженцу).
- Договорённость (agreement.reached = true): разговор закончился конкретным следующим шагом, на который собеседник
  согласился, и у шага есть дата или срок: встреча или ВКС, отправка КП с обещанием рассмотреть, счёт, согласование
  договора, заказ, созвон в названный день. Не считается: «перезвоните как-нибудь» без срока, отказ, «посмотрим»,
  секретарь не соединил, автоответчик, недозвон.

Ответь ОДНИМ JSON-объектом без пояснений и без markdown:
{{
 "turns": [{{"from": номер, "to": номер, "who": "М"}}] — кто говорит, диапазонами подряд идущих предложений, от первого до последнего без пропусков: М — сотрудник ТТК, К — собеседник, А — автоответчик, меню, музыка,
 "call_type": одно из {json.dumps(CALL_TYPES, ensure_ascii=False)},
 "summary": "суть звонка в 1–2 предложениях",
 "offer": {{"made": true/false, "what": "что предложено или null", "kp_promised": true/false, "price_named": true/false, "sentences": [номера]}},
 "agreement": {{"reached": true/false, "what": "о чём договорились или null", "when": "дата или срок как в разговоре или null", "sentences": [номера]}},
 "prices": [{{"value": "сумма как в разговоре", "what": "за что", "sentence": номер}}],
 "deadlines": [{{"value": "срок как в разговоре", "what": "что к этому сроку", "sentence": номер}}],
 "objections": [{{"type": одно из {json.dumps(OBJECTIONS, ensure_ascii=False)}, "text": "суть возражения кратко", "sentence": номер}}],
 "tone": {{"client": одно из {json.dumps(TONES, ensure_ascii=False)}, "manager": одно из {json.dumps(TONES, ensure_ascii=False)}}},
 "next_step": "следующий шаг или null"
}}"""


def sentences(segments):
    """Предложения расшифровки с таймкодом фрагмента: [(секунда, текст)]."""
    out = []
    for start, _end, text in segments:
        for s in re.split(r"(?<=[.!?…])\s+", text or ""):
            if s.strip():
                out.append((start, s.strip()))
    return out


def build_prompt(meta, numbered):
    head = (f"Звонок: {'исходящий' if meta.get('direction') == 'out' else 'входящий'}, длительность "
            f"{int(meta.get('duration') or 0) // 60}:{int(meta.get('duration') or 0) % 60:02d}."
            + (f" Сотрудник ТТК: {meta['position']}." if meta.get("position") else ""))
    lines = "\n".join(f"{i} [{int(t // 60)}:{int(t % 60):02d}] {s}" for i, (t, s) in enumerate(numbered, 1))
    return f"{head}\n\nРасшифровка:\n{lines}"


def claude_path(config):
    return (config.get("call_records") or {}).get("claude_path") or os.path.expanduser(r"~\.local\bin\claude.exe")


def _call_cli(prompt, config):
    work = os.path.join(ROOT, "data", "nlp-work")  # пустая папка: CLI не подхватывает CLAUDE.md и настройки проекта
    os.makedirs(work, exist_ok=True)
    cmd = [claude_path(config), "-p", "--output-format", "json", "--model", MODEL, "--no-session-persistence",
           "--system-prompt", SYSTEM_PROMPT, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--disable-slash-commands"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=work, timeout=TIMEOUT,
                       creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW
    try:
        env = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"Claude CLI: код {p.returncode}, {(p.stderr or p.stdout)[:300]}")
    if env.get("is_error"):
        raise RuntimeError(f"Claude CLI: {str(env.get('result'))[:300]}")
    return env


def run_claude(prompt, config, attempts=2):
    """Ответ Claude и сведения о вызове ({"result": dict, …}). Изредка модель отдаёт JSON с синтаксической ошибкой —
    тогда запрос повторяется (attempts раз всего); исключение — если CLI так и не ответил разборным JSON."""
    cost = tokens_in = tokens_out = 0
    for n in range(attempts):
        retry_note = "\n\nВажно: ответ должен быть строго корректным JSON."
        env = _call_cli(prompt if not n else prompt + retry_note, config)
        usage = env.get("usage") or {}
        cost += env.get("total_cost_usd") or 0
        tokens_in += sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        tokens_out += usage.get("output_tokens") or 0
        text = env.get("result") or ""
        m = re.search(r"\{.*\}", text, re.S)  # на случай обёртки ```json … ```
        try:
            result = json.loads(m.group()) if m else None
        except ValueError:
            result = None
        if isinstance(result, dict):
            return {"result": result, "cost_usd": cost, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "model": next(iter(env.get("modelUsage") or {}), MODEL), "attempts": n + 1}
    raise RuntimeError(f"Claude ответил не JSON: {text[:200]}")


def analyze(segments, meta, config, crm_names=()):
    """Разобрать расшифровку. Возвращает (результат с исходными значениями, таблица замен, сведения о вызове, текст
    для Claude). В результат добавляются предложения с ролью говорящего: lines = [[секунда, роль, текст]]."""
    numbered = sentences(segments)
    ps = Pseudonymizer(crm_names)
    masked = [(t, ps.mask(s)) for t, s in numbered]
    prompt = build_prompt(meta, masked)
    call = run_claude(prompt, config)
    res = ps.restore(call.pop("result"))
    roles = [""] * len(numbered)
    for turn in res.get("turns") or []:
        try:
            a, b = int(turn["from"]), int(turn["to"])
        except (KeyError, TypeError, ValueError):
            continue
        who = {"M": "М", "K": "К", "A": "А"}.get(str(turn.get("who")).upper(), str(turn.get("who")).upper())
        if who in ("М", "К", "А"):
            for i in range(max(1, a), min(len(numbered), b) + 1):
                roles[i - 1] = who
    res["speakers_ok"] = all(roles)  # каждому предложению досталась роль
    res["lines"] = [[t, roles[i], s] for i, (t, s) in enumerate(numbered)]
    return res, ps.mapping, call, prompt
