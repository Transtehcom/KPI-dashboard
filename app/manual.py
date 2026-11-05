"""Ручной ввод руководителя к записям из 1С: данные, которых в 1С нет (срок и дата оплаты реализации,
брак, пересорт, отсрочка, признаки конкурентной закупки).

Значения хранятся в manual_fields отдельно от выгрузки и после каждой синхронизации заново накладываются
на таблицы (apply), поэтому обновление из 1С их не стирает. Если ручное значение удалено, в поле остаётся пусто
до следующей синхронизации — тогда вернётся значение из 1С, если оно там есть.
"""
import datetime as dt
import math

from .supply_delivery import delivery_groups, delivery_key, delivery_overrides, delivery_deviation


def set_month_score(con, emp, month, criterion, score, comment, author):
    """Отдельная оценка за месяц: недельные значения сохраняются для истории."""
    if criterion != "docs":
        raise ValueError("За месяц вручную оценивается только документооборот; задачи — из Б24")
    dt.date.fromisoformat(month + "-01")
    if score is None:
        con.execute("DELETE FROM monthly_scores WHERE emp=? AND month=? AND criterion=?", (emp,month,criterion))
        return
    value = float(score)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Оценка должна быть от 0 до 100")
    con.execute("INSERT OR REPLACE INTO monthly_scores VALUES(?,?,?,?,?,?,datetime('now'))",
                (emp,month,criterion,value,str(comment or "")[:500],author))


def client_approvals(con, emp, month):
    return {r["ref"]: r["approved"] for r in con.execute(
        "SELECT ref,approved FROM client_approvals WHERE emp=? AND month=?", (emp,month))}


def set_client_approval(con, emp, month, ref, value, author):
    if value is not None and (type(value) is not int or value not in (0,1)):
        raise ValueError("Согласование должно быть «Да», «Нет» или пустым")
    exists = con.execute("SELECT 1 FROM clients WHERE emp=? AND id=? UNION ALL "
                         "SELECT 1 FROM clients_monthly WHERE emp=? AND id=? AND month=? LIMIT 1",
                         (emp,ref,emp,ref,month)).fetchone()
    if not exists:
        raise ValueError("Клиент не найден у выбранного сотрудника")
    if value is None:
        con.execute("DELETE FROM client_approvals WHERE emp=? AND month=? AND ref=?", (emp,month,ref))
    else:
        con.execute("INSERT OR REPLACE INTO client_approvals VALUES (?,?,?,?,?,datetime('now'))",
                    (emp,month,ref,value,author))


def lead_approvals(con, emp, month):
    return {r["ref"]: r["approved"] for r in con.execute(
        "SELECT ref,approved FROM lead_approvals WHERE emp=? AND month=?", (emp,month))}


def set_lead_approval(con, emp, month, ref, value, author):
    if value is not None and (type(value) is not int or value not in (0,1)):
        raise ValueError("Согласование должно быть «Да», «Нет» или пустым")
    if not con.execute("SELECT 1 FROM leads WHERE emp=? AND id=?", (emp,ref)).fetchone():
        raise ValueError("Лид не найден у выбранного сотрудника")
    if value is None:
        con.execute("DELETE FROM lead_approvals WHERE emp=? AND month=? AND ref=?", (emp,month,ref))
    else:
        con.execute("INSERT OR REPLACE INTO lead_approvals VALUES (?,?,?,?,?,datetime('now'))",
                    (emp,month,ref,value,author))

# таблица → поле → (тип, подпись)
FIELDS = {
    "receivables": {"due_date": ("date", "Срок оплаты"), "paid_date": ("date", "Оплачено полностью")},
    "sales": {
        "due_date": ("date", "Срок оплаты"),
        "paid_date": ("date", "Оплачено полностью"),
    },
    "supplier_orders": {
        "planned_date": ("date", "Плановая дата поставки"),
        "customer_actual_date": ("date", "Фактическая дата поставки"),
        "defect": ("bool", "Брак"),
        "defect_fixed_date": ("date", "Дата исправления брака"),
        "mis_sort": ("bool", "Пересорт"),
        "deferred_share": ("pct", "Отсрочка, % суммы"),
        "deferral_days": ("int", "Отсрочка, дней"),
        "lot": ("text", "Лот"),
        "kp_count": ("int", "Поставщиков в заявке снабжения"),
        "relevant": ("bool", "КП по ТЗ"),
        "evidence": ("bool", "Сканы и сравн. таблица"),
        "single_source": ("bool", "Монополия подтверждена письмом"),
        "emergency": ("bool", "Аварийная"),
        "justified": ("bool", "Утверждённая служебная записка"),
        "affiliated": ("bool", "Аффилированность"),
    },
}

# пункт KPI → таблица и поля, которые вводятся в его карточке
ENTRY = {
    "receivables_pm": ("sales", ["due_date", "paid_date"]),
    "receivables_overdue": ("sales", ["due_date", "paid_date"]),  # с октября 2026 — у менеджера ОП и тендерного отдела
    "on_time_delivery": ("supplier_orders", ["planned_date", "defect", "defect_fixed_date"]),
    "deferred_payment": ("supplier_orders", ["deferred_share", "deferral_days"]),
    # Для KPI 3 вручную указывается только единый лот. Признаки исключений и служебной записки
    # в карточке KPI не показываются и не редактируются.
    "competitive_purchases": ("supplier_orders", ["lot"]),
}


def parse(tbl, field, raw):
    """Значение из формы → строка для хранения (None — удалить). ValueError при ошибке."""
    kind = FIELDS[tbl][field][0]
    if raw is None or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    if tbl == "receivables" and field == "paid_date" and raw == "unpaid":
        return raw  # Explicitly unpaid differs from removing the manual override.
    if kind == "date":
        try:
            value = dt.date.fromisoformat(raw)
            if value.year < 1900:
                raise ValueError("неполный год")
            return value.isoformat()
        except ValueError:
            raise ValueError("неверная дата: укажите полный год (не ранее 1900)") from None
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
        if r["tbl"] == "receivables":  # KPI-only overlay: preserve the current source date for immediate reset.
            continue
        if r["tbl"] == "supplier_orders" and r["field"] in ("customer_actual_date", "planned_date"):
            continue  # План — из 1С; ручной факт применяется на уровне заказа клиента.
        if r["tbl"] in FIELDS and r["field"] in FIELDS[r["tbl"]]:
            con.execute(f"UPDATE {r['tbl']} SET {r['field']}=? WHERE id=?",
                        (_typed(r["tbl"], r["field"], r["value"]), r["ref"]))


def set_value(con, tbl, ref, field, raw, author):
    """Сохранить ручное значение. Возвращает сотрудника, к которому относится запись. Транзакцию фиксирует вызывающий."""
    if tbl not in FIELDS or field not in FIELDS[tbl]:
        raise ValueError("Это поле нельзя вводить вручную")
    if tbl == "supplier_orders" and field == "planned_date":
        raise ValueError("Плановая дата берётся из конечной даты поставки заказа клиента в 1С")
    row = con.execute(f"SELECT * FROM {tbl} WHERE id=?", (ref,)).fetchone()
    if not row:
        raise ValueError("Запись не найдена, обновите страницу")
    value = parse(tbl, field, raw)
    source_overlay = tbl == "supplier_orders" and field == "customer_actual_date"
    if source_overlay:
        ref = "delivery:" + delivery_key(dict(row))
    if value is None:
        con.execute("DELETE FROM manual_fields WHERE tbl=? AND ref=? AND field=?", (tbl, ref, field))
        if tbl != "receivables" and not source_overlay:
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
    onec = con.execute("SELECT id,dt,amount,reason FROM penalties WHERE emp=? AND substr(dt,1,7)=? ORDER BY dt", (emp, month)).fetchall()
    return {"month": month, "rows": [dict(r) for r in rows], "onec_rows": [dict(r) for r in onec], "checked": bool(checked)}


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
        if code == "on_time_delivery":
            rows = con.execute("SELECT * FROM supplier_orders WHERE emp=? AND planned_date BETWEEN ? AND ? "
                               "ORDER BY dt", (emp, first, last)).fetchall()
        else:  # отсрочка и конкурентность считаются по заказам, размещённым в месяце
            rows = con.execute("SELECT * FROM supplier_orders WHERE emp=? AND dt BETWEEN ? AND ? ORDER BY dt",
                               (emp, first, last)).fetchall()
        columns = ["Заказ", "Дата", "Поставщик", "Сумма без НДС, ₽"]
        cells = [[r["doc"], r["dt"], r["supplier"], r["amount_novat"]] for r in rows]
    manual = {}
    for m in con.execute("SELECT ref, field FROM manual_fields WHERE tbl=?", (tbl,)):
        manual.setdefault(m["ref"], set()).add(m["field"])
    result = {
        "table": tbl, "columns": columns,
        "fields": [{"key": f, "type": FIELDS[tbl][f][0], "title": FIELDS[tbl][f][1]} for f in fields],
        "rows": [{"ref": r["id"], "cells": c, "values": {f: r[f] for f in fields},
                  "manual": sorted(manual.get(r["id"], set()) & set(fields))} for r, c in zip(rows, cells)],
    }
    if code == "on_time_delivery":
        result["fields"].insert(1, {"key": "customer_actual_date", "type": "date",
                                    "title": "Фактическая дата поставки", "parent_edit": True})
        result["fields"].insert(2, {"key": "delivery_delta", "type": "delivery_delta",
                                    "title": "Просрочка, дн.", "readonly": True})
        result["fields"][0]["readonly"] = True
        next(f for f in result["fields"] if f["key"] == "defect_fixed_date")["visible_if"] = "defect"
        # Все связанные заказы остаются видны, даже если дата одного из них вне месяца.
        all_rows = con.execute("SELECT * FROM supplier_orders WHERE emp=? ORDER BY dt,doc,id", (emp,)).fetchall()
        groups, result["rows"] = [], []
        for group in delivery_groups(all_rows, delivery_overrides(con)):
            if not group["planned_date"] or not first <= group["planned_date"] <= last:
                continue
            group["delivery_delta"] = delivery_deviation(group, dt.date.today())
            children = [{"ref": r["id"], "cells": [r["doc"], r["supplier"], r["amount_novat"]],
                         "values": {**{f["key"]: r.get(f["key"]) for f in result["fields"]},
                                    "customer_actual_date": group["customer_actual_date"],
                                    "delivery_delta": group["delivery_delta"]},
                         "manual": sorted(manual.get(r["id"], set()) & set(fields))} for r in group["rows"]]
            groups.append({"ref": group["ref"], "edit_ref": children[0]["ref"],
                           "source_values": {"customer_actual_date": group["source_actual_date"]},
                           "manual": ["customer_actual_date"] if group["manual_actual"] else [],
                           "cells": [group["doc"],
                           f"Заказов поставщикам: {len(children)}", group["amount_novat"]],
                           "values": {f["key"]: group.get(f["key"]) for f in result["fields"]}, "rows": children})
            result["rows"].extend(children)
        result["groups"] = groups
        result["columns"] = ["Заказ клиента", "Заказы поставщикам", "Сумма без НДС, ₽"]
    return result
