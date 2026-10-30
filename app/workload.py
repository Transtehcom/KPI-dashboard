"""Вкладка «Загрузка»: кто чем занят и где нужна помощь (ТЗ «Загрузка сотрудников», версия 2.1; п. 9 не входит).

Объём работы — количество задач Битрикс24, качество — сроки, проверка, возвраты и замечания. Часы, таймеры, оценки
времени и процент загрузки не загружаются и не используются.

Источник — таблицы task_ledger и task_people (их наполняет синхронизация: bitrix24.fetch_task_ledger). Здесь
считаются свойства каждой задачи, не зависящие от периода; итоги за период и фильтры считает страница
(web/index.html, renderWorkload) по этим свойствам.

В Битрикс24 нет отдельных «запросов на перенос» и «замечаний», поэтому:
- режим с проверкой — «Принять работу после завершения» у задачи, где постановщик ≠ исполнитель; сдача на проверку —
  переход в «Ждёт контроля» (4), приёмка — из 4 в «Завершена» (5), возврат — из 4 обратно в работу;
- выполнено: без проверки — дата завершения, с проверкой — дата приёмки. Срок сравнивается с датой завершения или
  с датой сдачи той версии, которую приняли. Задача с проверкой, закрытая без «Ждёт контроля», — «качество не оценено»;
- согласованный срок — первоначальный срок с переносами постановщика (или кого-то, кроме исполнителя). Перенос самим
  исполнителем срок для оценки не меняет и считается запросом переноса, по которому решения нет;
- замечание по сроку (автоматическое) — согласованный срок прошёл, результат не сдан, переноса до срока не было; одно
  на один срок, дата — сам срок. Если исполнитель до срока сам переносил дату — «Запрос подан вовремя, решение не
  получено», замечание на разборе (в подтверждённые не входит). Замечание по качеству — каждый возврат с проверки;
- блокировка — статус «Отложена» или тег из workload.block_tags (срок не отменяет); помощь руководителя — тег из
  workload.help_tags; родительская задача с подзадачами и отклонённые задачи (7) в суммы не входят.
"""
import datetime as dt
import json
import math

LOOKBACK_DAYS = 180  # задачи, изменённые за этот срок, плюс все открытые
EKB = dt.timezone(dt.timedelta(hours=5))
OPEN = {"1", "2", "3", "4", "6"}
WORKING = {"1", "2", "3"}
STATUS = {"1": "Новая", "2": "Ждёт выполнения", "3": "Выполняется", "4": "На проверке", "5": "Завершена",
          "6": "Отложена", "7": "Отклонена"}
BLOCK_TAGS = ["блокировка", "заблокирована"]
HELP_TAGS = ["помощь", "нужна помощь", "помощь ИД", "вопрос ИД"]


def settings(config):
    """config["workload"]: block_tags / help_tags — теги блокировки и вопроса к ИД, director_b24_id — ИД
    (по умолчанию — сотрудник с должностью «Исполнительный директор»)."""
    c = config.get("workload") or {}
    low = lambda xs: {str(x).strip().lower() for x in xs}
    return {"block_tags": low(c.get("block_tags") or BLOCK_TAGS), "help_tags": low(c.get("help_tags") or HELP_TAGS),
            "director": int(c["director_b24_id"]) if c.get("director_b24_id") else None}


def members(config):
    """Сотрудники панели — из раздела «Сотрудники» (config["employees"]), у кого указан ID в Битрикс24:
    [{uid, login, name, title, department}]."""
    out = []
    for e in config.get("employees", []):
        try:
            uid = int(e.get("b24_user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid:
            out.append({"uid": uid, "login": e["login"], "name": e.get("name") or e["login"],
                        "title": e.get("title"), "department": e.get("department")})
    return out


def _t(s):
    return dt.datetime.fromisoformat(s) if s else None


def _days(a, b):
    """Целых дней от a до b (начатый день — целый), не меньше 1."""
    return max(1, math.ceil((_t(b) - _t(a)).total_seconds() / 86400))


def _agreed(t, hist):
    """(первоначальный срок, [(с какого момента, согласованный срок)], переносы до срока, после срока, запросы исполнителя)."""
    uid, creator = t["uid"], t["creator"]
    moves = [(at, a, b, who) for at, f, a, b, who in hist if f == "DEADLINE"]
    orig = moves[0][1] if moves else t["deadline"]
    chain, ok, late, asked = [(t["created"] or "", orig)], [], [], []
    for at, _a, b, who in moves:
        if who == uid and uid != creator:  # исполнитель сам сдвинул дату — для оценки это только запрос
            asked.append(at)
            continue
        cur = chain[-1][1]
        if cur:
            (late if at > cur else ok).append(at)
        chain.append((at, b))
    return orig, chain, ok, late, asked


def analyze(t, now, block_tags=(), help_tags=()):
    """Свойства задачи t (строка task_ledger) на момент now («ГГГГ-ММ-ДДTЧЧ:ММ:СС», Екатеринбург)."""
    hist = json.loads(t["history"] or "[]")
    uid, status = t["uid"], t["status"]
    check = bool(t["control"]) and t["creator"] != uid
    moves = [(at, a, b) for at, f, a, b, _w in hist if f == "STATUS"]
    submits = [at for at, a, b in moves if b == "4"]
    returns = [at for at, a, b in moves if a == "4" and b in WORKING]
    done = status == "5"
    closed = t["closed"] if done else None
    to_done = [a for _at, a, b in moves if b == "5"]
    checked = check and done and bool(to_done) and to_done[-1] == "4"
    if done:  # дата исполнения: сдача принятой версии (с проверкой) или завершение
        exec_at = max([s for s in submits if s <= closed], default=closed) if checked else closed
    elif status == "4" and submits:
        exec_at = submits[-1]
    else:
        exec_at = None
    orig, chain, moves_ok, moves_late, asked = _agreed(t, hist)
    agreed = chain[-1][1]

    remarks = [{"type": "качество", "at": r, "reason": "Возврат на доработку", "review": False} for r in returns]
    seen = set()
    for i, (since, d) in enumerate(chain):
        nxt = chain[i + 1][0] if i + 1 < len(chain) else None
        if not d or d >= now or since > d or (nxt and nxt <= d) or (exec_at and exec_at <= d) or d in seen:
            continue  # срок не наступил, перенесён до срока, результат сдан вовремя или замечание уже есть
        seen.add(d)
        req = any(since <= a <= d for a in asked)
        remarks.append({"type": "срок", "at": d, "review": req,
                        "reason": "Запрос подан вовремя, решение не получено" if req else
                        "Нарушение срока без предварительного согласования. Исполнитель не запросил перенос"})
    remarks.sort(key=lambda r: r["at"])
    confirmed = [r for r in remarks if not r["review"]]

    tags = json.loads(t["tags"] or "[]")
    low = {x.strip().lower() for x in tags}
    overdue = status in WORKING | {"6"} and agreed and agreed < now
    review_delay = status == "4" and agreed and agreed < now and exec_at and exec_at <= agreed
    transfers = [at for at, f, a, b, _w in hist if f == "RESPONSIBLE_ID" and str(b) == str(uid)]
    if check and checked:
        quality = "remarks" if confirmed else "clean"
    elif check and done:
        quality = "not_rated"
    else:
        quality = "remarks" if confirmed else "none"
    return {
        "id": t["id"], "uid": uid, "title": t["title"], "status": status,
        "status_label": "На доработке" if status in WORKING and returns and (not submits or returns[-1] > submits[-1])
        else STATUS.get(status, status),
        "priority": t["priority"], "project": t["project"], "tags": tags, "creator": t["creator"],
        "mode": "check" if check else "simple", "created": t["created"], "transferred": transfers[-1] if transfers else None,
        "deadline_orig": orig, "deadline": agreed, "deadline_b24": t["deadline"], "closed": closed,
        "exec_at": exec_at, "accepted": closed if checked else None, "checked": checked,
        "on_time": (exec_at <= agreed) if exec_at and agreed and (done or status == "4") else None,
        "open": status in OPEN, "on_review": status == "4",
        "on_rework": status in WORKING and bool(returns) and (not submits or returns[-1] > submits[-1]),
        "returns": returns, "remarks": remarks, "quality": quality,
        "moves_ok": moves_ok, "moves_late": moves_late, "self_moves": asked,
        "overdue_days": _days(agreed, now) if overdue else 0,
        "review_delay_days": _days(agreed, now) if review_delay else 0,
        "blocked": status == "6" or bool(low & set(block_tags)), "help": bool(low & set(help_tags)),
        "has_result": any(f in ("RESULT", "UF_TASK_WEBDAV_FILES") for _at, f, *_ in hist),
    }


def build(con, config, now=None, uids=None, portal=None, dept_titles=None):
    """Данные вкладки. uids — показать только этих исполнителей (не руководителю — только он сам);
    dept_titles — полные названия отделов из kpi_rules.json («ОП» → «Отдел продаж (ОП)»)."""
    st = settings(config)
    now = (now or dt.datetime.now(EKB).replace(tzinfo=None)).strftime("%Y-%m-%dT%H:%M:%S")
    team = members(config)
    names = {p["uid"]: p["name"] for p in con.execute("SELECT uid, name FROM task_people")}  # постановщики не из раздела
    names.update({p["uid"]: p["name"] for p in team})
    people = sorted((p for p in team if uids is None or p["uid"] in uids), key=lambda p: p["name"])
    shown = {p["uid"] for p in people}
    director = st["director"] or next((p["uid"] for p in team
                                       if "исполнительный директор" in (p["title"] or "").lower()), None)
    rows = [dict(r) for r in con.execute("SELECT * FROM task_ledger")]
    parents = {r["parent"] for r in rows if r["parent"]}
    tasks = [analyze(r, now, st["block_tags"], st["help_tags"]) for r in rows
             if r["status"] != "7" and r["id"] not in parents and r["uid"] in shown]
    for t in tasks:
        t["creator_name"] = names.get(t["creator"]) or (f"ID {t['creator']}" if t["creator"] else None)
    tasks.sort(key=lambda t: t["created"] or "", reverse=True)
    dept_titles = dept_titles or {}
    return {"generated": now, "portal": portal, "has_data": bool(rows), "director": director,
            "people": [{"uid": p["uid"], "name": p["name"], "position": p["title"],
                        "dept": dept_titles.get(p["department"], p["department"]) or "Без отдела"} for p in people],
            "tasks": tasks}
