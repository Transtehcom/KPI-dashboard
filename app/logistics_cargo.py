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


def report(con, emp, month):
    saved = {r["criterion"]: dict(r) for r in con.execute(
        "SELECT * FROM logistics_cargo_checks WHERE emp=? AND month=?", (emp, month))}
    rows = []
    for criterion, title in CRITERIA:
        row = saved.get(criterion, {})
        rows.append({"criterion": criterion, "title": title, "cases": row.get("cases"),
                     "damage": row.get("damage"), "comment": row.get("comment") or ""})
    cases = [r["cases"] for r in rows if r["cases"] is not None]
    damage = [Decimal(str(r["damage"])) for r in rows if r["damage"] is not None]
    return {"rows": rows, "entered": sum(r["cases"] is not None or r["damage"] is not None or bool(r["comment"]) for r in rows),
            "cases_total": sum(cases) if cases else None,
            "damage_total": float(sum(damage)) if damage else None}


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
        damage = entry.get("damage")
        if damage is not None:
            try:
                if isinstance(damage, bool):
                    raise ValueError()
                amount = Decimal(str(damage))
                if not amount.is_finite() or amount < 0:
                    raise ValueError()
                damage = float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError("Ущерб должен быть неотрицательной суммой в рублях") from None
        comment = str(entry.get("comment") or "")[:500]
        validated.append((criterion, cases, damage, comment))
    for criterion, cases, damage, comment in validated:
        if cases is None and damage is None and not comment:
            con.execute("DELETE FROM logistics_cargo_checks WHERE emp=? AND month=? AND criterion=?",
                        (emp, month, criterion))
        else:
            con.execute("INSERT OR REPLACE INTO logistics_cargo_checks VALUES(?,?,?,?,?,?,?,datetime('now'))",
                        (emp, month, criterion, cases, damage, comment, author))
