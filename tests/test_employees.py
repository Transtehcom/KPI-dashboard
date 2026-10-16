"""Тесты раздела «Сотрудники». Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import employees  # noqa: E402
from app.common import connect, load_rules  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.server import hash_password  # noqa: E402

CONFIG_EMPLOYEES = [
    {"login": "boss", "name": "Руководитель", "role": "manager"},
    {"login": "pm", "name": "Пётр Менеджеров", "position": "pm", "role": "employee", "b24_user_id": 56,
     "onec_manager": "Менеджеров Пётр Петрович", "absences": [{"from": "2026-06-01", "to": "2026-06-30"}]},
]


def form(**kw):
    data = {"login": "new", "name": "Новый Сотрудник", "department": "ОС", "title": "Специалист",
            "position": "supply", "b24_user_id": "https://portal.bitrix24.ru/company/personal/user/90/",
            "onec_manager": "Сотрудник Новый Иванович", "visible": True, "admin": False, "password": "secret1"}
    data.update(kw)
    return data


class EmployeesTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.config = {"employees": [dict(e) for e in CONFIG_EMPLOYEES]}
        employees.apply(self.con, self.config, self.rules)

    def save(self, is_new=True, me="boss", **kw):
        return employees.save(self.con, self.rules, form(**kw), me, is_new, hash_password)

    def test_import_from_config_keeps_roles_and_absences(self):
        by = {e["login"]: e for e in self.config["employees"]}
        self.assertEqual(by["boss"]["role"], "manager")
        self.assertFalse(by["boss"]["visible"])
        self.assertTrue(by["pm"]["visible"])
        self.assertEqual(by["pm"]["absences"][0]["from"], "2026-06-01")
        self.assertEqual(by["pm"]["title"], self.rules["positions"]["pm"]["title"])
        self.assertEqual([e["login"] for e in Engine(self.con, self.rules, self.config).employees()], ["pm"])

    def test_create_parses_b24_link_and_sets_password(self):
        self.save()
        employees.apply(self.con, self.config, self.rules)
        new = next(e for e in self.config["employees"] if e["login"] == "new")
        self.assertEqual(new["b24_user_id"], 90)
        self.assertEqual(new["position"], "supply")
        self.assertTrue(self.con.execute("SELECT 1 FROM users WHERE login='new'").fetchone())
        self.assertIn("new", [e["login"] for e in Engine(self.con, self.rules, self.config).employees()])

    def test_validation(self):
        bad = [dict(login="Bad Login"), dict(name=""), dict(position="", visible=True), dict(visible=False, admin=False),
               dict(password="123"), dict(b24_user_id="не ссылка"), dict(login="pm"), dict(b24_user_id="56")]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(employees.EmployeeError):
                self.save(**kw)

    def test_update_keeps_password_when_empty_and_protects_last_admin(self):
        self.save()
        before = self.con.execute("SELECT pwd_hash FROM users WHERE login='new'").fetchone()[0]
        self.save(is_new=False, title="Старший специалист", password="")
        self.assertEqual(self.con.execute("SELECT pwd_hash FROM users WHERE login='new'").fetchone()[0], before)
        with self.assertRaises(employees.EmployeeError):  # снять права с самого себя
            employees.save(self.con, self.rules, form(login="boss", position="", visible=False, admin=False, password=""),
                           "boss", False, hash_password)
        with self.assertRaises(employees.EmployeeError):  # удалить самого себя
            employees.delete(self.con, "boss", "boss")

    def test_delete_removes_login_and_sessions(self):
        self.save()
        self.con.execute("INSERT INTO sessions VALUES ('t', 'new', '2999-01-01')")
        employees.delete(self.con, "new", "boss")
        self.assertFalse(self.con.execute("SELECT 1 FROM users WHERE login='new'").fetchone())
        self.assertFalse(self.con.execute("SELECT 1 FROM sessions WHERE login='new'").fetchone())
        employees.apply(self.con, self.config, self.rules)
        self.assertNotIn("new", [e["login"] for e in self.config["employees"]])


if __name__ == "__main__":
    unittest.main()
