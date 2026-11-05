"""Расписание сотрудника на один день из Битрикс24 — как раздел «Календарь → Расписание».

В расписание попадают:
- события календаря сотрудника (calendar.event.get; вебхуку нужно право «Календарь» — calendar);
- задачи, где он ответственный или соисполнитель: задача видна в каждый день от начала работы над ней
  (или даты создания) до крайнего срока, если к этому дню она ещё не закрыта.
- CRM-дела с назначенной датой из лидов, сделок, контактов и компаний — в день срока выполнения.
Данные читаются из Битрикс24 при открытии, а не при синхронизации; ответ кэшируется на минуту.
"""
import datetime as dt
import re
import threading
import time

from .bitrix24 import B24Error, Bitrix24
from .crm_activities import activity_bindings, activity_datetime, activity_deadline
from .crm_links import portal_origin, object_url, activity_url

CACHE_SECONDS = 60
TASK_STATUS = {"1": "новая", "2": "ждёт выполнения", "3": "выполняется", "4": "ждёт контроля", "5": "завершена",
               "6": "отложена", "7": "отклонена"}
_cache, _cache_lock = {}, threading.Lock()


def _parse(v):
    """Дата-время из Б24: «19.10.2026 10:00:00», «2026-10-19T10:00:00+05:00» или «19.10.2026»."""
    s = str(v or "").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        d, mo, y, h, mi, sec = m.groups()
        return dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
    try:
        return dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def _portal(cfg):
    return portal_origin(cfg)


def _events(b24, uid, day, portal=None):
    """События календаря сотрудника, пересекающие день. Без права calendar — B24Error insufficient_scope."""
    rows = b24.call("calendar.event.get", {"type": "user", "ownerId": uid,
                                           "from": day.isoformat(), "to": day.isoformat()}).get("result") or []
    start, end = dt.datetime.combine(day, dt.time()), dt.datetime.combine(day, dt.time(23, 59, 59))
    out = []
    for e in rows:
        f, t = _parse(e.get("DATE_FROM")), _parse(e.get("DATE_TO"))
        if not f:
            continue
        t = t or f
        all_day = e.get("DT_SKIP_TIME") == "Y"
        if all_day:
            t = dt.datetime.combine(t.date(), dt.time(23, 59, 59))
        if t < start or f > end:
            continue
        private = e.get("PRIVATE_EVENT") in ("Y", True, "1") and str(e.get("OWNER_ID")) != str(uid)
        out.append({
            "kind": "event", "title": "Занят(а)" if private else (e.get("NAME") or "Событие"),
            "url": object_url({"webhook": portal},"event",e.get("ID"),uid) if not private and portal else None,
            "all_day": all_day or (f <= start and t >= end),
            "from": f.strftime("%H:%M") if f.date() == day and not all_day else None,
            "to": t.strftime("%H:%M") if t.date() == day and not all_day else None,
            # «calendar_5» и подобное — внутренний код переговорной Б24, а не адрес
            "location": None if private or re.match(r"calendar_\d", str(e.get("LOCATION") or "")) else (e.get("LOCATION") or None),
            "sort": "" if all_day else f.strftime("%H:%M") if f.date() == day else "00:00",
        })
    return out


def _tasks(b24, uid, day, portal):
    """Задачи сотрудника (ответственный или соисполнитель), которые в этот день были в работе."""
    start, end = dt.datetime.combine(day, dt.time()), dt.datetime.combine(day, dt.time(23, 59, 59))
    select = ["ID", "TITLE", "STATUS", "CREATED_DATE", "DATE_START", "START_DATE_PLAN", "DEADLINE", "CLOSED_DATE",
              "RESPONSIBLE_ID"]
    base = {"<=CREATED_DATE": end.isoformat(), ">=DEADLINE": start.isoformat()}
    found = {}
    for role in ("RESPONSIBLE_ID", "ACCOMPLICE"):
        for t in b24.list_all("tasks.task.list", {"filter": {**base, role: uid}, "select": select}, key="tasks"):
            found[str(t["id"])] = t
    out = []
    for t in found.values():
        closed = _parse(t.get("closedDate"))
        if closed and closed < start:
            continue
        deadline = _parse(t.get("deadline"))
        began = _parse(t.get("dateStart")) or _parse(t.get("startDatePlan"))
        if not began:  # ещё не начатая задача: как в Б24 — видна с сегодняшнего дня до срока
            began = max(_parse(t.get("createdDate")) or start, dt.datetime.combine(dt.date.today(), dt.time()))
            frm_shown = False
        else:
            frm_shown = True
        if not deadline or began > end:
            continue
        frm = began.strftime("%H:%M") if frm_shown and began.date() == day else None
        due = deadline.strftime("%H:%M") if deadline.date() == day else None
        out.append({
            "kind": "task", "title": t.get("title") or "Задача", "all_day": not (frm or due),
            "from": frm, "to": due, "status": TASK_STATUS.get(str(t.get("status")), ""),
            "done": bool(closed), "role": "ответственный" if str(t.get("responsibleId")) == str(uid) else "соисполнитель",
            "url": f"{portal}/company/personal/user/{uid}/tasks/task/view/{t['id']}/" if portal else None,
            "sort": frm or (due if due else ""),
        })
    return out


def _crm_activities(b24, uid, day, portal):
    """Dated CRM_TODO entries, once per activity ID regardless of the number of bound cards."""
    start, end = dt.datetime.combine(day, dt.time()), dt.datetime.combine(day, dt.time(23, 59, 59))
    select = ["ID", "SUBJECT", "OWNER_TYPE_ID", "OWNER_ID", "RESPONSIBLE_ID", "PROVIDER_ID", "TYPE_ID",
              "CREATED", "DEADLINE", "START_TIME", "COMPLETED"]
    found = {}
    # Two simple REST filters also cover entries whose deadline is empty but start time is assigned.
    for field in ("DEADLINE", "START_TIME"):
        for row in b24.list_all("crm.activity.list", {"filter": {"RESPONSIBLE_ID": uid, "PROVIDER_ID": "CRM_TODO",
                ">=" + field: start.astimezone().isoformat(), "<=" + field: end.astimezone().isoformat()}, "select": select}):
            found[str(row["ID"])] = row
    types = {1: ("lead", "Лид"), 2: ("deal", "Сделка"), 3: ("contact", "Контакт"), 4: ("company", "Компания")}
    out = []
    for row in found.values():
        due = activity_deadline(row)
        if (row.get("PROVIDER_ID") != "CRM_TODO" or str(row.get("RESPONSIBLE_ID")) != str(uid)
                or not due or due.date() != day):
            continue
        bindings = sorted(activity_bindings(row))
        primary = (int(row.get("OWNER_TYPE_ID") or 0), str(row.get("OWNER_ID") or ""))
        binding = primary if primary in bindings else next((b for b in bindings if b[0] in types), None)
        if not binding or binding[0] not in types:
            continue
        kind, label = types[binding[0]]
        created = activity_datetime(row.get("CREATED"))
        if created and created > end:
            continue
        done = row.get("COMPLETED") in ("Y", True, "1")
        out.append({"id": str(row["ID"]), "kind": "activity", "title": row.get("SUBJECT") or "Дело CRM",
                    "all_day": False, "from": None, "to": due.strftime("%H:%M"), "done": done,
                    "status": "выполнено" if done else "запланировано", "card": f"{label} №{binding[1]}",
                    "url": activity_url({"webhook": portal},row["ID"],kind.upper()+":"+binding[1]) if portal else None,
                    "card_url": object_url({"webhook": portal},kind,binding[1]) if portal else None, "sort": due.strftime("%H:%M")})
    return out


def day_schedule(cfg, uid, day):
    """Расписание пользователя Б24 uid на дату day: {items, calendar_error}. Кэш — минута."""
    key = (cfg.get("webhook"), uid, day.isoformat())
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    b24 = Bitrix24(cfg, log=lambda *a: None)
    items, errors = [], {}
    for name, load in (("calendar_error", lambda: _events(b24, uid, day, _portal(cfg))),
                       ("tasks_error", lambda: _tasks(b24, uid, day, _portal(cfg))),
                       ("activities_error", lambda: _crm_activities(b24, uid, day, _portal(cfg)))):
        try:
            items += load()
            errors[name] = None
        except B24Error as e:
            errors[name] = "scope" if "insufficient_scope" in str(e) else str(e)[:200]
    # сначала «весь день», потом по времени
    items.sort(key=lambda x: (not x["all_day"], x["sort"] or "", x["title"]))
    for x in items:
        x.pop("sort", None)
    res = {"items": items, **errors}
    with _cache_lock:
        _cache[key] = (time.monotonic(), res)
    return res
