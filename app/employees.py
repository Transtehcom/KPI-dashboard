"""Сотрудники дашборда: хранятся в базе (таблица employees), заводятся и правятся в разделе «Сотрудники».

При первом запуске список переносится из config.json → employees; дальше config.json для сотрудников
не используется. Для расчёта сотрудники отдаются в прежнем формате config["employees"]:
login, name, role (manager — администратор), position (ключ KPI из kpi_rules.json), b24_user_id,
onec_manager, absences, а также department, title, visible (показывать ли в KPI команды)
и listen_dept (слушает записи звонков сотрудников своего отдела).
"""
import datetime as dt
import json
import re

from .common import db_lock

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# категории отсутствия; прогул от норм не освобождает, остальные — освобождают (app/engine.py)
ABSENCE_KINDS = {"vacation": "Отпуск", "sick": "Больничный", "maternity": "Декрет",
                 "leave": "Отпросился(-лась)", "truancy": "Прогул"}


class EmployeeError(ValueError):
    pass


def _from_config(e, rules):
    pos = e.get("position")
    admin = e.get("role") == "manager"
    return {
        "login": e["login"], "name": e.get("name") or e["login"],
        "department": e.get("department") or (rules["positions"][pos]["department"] if pos in rules["positions"] else None),
        "title": e.get("title") or (rules["positions"][pos]["title"] if pos in rules["positions"] else None),
        "position": pos, "b24_user_id": e.get("b24_user_id"), "onec_manager": e.get("onec_manager"),
        "visible": int(e["visible"]) if "visible" in e else int(bool(pos) and not admin),
        "admin": int(admin), "absences": json.dumps(e.get("absences") or [], ensure_ascii=False),
    }


def _to_config(r):
    e = {"login": r["login"], "name": r["name"], "role": "manager" if r["admin"] else "employee",
         "department": r["department"], "title": r["title"], "visible": bool(r["visible"]),
         "listen_dept": bool(r["listen_dept"])}
    if r["position"]:
        e["position"] = r["position"]
    if r["b24_user_id"]:
        e["b24_user_id"] = r["b24_user_id"]
    if r["onec_manager"]:
        e["onec_manager"] = r["onec_manager"]
    absences = json.loads(r["absences"] or "[]")
    if absences:
        e["absences"] = absences
    return e


def load(con, config, rules):
    """Сотрудники из базы; если база пустая — перенести их из config.json. Возвращает список в формате config."""
    with db_lock():
        if not con.execute("SELECT 1 FROM employees LIMIT 1").fetchone() and config.get("employees"):
            for i, e in enumerate(config["employees"]):
                row = _from_config(e, rules)
                con.execute("INSERT OR IGNORE INTO employees (login, name, department, title, position, b24_user_id, "
                            "onec_manager, visible, admin, absences, sort) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (*row.values(), i))
            con.commit()
        rows = con.execute("SELECT * FROM employees ORDER BY sort, name").fetchall()
    return [_to_config(r) for r in rows]


def apply(con, config, rules):
    """Подставить сотрудников из базы в config (тот же словарь, который видят расчёт и синхронизация)."""
    config["employees"] = load(con, config, rules)
    return config["employees"]


def parse_b24_id(value):
    """ID пользователя Битрикс24: число или ссылка на его карточку (…/company/personal/user/56/)."""
    s = str(value or "").strip()
    if not s:
        return None
    m = re.search(r"/user/(\d+)", s) or re.fullmatch(r"\d+", s)
    if not m:
        raise EmployeeError("ID в Битрикс24: укажите число или ссылку на карточку сотрудника (…/company/personal/user/56/)")
    return int(m.group(1) if m.lastindex else m.group(0))


def parse_absences(items):
    """Периоды отсутствия из формы: [{from, to, kind, reason}] → проверенный список, отсортированный по дате начала."""
    if not isinstance(items, list) or len(items) > 200:
        raise EmployeeError("Некорректный список отсутствий")
    out = []
    for n, a in enumerate(items, 1):
        if not isinstance(a, dict):
            raise EmployeeError("Некорректный список отсутствий")
        try:
            d_from = dt.date.fromisoformat(str(a.get("from") or ""))
            d_to = dt.date.fromisoformat(str(a.get("to") or a.get("from")))
        except ValueError:
            raise EmployeeError(f"Отсутствие {n}: укажите даты «с» и «по»")
        if d_to < d_from:
            raise EmployeeError(f"Отсутствие {n}: дата «по» раньше даты «с»")
        if a.get("kind") not in ABSENCE_KINDS:
            raise EmployeeError(f"Отсутствие {n}: выберите категорию")
        row = {"from": d_from.isoformat(), "to": d_to.isoformat(), "kind": a["kind"]}
        reason = " ".join(str(a.get("reason") or "").split())[:200]
        if reason:
            row["reason"] = reason
        out.append(row)
    return sorted(out, key=lambda x: (x["from"], x["to"]))


def listing(con, rules):
    """Сотрудники для раздела «Сотрудники» (без паролей) и варианты для выпадающих списков."""
    with db_lock():
        rows = [dict(r) for r in con.execute("SELECT * FROM employees ORDER BY sort, name")]
        has_pwd = {r["login"] for r in con.execute("SELECT login FROM users")}
    for r in rows:
        r["visible"], r["admin"], r["listen_dept"] = bool(r["visible"]), bool(r["admin"]), bool(r["listen_dept"])
        r["has_password"] = r["login"] in has_pwd
        r["absences"] = json.loads(r["absences"] or "[]")
        r.pop("sort", None)
    departments = sorted({p["department"] for p in rules["positions"].values()} | {r["department"] for r in rows if r["department"]})
    return {
        "employees": rows,
        "departments": [{"key": d, "title": rules.get("departments", {}).get(d, d)} for d in departments],
        "positions": [{"key": k, "title": p["title"], "department": p["department"], "doc": p.get("doc")}
                      for k, p in rules["positions"].items()],
        "absence_kinds": [{"key": k, "title": t} for k, t in ABSENCE_KINDS.items()],
    }


def save(con, rules, data, me, is_new, hash_password):
    """Создать или изменить сотрудника. Проверяет поля и не даёт администратору лишить прав самого себя."""
    login = str(data.get("login") or "").strip().lower()
    if not LOGIN_RE.match(login):
        raise EmployeeError("Логин: 2–32 символа — латинские буквы, цифры, точка, дефис или подчёркивание")
    name = " ".join(str(data.get("name") or "").split())
    if not name or len(name) > 100:
        raise EmployeeError("Укажите имя и фамилию")
    department = " ".join(str(data.get("department") or "").split())[:60] or None
    title = " ".join(str(data.get("title") or "").split())[:100] or None
    position = data.get("position") or None
    if position and position not in rules["positions"]:
        raise EmployeeError("Неизвестный KPI")
    visible, admin, listen_dept = bool(data.get("visible")), bool(data.get("admin")), bool(data.get("listen_dept"))
    if login == me and not admin:
        raise EmployeeError("Нельзя снять права администратора с самого себя")
    b24 = parse_b24_id(data.get("b24_user_id"))
    onec = " ".join(str(data.get("onec_manager") or "").split())[:150] or None
    password = str(data.get("password") or "")
    if (is_new or password) and len(password) < 6:
        raise EmployeeError("Пароль должен быть не короче 6 символов")
    # отсутствия меняются, только если форма их прислала
    absences = json.dumps(parse_absences(data["absences"]), ensure_ascii=False) if "absences" in data else None

    with db_lock():
        exists = con.execute("SELECT * FROM employees WHERE login=?", (login,)).fetchone()
        if is_new and exists:
            raise EmployeeError(f"Логин {login} уже занят")
        if not is_new and not exists:
            raise EmployeeError("Сотрудник не найден")
        if b24 and con.execute("SELECT 1 FROM employees WHERE b24_user_id=? AND login<>?", (b24, login)).fetchone():
            raise EmployeeError("Этот пользователь Битрикс24 уже привязан к другому сотруднику")
        if not admin and exists and exists["admin"] and _admins(con) <= 1:
            raise EmployeeError("Должен остаться хотя бы один администратор")
        if is_new:
            sort = (con.execute("SELECT MAX(sort) FROM employees").fetchone()[0] or 0) + 1
            con.execute("INSERT INTO employees (login, name, department, title, position, b24_user_id, onec_manager, "
                        "visible, admin, absences, sort, listen_dept) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (login, name, department, title, position, b24, onec, int(visible), int(admin), absences or "[]", sort,
                         int(listen_dept)))
        else:
            con.execute("UPDATE employees SET name=?, department=?, title=?, position=?, b24_user_id=?, onec_manager=?, "
                        "visible=?, admin=?, absences=COALESCE(?, absences), listen_dept=? WHERE login=?",
                        (name, department, title, position, b24, onec, int(visible), int(admin), absences, int(listen_dept), login))
        if password:
            h, salt = hash_password(password)
            con.execute("INSERT OR REPLACE INTO users (login, pwd_hash, salt) VALUES (?,?,?)", (login, h, salt))
            if not is_new:  # после смены пароля — выйти на всех устройствах
                con.execute("DELETE FROM sessions WHERE login=?", (login,))
        con.commit()
    return login


def delete(con, login, me):
    """Удалить сотрудника: он больше не входит и не виден в дашборде. Выгруженные по нему данные остаются в базе."""
    with db_lock():
        row = con.execute("SELECT * FROM employees WHERE login=?", (login,)).fetchone()
        if not row:
            raise EmployeeError("Сотрудник не найден")
        if login == me:
            raise EmployeeError("Нельзя удалить самого себя")
        if row["admin"] and _admins(con) <= 1:
            raise EmployeeError("Должен остаться хотя бы один администратор")
        con.execute("DELETE FROM employees WHERE login=?", (login,))
        con.execute("DELETE FROM users WHERE login=?", (login,))
        con.execute("DELETE FROM sessions WHERE login=?", (login,))
        con.commit()


def _admins(con):
    return con.execute("SELECT COUNT(*) FROM employees WHERE admin=1").fetchone()[0]
