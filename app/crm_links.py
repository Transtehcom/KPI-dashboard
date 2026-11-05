"""Validated navigation to explicit Bitrix identities, independent of display labels."""
import re
from urllib.parse import urlsplit


def portal_origin(config):
    cfg = config.get("bitrix24", config) or {}
    try:
        parsed = urlsplit(cfg.get("webhook") or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname
        host = f"[{host}]" if ":" in host else host
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    except (ValueError, TypeError, AttributeError):
        return None


def _id(value, zero=False):
    value = str(value or "0")
    return str(int(value)) if re.fullmatch(r"[0-9]{1,20}", value) and (zero or int(value) > 0) else None


def object_url(config, kind, identity, user_id=0):
    origin, identity = portal_origin(config), _id(identity)
    kind = str(kind).lower()
    if not origin or not identity:
        return None
    if kind in ("lead", "deal", "company", "contact"):
        return f"{origin}/crm/{kind}/details/{identity}/"
    if kind == "user":
        return f"{origin}/company/personal/user/{identity}/"
    if kind == "group":
        return f"{origin}/workgroups/group/{identity}/"
    if kind == "task":
        owner = _id(user_id, zero=True)
        return f"{origin}/company/personal/user/{owner}/tasks/task/view/{identity}/" if owner else None
    if kind == "activity":
        return f"{origin}/crm/activity/?id={identity}"
    if kind == "event":
        owner = _id(user_id)
        return f"{origin}/company/personal/user/{owner}/calendar/?EVENT_ID={identity}" if owner else None
    return None


def reference_url(config, reference):
    kind, _, identity = str(reference or "").partition(":")
    return object_url(config, kind, identity) if kind.lower() in ("lead", "deal", "company", "contact") else None


def employee_url(config, login):
    employee = next((e for e in config.get("employees", []) if e["login"] == login), {})
    return object_url(config, "user", employee.get("b24_user_id"))


def activity_url(config, identity, owner=None):
    base, identity = reference_url(config, owner), _id(identity)
    if base and identity:
        return f"{base}?activity_id={identity}"
    return object_url(config, "activity", identity) if identity else base


def client_url(config, row):
    row = dict(row)
    return reference_url(config, row.get("client_ref"))


def record_url(config, table, row):
    row = dict(row)
    identity = str(row.get("id") or "")
    if table in ("activities", "call_records"):
        owner = row.get("contact") or row.get("crm_entity")
        match = re.fullmatch(r"act([0-9]+)", identity)
        activity = row.get("crm_activity_id") or (match.group(1) if match else None)
        return activity_url(config, activity, owner)
    if table == "kp_letters":
        match = re.fullmatch(r"mail([0-9]+)", identity)
        return activity_url(config, match.group(1), "DEAL:" + str(row.get("deal"))) if match else None
    types = {"leads": ("lead", "lead"), "clients": ("co", "company"), "tenders": ("deal", "deal"),
             "deals": ("deal", "deal"), "deal_failures": ("fail", "deal"), "tasks": ("task", "task"),
             "crm_deals": (str(row.get("entity") or "deal"), str(row.get("entity") or "deal")),
             "fact_records": (str(row.get("entity") or "deal"), str(row.get("entity") or "deal"))}
    if table not in types:
        return None
    prefix, kind = types[table]
    match = re.fullmatch(re.escape(prefix) + r"([0-9]+)", identity)
    employee = next((e for e in config.get("employees", []) if e["login"] == row.get("emp")), {})
    return object_url(config, kind, match.group(1), employee.get("b24_user_id") or 0) if match else None
