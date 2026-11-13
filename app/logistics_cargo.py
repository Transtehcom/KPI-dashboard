"""Monthly manager-entered cargo incidents, independent of source synchronization."""
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CRITERIA = (
    ("damage", "Повреждения груза"),
    ("loss", "Утрата или недостача"),
    ("moisture", "Намокание, загрязнение или коррозия"),
    ("deformation", "Деформация металлоконструкций"),
    ("packaging", "Повреждение упаковки"),
    ("securing", "Неверное крепление"),
    ("temperature", "Нарушение температурного режима"),
    ("repair", "Затраты на ремонт и переупаковку"),
    ("repeat_transport", "Срочная повторная перевозка"),
    ("claims", "Претензии заказчика по транспортным причинам"),
)


def report(con, emp, month, safety_target=.95):
    saved = {r["criterion"]: dict(r) for r in con.execute(
        "SELECT * FROM logistics_cargo_checks WHERE emp=? AND month=?", (emp, month))}
    rows = []
    for criterion, title in CRITERIA:
        row = saved.get(criterion, {})
        rows.append({"criterion": criterion, "title": title, "cases": row.get("cases"),
                     "damage": row.get("damage"), "comment": row.get("comment") or ""})
    cases = [r["cases"] for r in rows if r["cases"] is not None]
    damage = [Decimal(str(r["damage"])) for r in rows if r["damage"] is not None]
    summary = con.execute("SELECT cargo_value,comment FROM logistics_cargo_summary WHERE emp=? AND month=?",
                          (emp, month)).fetchone()
    cargo_value = summary["cargo_value"] if summary else None
    auto_score, safety_share = None, None
    if len(damage) != len(CRITERIA):
        reason = "Заполните ущерб по всем 10 критериям, включая нулевые значения"
    elif cargo_value is None or cargo_value <= 0:
        reason = "Укажите стоимость перевезённых грузов больше нуля"
    else:
        safety = max(Decimal(0), Decimal(1) - sum(damage) / Decimal(str(cargo_value)))
        safety_share = float(safety)
        completion = min(Decimal(100), safety / Decimal(str(safety_target)) * 100)
        # The dashboard rounds to tenths first, then to whole completion percent.
        auto_score = float(completion.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                           .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        reason = ""
    return {"rows": rows, "entered": sum(r["cases"] is not None or r["damage"] is not None or bool(r["comment"]) for r in rows),
            "cases_total": sum(cases) if cases else None,
            "damage_total": float(sum(damage)) if damage else None,
            "cargo_value": cargo_value, "comment": summary["comment"] if summary else "",
            "auto_score": auto_score, "safety_share": safety_share, "safety_target": safety_target, "auto_reason": reason}


def _money(value):
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Укажите неотрицательную сумму в рублях") from None


def save_summary(con, emp, month, cargo_value, comment, author):
    dt.date.fromisoformat(month + "-01")
    value = _money(cargo_value)
    con.execute("INSERT OR REPLACE INTO logistics_cargo_summary VALUES(?,?,?,?,?,datetime('now'))",
                (emp, month, value, str(comment or "")[:500], author))


def save(con, emp, month, entries, author):
    dt.date.fromisoformat(month + "-01")
    if not isinstance(entries, list) or len(entries) != len(CRITERIA):
        raise ValueError("Передайте все 10 критериев KPI 4")
    allowed = dict(CRITERIA)
    seen, validated = set(), []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Некорректная строка критерия")
        criterion = entry.get("criterion")
        if not isinstance(criterion, str) or criterion not in allowed or criterion in seen:
            raise ValueError("Неизвестный или повторяющийся критерий KPI 4")
        seen.add(criterion)
        cases = entry.get("cases")
        if cases is not None and (type(cases) is not int or not 0 <= cases <= 9223372036854775807):
            raise ValueError("Количество случаев должно быть целым неотрицательным числом")
        damage = _money(entry.get("damage"))
        comment = str(entry.get("comment") or "")[:500]
        validated.append((criterion, cases, damage, comment))
    for criterion, cases, damage, comment in validated:
        if cases is None and damage is None and not comment:
            con.execute("DELETE FROM logistics_cargo_checks WHERE emp=? AND month=? AND criterion=?",
                        (emp, month, criterion))
        else:
            con.execute("INSERT OR REPLACE INTO logistics_cargo_checks VALUES(?,?,?,?,?,?,?,datetime('now'))",
                        (emp, month, criterion, cases, damage, comment, author))
