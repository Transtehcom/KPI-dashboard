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

MODELS = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5"}
MODEL = "claude-sonnet-5-5"  # по умолчанию; руководитель выбирает модель в разделе «Звонки»
EFFORTS = {"low": "Низкий", "medium": "Средний", "high": "Высокий", "xhigh": "Сверхвысокий", "max": "Максимальный"}
EFFORT = "medium"
TIMEOUT = 300
CALL_TYPES = ["продажа", "тендер", "исполнение", "снабжение", "внутренний", "недозвон", "автоответчик", "личный", "другое"]
PROMPT_VERSION = 4  # меняется вместе с инструкцией: разбор прежней версии при ручной обработке считается недоделанным
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
- Секретарь (secretary = true): сотрудник ТТК говорил только с секретарём, приёмной, ресепшн, канцелярией, дежурным,
  коммутатором или другим промежуточным собеседником, который не решает вопрос по существу (спрашивает «по какому
  вопросу», принимает сообщение, просит прислать на почту, говорит, что нужного человека нет), и до нужного человека
  в этом звонке так и не дошёл. Сюда же — когда только узнали в приёмной или канцелярии, зарегистрировано ли письмо
  и кому передано, а с адресатом письма не поговорили. Если секретарь соединил и дальше разговор шёл с нужным человеком (специалистом,
  руководителем, ответственным за закупку) — false. Если с самого начала говорили с нужным человеком — false.
  Автоответчик, гудки, разговор с коллегой из ТТК — false.

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
 "secretary": true/false — разговор был только с секретарём, до нужного человека не дошли,
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


def _call_cli(prompt, config, model=MODEL, effort=EFFORT):
    work = os.path.join(ROOT, "data", "nlp-work")  # пустая папка: CLI не подхватывает CLAUDE.md и настройки проекта
    os.makedirs(work, exist_ok=True)
    cmd = [claude_path(config), "-p", "--output-format", "json", "--model", model, "--effort", effort, "--no-session-persistence",
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


def run_claude(prompt, config, attempts=2, model=MODEL, effort=EFFORT):
    """Ответ Claude и сведения о вызове ({"result": dict, …}). Изредка модель отдаёт JSON с синтаксической ошибкой —
    тогда запрос повторяется (attempts раз всего); исключение — если CLI так и не ответил разборным JSON."""
    cost = tokens_in = tokens_out = 0
    for n in range(attempts):
        retry_note = "\n\nВажно: ответ должен быть строго корректным JSON."
        env = _call_cli(prompt if not n else prompt + retry_note, config, model, effort)
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
                    "model": next(iter(env.get("modelUsage") or {}), model), "attempts": n + 1}
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


def analyze_masked(segments, meta, config, crm_names=(), model=MODEL, effort=EFFORT):
    """Разобрать расшифровку: (результат с метками вместо персональных данных, роли по предложениям, таблица замен,
    сведения о вызове, текст для Claude)."""
    numbered = sentences(segments)
    ps = Pseudonymizer(crm_names)
    prompt = build_prompt(meta, [(t, ps.mask(s)) for t, s in numbered])
    call = run_claude(prompt, config, model=model, effort=effort)
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

# Разбор Claude запускает только руководитель вручную (раздел «Звонки», кнопка «Обработать…»): лимит подписки
# автоматически не тратится.
DEFAULTS = {"analyze_min_chars": 80}
LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|лимит|429|overloaded", re.I)
MAX_ATTEMPTS = 3


def settings(config):
    return {**DEFAULTS, **(config.get("call_records") or {})}


def save(con, rid, res=None, roles=None, mapping=None, call=None, error=None, spent=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db_lock():
        if error:
            con.execute("INSERT INTO call_analysis (id, version, error, attempts, created) VALUES (?,?,?,1,?) "
                        "ON CONFLICT(id) DO UPDATE SET error=excluded.error, attempts=attempts+1, created=excluded.created",
                        (rid, PROMPT_VERSION, error[:300], now))
        else:
            call = call or {}
            secr = res.get("secretary")
            con.execute("INSERT OR REPLACE INTO call_analysis (id, version, call_type, offer, agreement, result, roles, pii, "
                        "model, tokens_in, tokens_out, cost_usd, spent, created, error, attempts, secretary) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?)",
                        (rid, PROMPT_VERSION, res.get("call_type"), int(bool((res.get("offer") or {}).get("made"))),
                         int(bool((res.get("agreement") or {}).get("reached"))), json.dumps(res, ensure_ascii=False),
                         roles, json.dumps(mapping or {}, ensure_ascii=False), call.get("model"), call.get("tokens_in"),
                         call.get("tokens_out"), call.get("cost_usd"), spent, now,
                         None if not isinstance(secr, bool) else int(secr)))
        con.commit()


def secretary_marks(con, ids):
    """Отметки «секретарь» для звонков: {id: {"ai": 1/0/None — разбор Claude v4+, "manual": 1/0/None — руководитель}}."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(f"SELECT id, secretary FROM call_analysis WHERE result IS NOT NULL AND id IN ({ph})", chunk):
            out.setdefault(r["id"], {"ai": None, "manual": None})["ai"] = r["secretary"]
        for r in con.execute(f"SELECT id, secretary FROM call_marks WHERE id IN ({ph})", chunk):
            out.setdefault(r["id"], {"ai": None, "manual": None})["manual"] = r["secretary"]
    return out


def set_secretary(con, rid, value, author):
    """Отметка руководителя: 1 — секретарь, 0 — не секретарь, None — как определил Claude."""
    with db_lock():
        if value is None:
            con.execute("DELETE FROM call_marks WHERE id=?", (rid,))
        else:
            con.execute("INSERT OR REPLACE INTO call_marks VALUES (?,?,?,?)",
                        (rid, int(bool(value)), author, dt.datetime.now().isoformat(timespec="seconds")))
        con.commit()


def get(con, rid):
    """Разбор с подставленными исходными значениями или None."""
    r = con.execute("SELECT * FROM call_analysis WHERE id=? AND result IS NOT NULL", (rid,)).fetchone()
    if not r:
        return None
    res = restore(json.loads(r["result"]), json.loads(r["pii"]) if r["pii"] else None)
    m = secretary_marks(con, [rid]).get(rid) or {}
    return {**res, "roles": r["roles"] or "", "version": r["version"], "model": r["model"],
            "secretary_ai": m.get("ai"), "secretary_manual": m.get("manual")}


def analyze_row(con, config, r, titles, log=print, model=MODEL, effort=EFFORT):
    """Разобрать один звонок очереди (строка call_records + расшифровка): "ok", "failed" или "limit" (лимит подписки —
    попытку не считаем, дальше идти бессмысленно)."""
    s = settings(config)
    segs = json.loads(r["segments"] or "[]")
    if len(r["text"] or "") < int(s["analyze_min_chars"]):  # гудки, «алло» без ответа — без Claude
        short = {"call_type": "недозвон", "summary": "Разговора нет или он слишком короткий для разбора",
                 "offer": {"made": False}, "agreement": {"reached": False}, "prices": [], "deadlines": [],
                 "objections": [], "tone": {}, "next_step": None, "local": True}
        save(con, r["id"], short, "-" * len(sentences(segs)), {}, {"model": "local"})
        return "ok"
    t = time.time()
    try:
        res, roles, mapping, call, _ = analyze_masked(
            segs, {"direction": r["direction"], "duration": r["duration"], "position": titles.get(r["emp"])},
            config, [r["client"] or ""], model, effort)
    except Exception as e:  # noqa
        if LIMIT_RE.search(str(e)):
            log(f"Разбор звонков: лимит Claude ({str(e)[:120]})")
            return "limit"
        save(con, r["id"], error=f"{type(e).__name__}: {e}")
        return "failed"
    save(con, r["id"], res, roles, mapping, call, spent=round(time.time() - t, 1))
    return "ok"


# ---------- ручная обработка звонков из раздела «Звонки»: расшифровка (если нет) и разбор Claude, в любое время суток

JOB = {"running": False, "total": 0, "done": 0, "failed": 0, "message": "", "started": None, "finished": None}


def todo(con, ids):
    """Из ids (новые первыми) — звонки с записью, у которых нет расшифровки текущей модели или разбора: {id: нужно_расшифровать}."""
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        have = {r["id"]: r["engine"] for r in con.execute(
            f"SELECT id, engine FROM call_transcripts WHERE segments IS NOT NULL AND id IN ({ph})", chunk)}
        done = {r["id"] for r in con.execute(f"SELECT id FROM call_analysis WHERE result IS NOT NULL AND version >= ? "
                                             f"AND id IN ({ph})", (PROMPT_VERSION, *chunk))}
        for rid in chunk:
            if rid not in done:
                out[rid] = have.get(rid) != ENGINE
    return {rid: out[rid] for rid in ids if rid in out}


def start_job(con, config, ids, force=False, model=MODEL, effort=EFFORT):
    """Запустить обработку ids в фоне. Возвращает (запущено, сообщение). force — разобрать заново, даже если разбор есть."""
    from . import call_records
    if not call_records._running.acquire(blocking=False):  # скачивание записей, расшифровка или другое задание уже идёт
        return False, "Сейчас идёт скачивание и обработка записей — попробуйте позже"
    ids = list(ids)
    plan = {rid: True for rid in ids} if force else todo(con, ids)
    if force:  # заново — только разбор; расшифровка нужна, лишь если её нет
        have = {r["id"] for r in con.execute(f"SELECT id FROM call_transcripts WHERE segments IS NOT NULL AND engine=? "
                                             f"AND id IN ({','.join('?' * len(ids)) or 'NULL'})", (ENGINE, *ids))}
        plan = {rid: rid not in have for rid in ids}
    if not plan:
        call_records._running.release()
        return False, "Все выбранные звонки уже обработаны"
    JOB.update(running=True, total=len(plan), done=0, failed=0, message="Расшифровка…" if any(plan.values()) else "Разбор…",
               started=dt.datetime.now().isoformat(timespec="seconds"), finished=None)

    def work():
        try:
            from . import transcripts
            need = [rid for rid, t in plan.items() if t]
            if need:
                def progress(ok, failed):
                    JOB["message"] = f"Расшифровка: {ok + failed} из {len(need)}"
                transcripts.run(con, config, call_records.records_dir(config), print, ids=need, progress=progress)
            titles = {e["login"]: e.get("title") for e in config.get("employees", [])}
            order = list(plan)
            for n, rid in enumerate(order, 1):
                JOB["message"] = f"Разбор: {n} из {len(order)}"
                r = con.execute("SELECT c.*, t.segments, t.text FROM call_records c JOIN call_transcripts t ON t.id = c.id "
                                "WHERE c.id=? AND t.engine=? AND t.segments IS NOT NULL", (rid, ENGINE)).fetchone()
                if not r:  # расшифровать не удалось
                    JOB["failed"] += 1
                    continue
                res = analyze_row(con, config, dict(r), titles, model=model, effort=effort)
                if res == "limit":
                    JOB["message"] = "Остановлено: лимит подписки Claude — повторите позже"
                    break
                JOB["done"] += res == "ok"
                JOB["failed"] += res == "failed"
            else:
                JOB["message"] = f"Готово: обработано {JOB['done']}" + (f", ошибок {JOB['failed']}" if JOB["failed"] else "")
        except Exception as e:  # noqa
            JOB["message"] = f"Ошибка: {e}"
        finally:
            JOB.update(running=False, finished=dt.datetime.now().isoformat(timespec="seconds"))
            call_records._running.release()

    import threading
    threading.Thread(target=work, daemon=True).start()
    return True, f"Запущена обработка: {len(plan)}"
