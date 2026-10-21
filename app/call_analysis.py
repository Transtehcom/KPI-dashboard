"""Разбор звонка через Claude Code CLI по подписке (claude -p): обезличенная расшифровка → JSON с предложением,
договорённостью, ценами, сроками, возражениями, тональностью, следующим шагом и ролью говорящего по предложениям.

Перед отправкой текст обезличивается (app/anonymize.py), в ответ возвращаются исходные значения. CLI вызывается
с собственной короткой инструкцией, без инструментов, MCP, настроек и сохранения сессий, из пустой папки —
служебная часть запроса ~1 тыс. токенов вместо ~39 тыс. (на подписке Pro это лимит, общий с обычной работой)."""
import datetime as dt
import json
import os
import re
import subprocess
import time

from .anonymize import Pseudonymizer, restore
from .common import ROOT, db_lock
from .transcripts import ENGINE

MODEL = "sonnet"
TIMEOUT = 300
CALL_TYPES = ["продажа", "тендер", "исполнение", "снабжение", "внутренний", "недозвон", "автоответчик", "личный", "другое"]
PROMPT_VERSION = 3  # меняется вместе с инструкцией: разборы прежней версии делаются заново
OBJECTIONS = ["цена", "сроки", "нет потребности", "уже есть поставщик", "нет бюджета", "не ЛПР", "нужно согласование",
              "условия оплаты", "качество", "другое"]
TONES = ["позитивный", "нейтральный", "негативный"]

SYSTEM_PROMPT = f"""Ты анализируешь расшифровки телефонных звонков сотрудников компании «Транстехком» (ТТК) — поставщика
защитных сооружений и оборудования для промышленных предприятий. Расшифровка автоматическая: возможны ошибки в словах
(название нашей компании часто искажено: «Транстелеком», «Транстеком»), реплики собеседников не разделены.
Персональные данные заменены метками вида [ИМЯ_1], [ОРГ_2], [МЕСТО_1], [ТЕЛЕФОН_1] — используй метки как есть.
Каждое предложение расшифровки пронумеровано.

Тип звонка (call_type) — ровно один, по главной теме разговора:
- продажа — ТТК продаёт: поиск и развитие клиента, выявление потребности, предложение продукции, КП вне тендерной процедуры;
- тендер — работа по закупочной процедуре клиента: запрос ТКП, вопросы по документации, подача и итоги заявки, переторжка;
- исполнение — по уже заключённому договору или заказу клиента: спецификации, сроки поставки, документы, оплата, отгрузка;
- снабжение — ТТК покупает: разговор с поставщиком или подрядчиком, в том числе входящее предложение поставщика нам,
  даже если сотрудник ТТК только перенаправил звонившего в отдел снабжения («закупка» клиента, в которой покупают
  у ТТК, — это тендер или исполнение, не снабжение);
- внутренний — разговор с коллегой из ТТК;
- недозвон — по теме звонка ни с кем не поговорили: нужного человека нет, секретарь не соединил, просят перезвонить;
  если по существу поговорили с другим сотрудником собеседника (узнали статус письма, заявки, получили сведения) —
  это не недозвон, тип — по теме разговора;
- автоответчик — живого разговора не было: меню, голосовая почта, гудки;
- личный — не по работе; другое — ничего из перечисленного.

Определения (строго):
- Предложение (offer.made = true): сотрудник ТТК в этом звонке предложил собеседнику конкретный товар, услугу или
  решение ТТК (назвал, что именно, — с ценой или без), ИЛИ договорился выслать или подготовить коммерческое предложение (КП/ТКП),
  в том числе после того, как клиент пришлёт недостающие данные.
  Не считается: общий рассказ о компании, только вопросы о потребности, обсуждение деталей уже отправленного КП или
  действующего заказа (уточнения, согласование формулировок), предложение, которое делают сотруднику ТТК
  (например, поставщик — нашему снабженцу).
- Договорённость (agreement.reached = true): разговор закончился конкретным следующим шагом, на который собеседник
  согласился, и у шага есть дата или срок: встреча или ВКС, отправка КП с обещанием рассмотреть, счёт, согласование
  договора, заказ, подписание и отправка документов, созвон в названный день. Относится к звонку любого типа,
  в том числе к исполнению договора («подпишем спецификацию и отправим сегодня»). Не считается: «перезвоните как-нибудь» без срока, отказ, «посмотрим»,
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


def roles_of(turns, n):
    """Роль говорящего для каждого из n предложений по диапазонам из ответа: строка из «М», «К», «А» и «-» (нет роли)."""
    roles = ["-"] * n
    for turn in turns or []:
        try:
            a, b = int(turn["from"]), int(turn["to"])
        except (KeyError, TypeError, ValueError):
            continue
        who = {"M": "М", "K": "К", "A": "А"}.get(str(turn.get("who")).upper(), str(turn.get("who")).upper())
        if who in ("М", "К", "А"):
            for i in range(max(1, a), min(n, b) + 1):
                roles[i - 1] = who
    return "".join(roles)


def analyze_masked(segments, meta, config, crm_names=()):
    """Разобрать расшифровку: (результат с метками вместо персональных данных, роли по предложениям, таблица замен,
    сведения о вызове, текст для Claude)."""
    numbered = sentences(segments)
    ps = Pseudonymizer(crm_names)
    prompt = build_prompt(meta, [(t, ps.mask(s)) for t, s in numbered])
    call = run_claude(prompt, config)
    res = call.pop("result")
    roles = roles_of(res.pop("turns", None), len(numbered))
    return res, roles, ps.mapping, call, prompt


def analyze(segments, meta, config, crm_names=()):
    """То же с исходными значениями (для проб): (результат, таблица замен, сведения о вызове, текст для Claude);
    lines = [[секунда, роль, текст]], speakers_ok — роль есть у каждого предложения."""
    res, roles, mapping, call, prompt = analyze_masked(segments, meta, config, crm_names)
    res = restore(res, mapping)
    res["speakers_ok"] = "-" not in roles
    res["lines"] = [[t, roles[i].replace("-", ""), s] for i, (t, s) in enumerate(sentences(segments))]
    return res, mapping, call, prompt


# ---------- очередь разбора в дашборде (таблица call_analysis)

# analyze_hours — когда можно тратить лимит подписки на разбор: [«с», «до»] по местному времени, окно может переходить
# через полночь; пусто — в любое время
DEFAULTS = {"analyze": True, "analyze_per_run": 30, "analyze_min_chars": 80, "analyze_hours": ["17:30", "08:00"]}
LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|лимит|429|overloaded", re.I)
MAX_ATTEMPTS = 3


def settings(config):
    return {**DEFAULTS, **(config.get("call_records") or {})}


def in_hours(config, now=None):
    """Сейчас — разрешённое время для разбора (analyze_hours)."""
    hours = settings(config).get("analyze_hours")
    if not hours:
        return True
    t = (now or dt.datetime.now()).strftime("%H:%M")
    start, end = hours
    return start <= t < end if start <= end else (t >= start or t < end)


def pending(con, limit):
    """Расшифрованные текущей моделью звонки без разбора текущей версии инструкции — от новых к старым."""
    return [dict(r) for r in con.execute(
        "SELECT c.*, t.segments, t.text FROM call_records c JOIN call_transcripts t ON t.id = c.id "
        "LEFT JOIN call_analysis a ON a.id = c.id WHERE c.file IS NOT NULL AND t.engine = ? "
        "AND (a.id IS NULL OR (a.version < ? AND a.attempts < ?) OR (a.error IS NOT NULL AND a.attempts < ?)) "
        "ORDER BY c.dt DESC LIMIT ?", (ENGINE, PROMPT_VERSION, MAX_ATTEMPTS, MAX_ATTEMPTS, int(limit)))]


def save(con, rid, res=None, roles=None, mapping=None, call=None, error=None, spent=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db_lock():
        if error:
            con.execute("INSERT INTO call_analysis (id, version, error, attempts, created) VALUES (?,?,?,1,?) "
                        "ON CONFLICT(id) DO UPDATE SET error=excluded.error, attempts=attempts+1, created=excluded.created",
                        (rid, PROMPT_VERSION, error[:300], now))
        else:
            call = call or {}
            con.execute("INSERT OR REPLACE INTO call_analysis (id, version, call_type, offer, agreement, result, roles, pii, "
                        "model, tokens_in, tokens_out, cost_usd, spent, created, error, attempts) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
                        (rid, PROMPT_VERSION, res.get("call_type"), int(bool((res.get("offer") or {}).get("made"))),
                         int(bool((res.get("agreement") or {}).get("reached"))), json.dumps(res, ensure_ascii=False),
                         roles, json.dumps(mapping or {}, ensure_ascii=False), call.get("model"), call.get("tokens_in"),
                         call.get("tokens_out"), call.get("cost_usd"), spent, now))
        con.commit()


def get(con, rid):
    """Разбор с подставленными исходными значениями или None."""
    r = con.execute("SELECT * FROM call_analysis WHERE id=? AND result IS NOT NULL", (rid,)).fetchone()
    if not r:
        return None
    res = restore(json.loads(r["result"]), json.loads(r["pii"]) if r["pii"] else None)
    return {**res, "roles": r["roles"] or "", "version": r["version"], "model": r["model"]}


def run(con, config, log=print):
    """Разобрать очередь. Возвращает (разобрано, ошибок, остановлено_по_лимиту)."""
    s = settings(config)
    if not s["analyze"] or not in_hours(config):
        return 0, 0, False
    titles = {e["login"]: e.get("title") for e in config.get("employees", [])}
    ok = failed = 0
    for r in pending(con, s["analyze_per_run"]):
        if not in_hours(config):  # окно закончилось посреди прохода
            break
        segs = json.loads(r["segments"] or "[]")
        if len(r["text"] or "") < int(s["analyze_min_chars"]):  # гудки, «алло» без ответа — без Claude
            short = {"call_type": "недозвон", "summary": "Разговора нет или он слишком короткий для разбора",
                     "offer": {"made": False}, "agreement": {"reached": False}, "prices": [], "deadlines": [],
                     "objections": [], "tone": {}, "next_step": None, "local": True}
            save(con, r["id"], short, "-" * len(sentences(segs)), {}, {"model": "local"})
            ok += 1
            continue
        t = time.time()
        try:
            res, roles, mapping, call, _ = analyze_masked(
                segs, {"direction": r["direction"], "duration": r["duration"], "position": titles.get(r["emp"])},
                config, [r["client"] or ""])
        except Exception as e:  # noqa
            if LIMIT_RE.search(str(e)):  # лимит подписки — остановиться до следующей синхронизации, попытку не считать
                log(f"Разбор звонков: лимит Claude ({str(e)[:120]}) — продолжу при следующей синхронизации")
                return ok, failed, True
            save(con, r["id"], error=f"{type(e).__name__}: {e}")
            failed += 1
            continue
        save(con, r["id"], res, roles, mapping, call, spent=round(time.time() - t, 1))
        ok += 1
    return ok, failed, False
