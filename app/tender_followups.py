"""Tender and sales call follow-ups and deadline changes observed during refreshes."""
import datetime as dt
import json

from .crm_activities import activity_bindings, activity_datetime, activity_deadline


def assigned_activity(activity):
    provider = str(activity.get("PROVIDER_ID") or "").upper()
    return bool(activity.get("ID") and str(activity.get("TYPE_ID")) != "2" and "CALL" not in provider
                and (provider in ("CRM_TODO", "CRM_TASKS_TASK") or str(activity.get("TYPE_ID")) in ("1", "3"))
                and activity_datetime(activity.get("CREATED")))


def normalize(activity):
    deadline = activity_deadline(activity)
    return {"id": str(activity["ID"]), "created": activity_datetime(activity["CREATED"]).isoformat(),
            "deadline": deadline.isoformat() if deadline else None,
            "completed": int(str(activity.get("COMPLETED") or "").upper() == "Y"),
            "subject": activity.get("SUBJECT") or "Дело",
            "companies": json.dumps(sorted(owner for kind, owner in activity_bindings(activity) if kind == 4))}


def store(con, rows, date_from):
    """Apply a complete creation-date window atomically with the surrounding sync."""
    previous = {r["id"]: dict(r) for r in con.execute("SELECT * FROM crm_followups")}
    con.execute("UPDATE crm_followups SET present=0 WHERE created>=?", (date_from,))
    observed_at = dt.datetime.now().isoformat()
    for row in rows:
        old = previous.get(row["id"], {})
        last_deadline = old.get("last_deadline") or old.get("deadline")
        changed = bool(last_deadline and row["deadline"] and last_deadline != row["deadline"])
        con.execute("INSERT INTO crm_followups(id,created,deadline,completed,subject,companies,moves,last_deadline,observed_at,present) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                    "created=excluded.created,deadline=excluded.deadline,completed=excluded.completed,subject=excluded.subject,"
                    "companies=excluded.companies,moves=excluded.moves,last_deadline=excluded.last_deadline,observed_at=excluded.observed_at,present=1",
                    (row["id"], row["created"], row["deadline"], row["completed"], row["subject"], row["companies"],
                     int(old.get("moves") or 0) + int(changed), row["deadline"] or last_deadline, observed_at))
    con.execute("INSERT OR REPLACE INTO dataset_state VALUES(?,?,?,?)",
                ("crm_followups", date_from, dt.date.today().isoformat(), observed_at))


def effective_moves(row):
    return row["moves"] if row["override_moves"] is None else row["override_moves"] + row["moves"] - row["override_baseline"]


def set_moves(con, identity, value, author):
    row = con.execute("SELECT moves FROM crm_followups WHERE id=? AND present=1", (identity,)).fetchone()
    if row is None:
        raise ValueError("Дело не найдено в загруженных данных")
    if value is not None:
        if isinstance(value, bool) or not str(value).strip().isdecimal():
            raise ValueError("Количество переносов должно быть целым числом от нуля")
        value = int(str(value).strip())
        if value > 2**63 - 1:
            raise ValueError("Количество переносов превышает допустимое значение")
    con.execute("UPDATE crm_followups SET override_moves=?,override_baseline=?,author=?,manual_updated=? WHERE id=?",
                (value, row["moves"] if value is not None else None, author, dt.datetime.now().isoformat(), identity))


def mark_calls(con, calls, now=None):
    """Each call receives a disjoint status and all matching assigned activities."""
    if not calls:
        return
    now = now or dt.datetime.now()
    starts = [activity_datetime(row.get("call_started_at") or row["dt"]) for row in calls]
    first = min(starts).isoformat()
    last = (max(starts) + dt.timedelta(days=1)).isoformat()
    index = {}
    for source in con.execute("SELECT * FROM crm_followups WHERE present=1 AND created>=? AND created<=?", (first, last)):
        activity = dict(source)
        activity["total_moves"] = effective_moves(activity)
        deadline = activity_datetime(activity["deadline"])
        activity["state"] = ("excluded" if activity["total_moves"] > 5 else "completed" if activity["completed"]
                             else "open" if deadline and deadline >= now else "overdue" if deadline else "undated")
        for company in json.loads(activity["companies"]):
            index.setdefault(company, []).append(activity)
    coverage = con.execute("SELECT date_from,date_to FROM dataset_state WHERE dataset='crm_followups'").fetchone()
    for row in calls:
        start = activity_datetime(row.get("call_started_at") or row["dt"])
        end = start + dt.timedelta(days=1)
        matched = [a for a in index.get(str(row.get("company_id") or ""), [])
                   if start <= activity_datetime(a["created"]) <= end and a["id"] != str(row.get("crm_activity_id"))]
        row["followups"] = matched
        states = {a["state"] for a in matched}
        covered = bool(coverage and coverage["date_from"] <= start.date().isoformat() <= coverage["date_to"]
                       and row.get("company_id") is not None)
        row["followup_status"] = ("unknown" if not covered else "completed" if "completed" in states else "open" if "open" in states
                                  else "overdue" if states & {"excluded", "overdue"} else "missing")
        row["next_step"] = None if not covered else int(bool(states & {"completed", "open"}))
