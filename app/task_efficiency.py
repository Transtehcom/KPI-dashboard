"""Месячная эффективность: 100 − замечания / задачи в работе × 100.

Задачи исполнителя и соисполнителя, кроме поставленных им самому себе.
Замечание — нарушение отдельного крайнего срока; перенос после просрочки
не стирает нарушение. История позволяет восстановить выбранный месяц.
"""
import datetime as dt
import json
import math
import re
from urllib.parse import urlencode

from .common import month_bounds

ACTIVE = {"1", "2", "3", "4", "6"}
LATE = ACTIVE - {"4"}  # сданная на контроль задача не просрочена по вине исполнителя
FIELDS = {"DEADLINE", "STATUS", "RESPONSIBLE_ID", "ACCOMPLICES", "CREATED_BY"}


def ids(value):
    if isinstance(value, (list, tuple, set)):
        return {int(x) for x in value if str(x).isdigit()}
    return {int(x) for x in re.findall(r"\d+", str(value or ""))}


def calculate(rows, uid, month, now=None):
    first,last = month_bounds(month)
    start = first.isoformat() + "T00:00:00"
    finish = (last + dt.timedelta(days=1)).isoformat() + "T00:00:00"
    now = now or dt.datetime.now()
    finish = min(finish, now.isoformat(timespec="seconds"))
    details = []
    for row in {r["id"]: dict(r) for r in rows}.values():
        # Как в виджете Б24: удалённое участие не добавляет задачу в его текущую выборку,
        # даже если сотрудника кратковременно добавляли в соисполнители в прошлом.
        current_responsible = int(row.get("responsible_id") or 0) == uid
        if (not current_responsible and uid not in ids(row.get("accomplices"))) or \
                (current_responsible and int(row.get("creator_id") or 0) == uid):
            continue
        created = row.get("created")
        if not created or created >= finish:
            continue
        history = sorted(json.loads(row.get("history") or "[]"), key=lambda h:h[0])
        state = {"DEADLINE":row.get("deadline"), "STATUS":str(row.get("status") or "2"),
                 "RESPONSIBLE_ID":str(row.get("responsible_id") or "0"),
                 "ACCOMPLICES":row.get("accomplices") or "[]", "CREATED_BY":str(row.get("creator_id") or "0")}
        for field in FIELDS:
            changes = [h for h in history if h[1] == field]
            if changes:
                state[field] = changes[0][2]
        # Закрытые задачи без изменений статуса: восстановить открытое состояние до CLOSED_DATE.
        closed = row.get("closed")
        events = [h for h in history if h[1] in FIELDS]
        if closed and not any(h[1] == "STATUS" and str(h[3]) == "5" and h[0] == closed for h in events):
            if not any(h[1] == "STATUS" for h in events):
                state["STATUS"] = "2"
            events.append([closed,"STATUS","2","5"])
        events.sort(key=lambda h:h[0])
        remarks, roles, counted, completed = {},set(),False,False

        def member():
            responsible = str(state["RESPONSIBLE_ID"]) == str(uid)
            helper = uid in ids(state["ACCOMPLICES"])
            own = responsible and str(state["CREATED_BY"]) == str(uid)
            return (responsible or helper) and not own

        def segment(since, until):
            nonlocal counted
            a,b = max(since,start),min(until,finish)
            if a >= b or str(state["STATUS"]) not in ACTIVE or not member():
                return
            counted = True
            roles.add("Исполнитель" if str(state["RESPONSIBLE_ID"]) == str(uid) else "Соисполнитель")
            deadline = state["DEADLINE"]
            if deadline and str(state["STATUS"]) in LATE and created <= deadline and deadline < b:
                remarks.setdefault(deadline,max(deadline,a))

        since = created
        for at,field,_before,after in events:
            if at < created:
                state[field] = after
                continue
            if at >= finish:
                break
            segment(since,at)
            if field == "STATUS" and str(after) == "5" and start <= at and member():
                completed = counted = True
                roles.add("Исполнитель" if str(state["RESPONSIBLE_ID"]) == str(uid) else "Соисполнитель")
            state[field],since = after,at
        segment(since,finish)
        if counted:
            details.append({"id":row["id"],"title":row.get("title") or row["id"],"role":" / ".join(sorted(roles)),
                            "created":created,"closed":closed,"completed":completed,
                            "violations":[{"deadline":d,"at":at} for d,at in sorted(remarks.items())]})
    total = len(details)
    violations = sum(len(r["violations"]) for r in details)
    raw = max(0, min(100, 100 - violations / total * 100)) if total else 100
    return {"total":total,"completed":sum(r["completed"] for r in details),"violations":violations,
            "percent":math.floor(raw + .5),"raw_percent":raw,"rows":details}


def normalize(task, history, b24):
    changes = []
    for h in history:
        field = h.get("field")
        if field not in FIELDS:
            continue
        value = h.get("value") or {}
        before,after = value.get("from"),value.get("to")
        if field == "DEADLINE":
            before,after = b24.ekb(before),b24.ekb(after)
        changes.append([b24.ekb(h["createdDate"]),field,before,after])
    return {"id":str(task["id"]),"title":task.get("title"),"responsible_id":int(task.get("responsibleId") or 0),
            "creator_id":int(task.get("createdBy") or 0),"created":b24.ekb(task.get("createdDate")),
            "deadline":b24.ekb(task.get("deadline")),"closed":b24.ekb(task.get("closedDate")),
            "status":str(task.get("status") or "2"),"accomplices":json.dumps(task.get("accomplices") or []),
            "history":json.dumps(changes,ensure_ascii=False)}


def fetch(b24, employees, date_from=None):
    from .bitrix24 import B24Error
    uids = sorted({int(e["b24_user_id"]) for e in employees if e.get("position") == "supply" and e.get("b24_user_id")})
    if not uids:
        return []
    b24.log("Б24: задачи и история для месячной эффективности снабжения")
    select = ["ID","TITLE","RESPONSIBLE_ID","CREATED_BY","CREATED_DATE","DEADLINE","CLOSED_DATE","STATUS","ACCOMPLICES"]
    tasks = {}
    since = (date_from or dt.date.today()-dt.timedelta(days=180)).isoformat()
    # CHANGED_DATE находит и задачи, из которых сотрудника уже убрали до первой выгрузки.
    for filters in ({"RESPONSIBLE_ID":uids},{"ACCOMPLICE":uids},{">=CHANGED_DATE":since}):
        for t in b24.list_all("tasks.task.list",{"filter":filters,"select":select},key="tasks"):
            tasks[str(t["id"])] = t
    out = []
    items = list(tasks.items())
    for i in range(0,len(items),50):
        chunk = items[i:i+50]
        b24.log(f"Б24: история эффективности задач {min(i+50,len(items))}/{len(items)}")
        response = b24.call("batch",{"halt":0,"cmd":{tid:"tasks.task.history.list?"+urlencode({"taskId":tid}) for tid,_ in chunk}}).get("result") or {}
        results,errors = response.get("result") or {}, response.get("result_error") or {}
        pages = response.get("result_next") or {}
        for tid,t in chunk:
            if tid in errors or tid not in results:
                raise B24Error("Эффективность: история задач получена не полностью")
            history = (results[tid] or {}).get("list")
            if not isinstance(history,list):
                raise B24Error("Эффективность: неверный ответ истории задач")
            if pages.get(tid) is not None or len(history) >= 50:
                history = b24.list_all("tasks.task.history.list",{"taskId":tid},key="list")
            related = int(t.get("responsibleId") or 0) in uids or bool(ids(t.get("accomplices")) & set(uids))
            related = related or any(h.get("field") in ("RESPONSIBLE_ID","ACCOMPLICES") and
                bool((ids((h.get("value") or {}).get("from")) | ids((h.get("value") or {}).get("to"))) & set(uids)) for h in history)
            if related:
                out.append(normalize(t,history,b24))
    return out


def report(con, uid, month, now=None):
    marker = con.execute("SELECT * FROM dataset_state WHERE dataset=?",("task_efficiency:"+str(uid),)).fetchone()
    first,_last = month_bounds(month)
    if not marker or not marker["date_from"] <= first.isoformat() <= marker["date_to"]:
        return None
    result = calculate([dict(r) for r in con.execute("SELECT * FROM task_efficiency_tasks")],int(uid),month,now)
    result["fetched"] = marker["updated_at"]
    return result
