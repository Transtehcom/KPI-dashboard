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
from .crm_links import object_url
import json
import math
import re
from .common import db_lock, merge_rows
from .onec_workload import AUTHOR_ONLY, IMMEDIATE_COMPLETION_ENTITIES, document_closed

LOOKBACK_DAYS = 180  # задачи, изменённые за этот срок, плюс все открытые
EKB = dt.timezone(dt.timedelta(hours=5))
OPEN = {"1", "2", "3", "4", "6"}
WORKING = {"1", "2", "3"}
STATUS = {"1": "Новая", "2": "Ждёт выполнения", "3": "Выполняется", "4": "На проверке", "5": "Завершена",
          "6": "Отложена", "7": "Отклонена"}
BLOCK_TAGS = ["блокировка", "заблокирована"]
HELP_TAGS = ["помощь", "нужна помощь", "помощь ИД", "вопрос ИД"]
DIRECTIONS = ["ОП", "ОС", "ОЛ", "Маркетолог", "ФО", "ЮО", "HR", "IT", "Помощники руководителя"]


def employee_group(employee):
    """Категория сотрудника по отделу или конкретной должности из согласованной таблицы."""
    if employee.get("login") in AUTHOR_ONLY:
        return "Помощники руководителя"
    role = str(employee.get("title") or "").strip().casefold()
    department = str(employee.get("department") or "").strip().casefold()
    if "финансов" in role and "менеджер" in role:
        return "ФО"
    if "юрист" in role:
        return "ЮО"
    if re.search(r"(?:^|\W)(?:it|ит)[\s-]+специалист(?:\W|$)", role):
        return "IT"
    if department == "оп" or department.startswith("отдел продаж"):
        return "ОП"
    if department == "ос" or department.startswith("отдел снабжения"):
        return "ОС"
    if department == "ол" or department.startswith("отдел логистики"):
        return "ОЛ"
    if "маркетинг" in department:
        return "Маркетолог"
    if department == "hr" or "управления персоналом" in department:
        return "HR"
    return None


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


def member_configuration(con, team):
    """Личные настройки состава поверх категории, определённой по текущим сотрудникам."""
    overrides = {row["login"]: row for row in con.execute("SELECT * FROM workload_member_settings")}
    result = []
    for person in team:
        default_group = employee_group(person)
        saved = overrides.get(person["login"])
        direction = saved["direction"] if saved else None
        result.append({"login": person["login"], "name": person["name"], "uid": person["uid"],
                       "direction": direction, "default_group": default_group, "group": direction or default_group,
                       "included": bool(saved["included"]) if saved else default_group is not None})
    return result


def save_members(con, config, rows, author):
    """Проверить весь пакет и атомарно сохранить только переданные строки состава панели."""
    with db_lock():
        if not isinstance(rows, list):
            raise ValueError("Ожидается список сотрудников")
        candidates = {person["login"]: person for person in members(config)}
        seen, values = set(), []
        updated = dt.datetime.now(EKB).isoformat(timespec="seconds")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Некорректная запись сотрудника")
            login = row.get("login")
            if not isinstance(login, str) or login not in candidates:
                raise ValueError("Сотрудник с ID Битрикс24 не найден")
            if login in seen:
                raise ValueError("Сотрудник повторяется в списке")
            seen.add(login)
            included = row.get("included")
            if not isinstance(included, bool):
                raise ValueError("Укажите, включён ли сотрудник в таблицу")
            direction = row.get("direction")
            if direction is not None and (not isinstance(direction, str) or direction and direction not in DIRECTIONS):
                raise ValueError("Неизвестное направление")
            direction = direction or None
            if included and not (direction or employee_group(candidates[login])):
                raise ValueError("Выберите направление для включаемого сотрудника")
            values.append((login, int(included), direction, author, updated))
        with con:
            con.executemany("INSERT INTO workload_member_settings(login,included,direction,updated_by,updated_at) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(login) DO UPDATE SET included=excluded.included, "
                            "direction=excluded.direction,updated_by=excluded.updated_by,updated_at=excluded.updated_at", values)


def store_onec(con, data, updated=None):
    """Обновить успешные типы документов, сохраняя срезы источников с ошибкой. Транзакцией управляет синхронизация."""
    updated = updated or dt.datetime.now(EKB).replace(tzinfo=None).isoformat(timespec="seconds")
    con.execute("DELETE FROM workload_onec_state WHERE entity IN ('__fetch__','$metadata','Catalog_Пользователи')")
    errors = data.get("errors") or {}
    successful = set(data.get("entities") or ()) - set(errors)
    for entity in successful:
        con.execute("DELETE FROM workload_onec_documents WHERE entity=?", (entity,))
        con.execute("INSERT INTO workload_onec_state(entity,updated,since,error) VALUES(?,?,?,NULL) "
                    "ON CONFLICT(entity) DO UPDATE SET updated=excluded.updated,since=excluded.since,error=NULL",
                    (entity, updated, data.get("since")))
    merge_rows(con, "workload_onec_documents", [r for r in data.get("rows", []) if r["entity"] in successful],
               commit=False)
    for entity, error in errors.items():
        con.execute("INSERT INTO workload_onec_state(entity,error) VALUES(?,?) "
                    "ON CONFLICT(entity) DO UPDATE SET error=excluded.error", (entity, error))


def _onec_data(con, people):
    by_login = {p["login"]: p["uid"] for p in people}
    documents = []
    for row in con.execute("SELECT * FROM workload_onec_documents ORDER BY date DESC, id"):
        if row["emp"] not in by_login:
            continue
        document = dict(row)
        timing_applicable = document["entity"] not in IMMEDIATE_COMPLETION_ENTITIES
        if not timing_applicable:
            document.update(closed_at=document["date"], deadline=None, deadline_source=None)
        closed = document_closed(document)
        document.update(uid=by_login[row["emp"]], posted=bool(row["posted"]), is_closed=closed, open=not closed, timing_applicable=timing_applicable)
        documents.append(document)
    states = [dict(r) for r in con.execute("SELECT * FROM workload_onec_state ORDER BY entity")]
    updated = [s["updated"] for s in states if s["updated"]]
    since = [s["since"] for s in states if s["updated"] and s["since"]]
    return documents, {"loaded": bool(updated), "updated": min(updated, default=None),
                       "since": max(since, default=None),
                       "errors": {s["entity"]: s["error"] for s in states if s["error"]}}


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
    late_intervals = []
    seen = set()
    for i, (since, d) in enumerate(chain):
        nxt = chain[i + 1][0] if i + 1 < len(chain) else None
        if d:
            late_start = max(since, d)
            late_end = min(at for at in (now, nxt, exec_at) if at)
            if late_start < late_end:
                late_intervals.append({"start": late_start, "end": late_end})
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
        "priority": t["priority"], "project": t["project"], "project_id": t.get("project_id"), "tags": tags, "creator": t["creator"],
        "mode": "check" if check else "simple", "created": t["created"], "transferred": transfers[-1] if transfers else None,
        "deadline_orig": orig, "deadline": agreed, "deadline_b24": t["deadline"], "closed": closed,
        "exec_at": exec_at, "accepted": closed if checked else None, "checked": checked,
        "on_time": (exec_at <= agreed) if exec_at and agreed and (done or status == "4") else None,
        "open": status in OPEN, "on_review": status == "4",
        "on_rework": status in WORKING and bool(returns) and (not submits or returns[-1] > submits[-1]),
        "returns": returns, "remarks": remarks, "quality": quality,
        "moves_ok": moves_ok, "moves_late": moves_late, "self_moves": asked,
        "late_intervals": late_intervals,
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
    configuration = member_configuration(con, people)
    member_settings = {p["login"]: p for p in configuration}
    shown = {p["uid"] for p in people}
    director = st["director"] or next((p["uid"] for p in team
                                       if "исполнительный директор" in (p["title"] or "").lower()), None)
    rows = [dict(r) for r in con.execute("SELECT * FROM task_ledger")]
    parents = {r["parent"] for r in rows if r["parent"]}
    tasks = [analyze(r, now, st["block_tags"], st["help_tags"]) for r in rows
             if r["status"] != "7" and r["id"] not in parents and r["uid"] in shown]
    link_config = config if config.get("bitrix24") else {"webhook": "https://" + portal} if portal else {}
    for t in tasks:
        t["creator_name"] = names.get(t["creator"]) or (f"ID {t['creator']}" if t["creator"] else None)
        t["url"] = object_url(link_config,"task",t["id"],t["uid"])
        t["creator_url"] = object_url(link_config,"user",t["creator"])
        t["project_url"] = object_url(link_config,"group",t["project_id"])
    tasks.sort(key=lambda t: t["created"] or "", reverse=True)
    documents, onec = _onec_data(con, people)
    dept_titles = dept_titles or {}
    return {"generated": now, "portal": portal, "has_data": bool(rows), "director": director,
            "people": [{"uid": p["uid"], "name": p["name"], "position": p["title"],
                        "group": member_settings[p["login"]]["group"],
                        "included": member_settings[p["login"]]["included"],
                        "url": object_url(link_config,"user",p["uid"]),
                        "dept": dept_titles.get(p["department"], p["department"]) or "Без отдела"} for p in people],
            "tasks": tasks, "documents": documents, "onec": onec,
            "configuration": {"directions": DIRECTIONS, "members": configuration}}
