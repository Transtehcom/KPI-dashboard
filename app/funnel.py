"""Воронки сделок и лидов: текущий срез открытых записей CRM, разложенный по секциям из config.deal_funnel / lead_funnel."""
import datetime as dt

DEFAULT_SECTIONS = {
    "deals": [
        {"key": "work", "title": "В работе", "all_open": True},
        {"key": "precontract", "title": "Преддоговорные сделки", "stages": ["PREPARATION", "PREPAYMENT_INVOICE"]},
        {"key": "contract", "title": "Договорные сделки", "stages": ["EXECUTING", "FINAL_INVOICE"]},
    ],
    "leads": [
        {"key": "new", "title": "Распределено", "stages": ["NEW"]},
        {"key": "work", "title": "В работе", "stages": ["IN_PROCESS"]},
        {"key": "processed", "title": "Обработано", "stages": ["PROCESSED"]},
    ],
}
# вид воронки → значение crm_deals.entity и блок настроек в config.json
KINDS = {"deals": ("deal", "deal_funnel"), "leads": ("lead", "lead_funnel")}
# по какой дате отбирать записи за период → столбец crm_deals
DATE_FIELDS = {"created": "created", "stage": "stage_since"}


def _days(since, now):
    try:
        return max(0, (now - dt.datetime.fromisoformat(str(since)[:19])).days)
    except (TypeError, ValueError):
        return None


def deal_funnel(con, config, login=None, category=None, kind="deals", date_from=None, date_to=None, date_field="created"):
    """date_from / date_to (ГГГГ-ММ-ДД, включительно) оставляют в срезе записи, у которых дата date_field попадает
    в период: created — дата создания, stage — дата перехода на текущую стадию."""
    entity, cfg_key = KINDS[kind]
    fcfg = config.get(cfg_key, {})
    sections_cfg = fcfg.get("sections") or DEFAULT_SECTIONS[kind]
    all_rows = [dict(r) for r in con.execute("SELECT * FROM crm_deals WHERE COALESCE(entity, 'deal') = ?", (entity,))]
    if not fcfg.get("include_closed"):
        all_rows = [r for r in all_rows if r["semantic"] not in ("S", "F")]
    snap = max((r["snapshot"] for r in all_rows if r["snapshot"]), default=None)

    # варианты фильтров — по всему срезу (чтобы выбранный вариант не пропадал из списка), счётчики — по периоду
    resp = {}
    for r in all_rows:
        key = r["emp"] or ("id:" + (r["responsible_id"] or ""))
        resp.setdefault(key, {"value": key, "name": r["responsible"] or "Без ответственного", "count": 0})
    cats = {}
    for r in all_rows:
        cats.setdefault(r["category_id"], {"value": r["category_id"], "name": r["category"], "count": 0})

    if date_from or date_to:
        col = DATE_FIELDS[date_field]
        all_rows = [r for r in all_rows if r[col] and (not date_from or r[col][:10] >= date_from)
                    and (not date_to or r[col][:10] <= date_to)]
    for r in all_rows:
        resp[r["emp"] or ("id:" + (r["responsible_id"] or ""))]["count"] += 1
        cats[r["category_id"]]["count"] += 1

    rows = all_rows
    if login:
        rows = [r for r in rows if (r["emp"] or ("id:" + (r["responsible_id"] or ""))) == login]
    if category not in (None, ""):
        rows = [r for r in rows if r["category_id"] == str(category)]

    now = dt.datetime.now()
    sections, matched_any, in_funnel = [], set(), set()
    for sc in sections_cfg:
        ids = set(sc.get("stages") or [])
        names = {n.strip().lower() for n in sc.get("stage_names") or []}
        only_cats = {str(c) for c in sc.get("category_ids") or []}  # секция только по этим воронкам Б24
        sel = [r for r in rows if not only_cats or r["category_id"] in only_cats]
        if not sc.get("all_open"):
            sel = [r for r in sel if r["stage_id"] in ids or (r["stage"] or "").strip().lower() in names]
            matched_any |= {r["id"] for r in sel}
        in_funnel |= {r["id"] for r in sel}
        by_stage, by_resp = {}, {}
        for r in sel:
            k = (r["category"], r["stage"])
            s = by_stage.setdefault(k, {"category": r["category"], "stage": r["stage"], "sort": r["stage_sort"] or 0, "count": 0, "amount": 0})
            s["count"] += 1
            s["amount"] += r["amount"] or 0
            rk = r["emp"] or r["responsible_id"]
            p = by_resp.setdefault(rk, {"name": r["responsible"] or "Без ответственного", "login": r["emp"],
                                        "value": r["emp"] or ("id:" + (r["responsible_id"] or "")), "count": 0, "amount": 0})
            p["count"] += 1
            p["amount"] += r["amount"] or 0
        ages = [a for a in (_days(r["stage_since"], now) for r in sel) if a is not None]
        deals = sorted(sel, key=lambda r: -(r["amount"] or 0))
        sections.append({
            "key": sc.get("key"), "title": sc.get("title"), "all_open": bool(sc.get("all_open")),
            "rule": "все открытые" if sc.get("all_open") else
                    "стадии: " + ", ".join(list(sc.get("stage_names") or []) + list(sc.get("stages") or [])),
            "count": len(sel), "amount": sum(r["amount"] or 0 for r in sel),
            "avg_days_on_stage": round(sum(ages) / len(ages)) if ages else None,
            "by_stage": sorted(by_stage.values(), key=lambda x: (x["category"], x["sort"])),
            "by_responsible": sorted(by_resp.values(), key=lambda x: (-x["amount"], -x["count"])),
            "deals": [{"title": r["title"], "client": r["client"], "responsible": r["responsible"], "category": r["category"],
                       "stage": r["stage"], "amount": r["amount"], "days": _days(r["stage_since"], now)} for r in deals[:300]],
        })
    first = sections[0]["count"] if sections else 0
    for i, s in enumerate(sections):
        s["share_of_first"] = s["count"] / first if first else None
        prev = sections[i - 1]["count"] if i else None
        s["share_of_prev"] = s["count"] / prev if prev else None
        s["share_of_total"] = s["count"] / len(in_funnel) if in_funnel else None

    other = {}
    for r in rows:
        if r["id"] not in matched_any:
            other.setdefault((r["category"], r["stage"]), 0)
            other[(r["category"], r["stage"])] += 1
    return {
        "kind": kind,
        "config_key": cfg_key,
        "snapshot": snap,
        "sections": sections,
        "total": len(in_funnel),
        "other_stages": [{"category": c, "stage": s, "count": n} for (c, s), n in sorted(other.items(), key=lambda x: -x[1])],
        "filters": {
            "responsibles": sorted(resp.values(), key=lambda x: x["name"]),
            "categories": sorted(cats.values(), key=lambda x: x["name"]),
        },
        "applied": {"login": login or "", "category": "" if category is None else str(category),
                    "date_from": date_from or "", "date_to": date_to or "", "date_field": date_field},
    }
