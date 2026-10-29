"""Загрузка сотрудников по открытым задачам Битрикс24 (вкладка «Загрузка»).

Источник — таблица workload_tasks (её наполняет синхронизация: bitrix24.fetch_workload_tasks). Расчёт:

- ёмкость сотрудника — day_hours (9) часов в рабочий день: пн–пт без праздников (config.holidays), плюс рабочие
  субботы/воскресенья (config.workdays), без дней отсутствия из раздела «Сотрудники» (отпуск, больничный и т. д.);
- часы задачи = оценка − списанное время; они поровну раскладываются по рабочим дням от начала работы
  (плановое начало, иначе дата создания, но не раньше сегодня) до срока. Просроченная задача целиком ложится на
  сегодня. Задача без срока в расчёт не входит и попадает в список «без срока»;
- задача без оценки в расчёт не входит и попадает в список «без оценки» (оценка обязательна);
- родительская задача с открытыми подзадачами не считается, чтобы не удваивать часы;
- загрузка недели = часы / ёмкость; больше 100% — перегрузка. Ёмкость текущей недели считается с сегодняшнего дня.
"""
import datetime as dt
from collections import defaultdict

from .common import parse_date

DEFAULTS = {"day_hours": 9.0, "weeks": 6}
OVERLOAD = 100          # % загрузки, выше которого неделя считается перегруженной
WARN = 80               # % загрузки, выше которого неделя «на пределе»


def settings(config):
    """Параметры из config["workload"]: day_hours, weeks, include_b24_ids (кого учитывать; по умолчанию всех)."""
    c = config.get("workload") or {}
    return {"day_hours": float(c.get("day_hours") or DEFAULTS["day_hours"]),
            "weeks": max(1, min(26, int(c.get("weeks") or DEFAULTS["weeks"]))),
            "include": {int(x) for x in c["include_b24_ids"]} if c.get("include_b24_ids") else None}


def members(config):
    """{b24_user_id: login} — кого учитываем в загрузке: сотрудники с ID в Битрикс24 (из раздела «Сотрудники»),
    ограниченные списком workload.include_b24_ids, если он задан."""
    include = settings(config)["include"]
    out = {}
    for e in config.get("employees", []):
        try:
            uid = int(e.get("b24_user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid and (include is None or uid in include):
            out[uid] = e["login"]
    return out


def _absent_days(absences):
    days = set()
    for a in absences or []:
        d1, d2 = parse_date(a.get("from")), parse_date(a.get("to") or a.get("from"))
        while d1 and d2 and d1 <= d2:
            days.add(d1)
            d1 += dt.timedelta(days=1)
    return days


def _work_days(first, last, holidays, workdays, absent):
    out, d = [], first
    while d <= last:
        iso = d.isoformat()
        if ((d.weekday() < 5 and iso not in holidays) or iso in workdays) and d not in absent:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def build(con, config, today=None, logins=None, portal=None):
    """Загрузка по неделям. logins — ограничить сотрудниками (для не руководителя: только он сам)."""
    st = settings(config)
    today = today or dt.date.today()
    day_hours, weeks_n = st["day_hours"], st["weeks"]
    holidays, workdays = set(config.get("holidays") or []), set(config.get("workdays") or [])
    monday0 = today - dt.timedelta(days=today.weekday())
    mondays = [monday0 + dt.timedelta(weeks=w) for w in range(weeks_n)]
    horizon = mondays[-1] + dt.timedelta(days=6)

    people = []
    for e in config.get("employees", []):
        try:
            uid = int(e.get("b24_user_id") or 0)
        except (TypeError, ValueError):
            uid = 0
        if not uid or (st["include"] is not None and uid not in st["include"]):
            continue
        if logins is not None and e["login"] not in logins:
            continue
        people.append(e)

    rows = con.execute("SELECT * FROM workload_tasks").fetchall()
    by_emp = defaultdict(list)
    for r in rows:
        by_emp[r["emp"]].append(dict(r))
    parents = {r["parent"] for r in rows if r["parent"]}
    out_people, no_est, no_dl = [], [], []
    for e in people:
        login, name, uid = e["login"], e["name"], int(e["b24_user_id"])
        absent = _absent_days(e.get("absences"))
        load = defaultdict(float)
        planned = []
        n_noest = n_nodl = 0
        for t in by_emp.get(login, []):
            if t["task_id"] in parents:
                continue  # родитель с открытыми подзадачами — считаем подзадачи
            dl = parse_date(t["deadline"])
            item = {"task_id": t["task_id"], "title": t["title"], "deadline": dl.isoformat() if dl else None}
            if not (t["estimate"] or 0) > 0:
                n_noest += 1
                no_est.append({**item, "login": login, "name": name, "b24_user_id": uid})
                continue
            left = max((t["estimate"] or 0) - (t["spent"] or 0), 0)
            if left <= 0:
                continue
            if not dl:
                n_nodl += 1
                no_dl.append({**item, "login": login, "name": name, "b24_user_id": uid, "hours": round(left, 1)})
                continue
            a = max(today, parse_date(t["start"]) or parse_date(t["created"]) or today)
            b = dl
            if b < today:  # просрочена — вся работа на сегодня
                a = b = today
            a = min(a, b)
            # окно задачи целиком в отпуске или выходных — первый доступный день после начала окна (а не раньше него)
            days = _work_days(a, b, holidays, workdays, absent) or _work_days(a, horizon, holidays, workdays, absent)[:1] or [a]
            per_day = left / len(days)
            for d in days:
                load[d] += per_day
            planned.append({**item, "hours": round(left, 1), "per_week": [
                round(per_day * sum(1 for d in days if m <= d <= m + dt.timedelta(days=6)), 1) for m in mondays]})
        wk = []
        for m in mondays:
            first = max(m, today)
            wdays = _work_days(first, m + dt.timedelta(days=6), holidays, workdays, absent)
            cap = day_hours * len(wdays)
            hours = sum(h for d, h in load.items() if m <= d <= m + dt.timedelta(days=6))
            peak = max((load.get(d, 0) / day_hours for d in wdays), default=0)
            if not cap and hours:
                pct = 999
            else:
                pct = round(100 * hours / cap) if cap else 0
            wk.append({"monday": m.isoformat(), "hours": round(hours, 1), "capacity": cap, "percent": min(pct, 999),
                       "peak": round(100 * peak), "absent_days": sum(1 for i in range(5) if (m + dt.timedelta(days=i)) in absent)})
        planned.sort(key=lambda x: -x["hours"])
        out_people.append({"login": login, "name": name, "b24_user_id": uid, "department": e.get("department"), "weeks": wk,
                           "planned": len(planned), "no_estimate": n_noest, "no_deadline": n_nodl,
                           "tasks": planned[:40], "overloaded": any(w["percent"] > OVERLOAD for w in wk)})
    return {"generated": dt.datetime.now().isoformat(timespec="minutes"), "today": today.isoformat(), "day_hours": day_hours,
            "mondays": [m.isoformat() for m in mondays], "people": out_people, "no_estimate": no_est[:300],
            "no_deadline": no_dl[:300], "overload": OVERLOAD, "warn": WARN, "portal": portal,
            "has_data": bool(rows)}
