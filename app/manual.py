"""Ручной ввод руководителя к записям из 1С: данные, которых в 1С нет (срок и дата оплаты реализации,
брак, пересорт, отсрочка, признаки конкурентной закупки).

Значения хранятся в manual_fields отдельно от выгрузки и после каждой синхронизации заново накладываются
на таблицы (apply), поэтому обновление из 1С их не стирает. Если ручное значение удалено, в поле остаётся пусто
до следующей синхронизации — тогда вернётся значение из 1С, если оно там есть.
"""
import datetime as dt

# таблица → поле → (тип, подпись)
FIELDS = {
    "sales": {
        "due_date": ("date", "Срок оплаты"),
        "paid_date": ("date", "Оплачено полностью"),
    },
    "supplier_orders": {
        "planned_date": ("date", "Плановая дата поставки"),
        "defect": ("bool", "Брак"),
        "mis_sort": ("bool", "Пересорт"),
        "deferred_share": ("pct", "Отсрочка, % суммы"),
        "deferral_days": ("int", "Отсрочка, дней"),
        "lot": ("text", "Лот"),
        "kp_count": ("int", "Получено КП"),
        "relevant": ("bool", "КП по ТЗ"),
        "evidence": ("bool", "Сканы и сравн. таблица"),
        "single_source": ("bool", "Монополия"),
        "emergency": ("bool", "Аварийная"),
        "justified": ("bool", "Обоснование без конкурса"),
        "affiliated": ("bool", "Аффилированность"),
    },
}

# пункт KPI → таблица и поля, которые вводятся в его карточке
ENTRY = {
    "receivables_pm": ("sales", ["due_date", "paid_date"]),
    "receivables_overdue": ("sales", ["due_date", "paid_date"]),  # с октября 2026 — у менеджера ОП и тендерного отдела
    "on_time_delivery": ("supplier_orders", ["planned_date", "defect", "mis_sort"]),
    "deferred_payment": ("supplier_orders", ["deferred_share", "deferral_days"]),
    "competitive_purchases": ("supplier_orders", ["lot", "kp_count", "relevant", "evidence", "single_source",
                                                  "emergency", "justified", "affiliated"]),
}


def parse(tbl, field, raw):
    """Значение из формы → строка для хранения (None — удалить). ValueError при ошибке."""
    kind = FIELDS[tbl][field][0]
    if raw is None or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    if kind == "date":
        try:
            return dt.date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError("неверная дата") from None
    if kind == "bool":
        if raw.lower() not in ("1", "0", "true", "false"):
            raise ValueError("ожидается да/нет")
        return "1" if raw.lower() in ("1", "true") else "0"
    if kind == "pct":
        v = float(raw.replace(",", "."))
        if not 0 <= v <= 100:
            raise ValueError("доля от 0 до 100%")
        return str(v / 100)
    if kind == "int":
        v = int(float(raw.replace(",", ".")))
        if v < 0:
            raise ValueError("ожидается число не меньше 0")
        return str(v)
    return raw[:100]


def _typed(tbl, field, value):
    if value is None:
        return None
    kind = FIELDS[tbl][field][0]
    if kind == "bool":
        return int(value)
    if kind == "pct":
        return float(value)
    if kind == "int":
        return int(value)
    return value


def apply(con, tbl=None, ref=None):
    """Наложить ручные значения на таблицы (все или одну запись). Транзакцию фиксирует вызывающий."""
    q, args = "SELECT tbl, ref, field, value FROM manual_fields", []
    if tbl:
        q += " WHERE tbl=? AND ref=?"
        args = [tbl, ref]
    for r in con.execute(q, args).fetchall():
        if r["tbl"] in FIELDS and r["field"] in FIELDS[r["tbl"]]:
            con.execute(f"UPDATE {r['tbl']} SET {r['field']}=? WHERE id=?",
                        (_typed(r["tbl"], r["field"], r["value"]), r["ref"]))


def set_value(con, tbl, ref, field, raw, author):
    """Сохранить ручное значение. Возвращает сотрудника, к которому относится запись. Транзакцию фиксирует вызывающий."""
    if tbl not in FIELDS or field not in FIELDS[tbl]:
        raise ValueError("Это поле нельзя вводить вручную")
    row = con.execute(f"SELECT emp FROM {tbl} WHERE id=?", (ref,)).fetchone()
    if not row:
        raise ValueError("Запись не найдена, обновите страницу")
    value = parse(tbl, field, raw)
    if value is None:
        con.execute("DELETE FROM manual_fields WHERE tbl=? AND ref=? AND field=?", (tbl, ref, field))
        con.execute(f"UPDATE {tbl} SET {field}=NULL WHERE id=?", (ref,))
    else:
        con.execute("INSERT OR REPLACE INTO manual_fields VALUES (?,?,?,?,?,datetime('now'))",
                    (tbl, ref, field, value, author))
        apply(con, tbl, ref)
    return row["emp"]


def penalties_block(con, emp, month):
    """Штрафы сотрудника за месяц и отметка «штрафов нет» — для карточки KPI тендерного отдела."""
    rows = con.execute("SELECT id, dt, amount, reason FROM manual_penalties WHERE emp=? AND substr(dt, 1, 7)=? ORDER BY dt",
                       (emp, month)).fetchall()
    checked = con.execute("SELECT 1 FROM penalty_checks WHERE emp=? AND month=?", (emp, month)).fetchone()
    return {"month": month, "rows": [dict(r) for r in rows], "checked": bool(checked)}


def add_penalty(con, emp, date, amount, reason, author):
    d = parse("sales", "due_date", date)  # та же проверка даты
    if not d:
        raise ValueError("укажите дату")
    try:
        amount = float(str(amount or 0).replace(",", ".").replace(" ", ""))
    except ValueError:
        raise ValueError("неверная сумма") from None
    if amount < 0:
        raise ValueError("сумма не может быть отрицательной")
    con.execute("INSERT INTO manual_penalties (emp, dt, amount, reason, author, updated) VALUES (?,?,?,?,?,datetime('now'))",
                (emp, d, amount, str(reason or "").strip()[:300], author))


def delete_penalty(con, emp, pid):
    if not con.execute("DELETE FROM manual_penalties WHERE id=? AND emp=?", (pid, emp)).rowcount:
        raise ValueError("штраф не найден, обновите страницу")


def set_penalty_check(con, emp, month, checked, author):
    if checked:
        con.execute("INSERT OR REPLACE INTO penalty_checks VALUES (?,?,?,datetime('now'))", (emp, month, author))
    else:
        con.execute("DELETE FROM penalty_checks WHERE emp=? AND month=?", (emp, month))


def entry(con, code, emp, month_first, month_last):
    """Таблица для ввода в карточке пункта KPI: записи сотрудника за месяц и поля, которые можно заполнить."""
    tbl, fields = ENTRY[code]
    first, last = month_first.isoformat(), month_last.isoformat()
    if tbl == "sales":
        # реализации месяца и более ранние, ещё не отмеченные оплаченными
        rows = con.execute("SELECT * FROM sales WHERE emp=? AND (dt BETWEEN ? AND ? OR (dt < ? AND paid_date IS NULL)) "
                           "ORDER BY dt", (emp, first, last + "T23:59:59", first)).fetchall()
        columns = ["Реализация", "Дата", "Клиент", "Выручка без НДС, ₽"]
        cells = [[r["doc"], (r["dt"] or "")[:10], r["client"], r["revenue"]] for r in rows]
    else:
        rows = con.execute("SELECT * FROM supplier_orders WHERE emp=? AND (dt BETWEEN ? AND ? OR planned_date BETWEEN ? AND ?) "
                           "ORDER BY dt", (emp, first, last, first, last)).fetchall()
        columns = ["Заказ", "Дата", "Поставщик", "Сумма без НДС, ₽"]
        cells = [[r["doc"], r["dt"], r["supplier"], r["amount_novat"]] for r in rows]
    manual = {}
    for m in con.execute("SELECT ref, field FROM manual_fields WHERE tbl=?", (tbl,)):
        manual.setdefault(m["ref"], set()).add(m["field"])
    return {
        "table": tbl, "columns": columns,
        "fields": [{"key": f, "type": FIELDS[tbl][f][0], "title": FIELDS[tbl][f][1]} for f in fields],
        "rows": [{"ref": r["id"], "cells": c, "values": {f: r[f] for f in fields},
                  "manual": sorted(manual.get(r["id"], set()) & set(fields))} for r, c in zip(rows, cells)],
    }
