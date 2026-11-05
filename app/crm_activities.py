"""Shared date and CRM-card normalization for calendar entries and planned next steps."""
import datetime as dt
import re

OWNER_TYPES = {"LEAD": 1, "DEAL": 2, "CONTACT": 3, "COMPANY": 4}


def activity_datetime(value, tz=None):
    """Return a naive datetime in the application's local timezone; ignore empty/service dates."""
    s = str(value or "").strip()
    if not s or s[:4] in ("0001", "9999"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                parsed = dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            return None
    if parsed.year in (1, 9999):
        return None
    return parsed.astimezone(tz).replace(tzinfo=None) if parsed.tzinfo else parsed


def activity_deadline(activity, tz=None):
    """Prefer the assigned deadline; scheduled start is a fallback, never the creation date."""
    return activity_datetime(activity.get("DEADLINE"), tz) or activity_datetime(activity.get("START_TIME"), tz)


def activity_bindings(activity):
    """Normalize primary ownership and the additional crm.activity.binding.list response."""
    out = set()
    for binding in [activity, *(activity.get("BINDINGS") or [])]:
        try:
            kind = int(binding.get("OWNER_TYPE_ID") or binding.get("entityTypeId") or binding.get("ENTITY_TYPE_ID") or 0)
            owner = int(binding.get("OWNER_ID") or binding.get("entityId") or binding.get("ENTITY_ID") or 0)
        except (TypeError, ValueError):
            continue
        if kind > 0 and owner > 0:
            out.add((kind, str(owner)))
    return out


def planned_activity(activity):
    """A dated plan, rather than an email or a telephony record."""
    provider = activity.get("PROVIDER_ID")
    kind = str(activity.get("TYPE_ID") or "")
    return bool(activity.get("ID") and provider not in ("VOXIMPLANT_CALL", "CRM_EMAIL")
                and kind != "4" and (provider in ("CRM_TODO", "CRM_TASKS_TASK") or kind in ("1", "2", "3"))
                and activity_deadline(activity))


def call_binding(call):
    bindings = activity_bindings({"OWNER_TYPE_ID": OWNER_TYPES.get(call.get("CRM_ENTITY_TYPE")),
                                  "OWNER_ID": call.get("CRM_ENTITY_ID")})
    return next(iter(bindings), None)


def followup_activity(activity):
    """Any CRM activity except a telephone call or an explicitly named callback."""
    provider = str(activity.get("PROVIDER_ID") or "").upper()
    subject = str(activity.get("SUBJECT") or "").strip()
    callback = re.match(r"^(?:(?:повторный|исходящий|запланированный)\s+)?(?:звонок\b|пере?звон\w*|позвон\w*|созвон\w*|call\b|callback\b)", subject, re.I)
    return bool(activity.get("ID") and str(activity.get("TYPE_ID")) != "2"
                and "CALL" not in provider and not callback)


def is_next_step_for_call(activity, call):
    """A later non-call activity explicitly bound to the call's CRM card."""
    if not followup_activity(activity) or str(activity["ID"]) == str(call.get("CRM_ACTIVITY_ID")):
        return False
    if call_binding(call) not in activity_bindings(activity):
        return False
    start = activity_datetime(call.get("CALL_START_DATE"))
    created = activity_datetime(activity.get("CREATED"))
    return bool(start and created and created > start)
