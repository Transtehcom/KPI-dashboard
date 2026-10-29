"""Тесты расчёта загрузки (app/workload.py): ёмкость 9 ч/день, праздники, отпуска, просрочка, подзадачи, права."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import workload  # noqa: E402
from app.common import connect  # noqa: E402

TODAY = dt.date(2026, 10, 28)          # среда; текущая неделя — с 26.10
W1, W2 = "2026-11-02", "2026-11-09"    # понедельники следующих недель


def D(s):
    return s + "T18:00:00"


class WorkloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))
        self.config = {"holidays": ["2026-11-04"], "workdays": [], "employees": [
            {"login": "a", "name": "Анна", "b24_user_id": 27, "absences": []},
            {"login": "b", "name": "Борис", "b24_user_id": 28, "absences": []},
            {"login": "c", "name": "Без Б24", "absences": []}]}
        self.n = 0

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def task(self, emp, estimate, deadline=None, start=None, spent=0, parent=None, created="2026-10-20T10:00:00"):
        self.n += 1
        self.con.execute(
            "INSERT INTO workload_tasks (id, emp, title, status, created, start, deadline, estimate, spent, parent, task_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"wl{self.n}", emp, f"Задача {self.n}", "2", created, D(start) if start else None,
             D(deadline) if deadline else None, estimate, spent, parent, str(self.n)))
        self.con.commit()
        return str(self.n)

    def build(self, **kw):
        return workload.build(self.con, self.config, today=TODAY, **kw)

    def person(self, data, login="a"):
        return next(p for p in data["people"] if p["login"] == login)

    def test_two_tasks_one_week_overload(self):
        self.task("a", 45, deadline="2026-11-13", start="2026-11-09")
        self.task("a", 9, deadline="2026-11-13", start="2026-11-09")
        w = self.person(self.build())["weeks"][2]
        self.assertEqual((w["hours"], w["capacity"], w["percent"], w["peak"]), (54.0, 45.0, 120, 120))

    def test_holiday_reduces_capacity_and_concentrates_load(self):
        self.task("a", 36, deadline="2026-11-06", start="2026-11-02")  # 4 рабочих дня (04.11 праздник) → 9 ч в день
        w = self.person(self.build())["weeks"][1]
        self.assertEqual((w["capacity"], w["hours"], w["percent"], w["peak"]), (36.0, 36.0, 100, 100))

    def test_current_week_capacity_counts_from_today(self):
        w = self.person(self.build())["weeks"][0]
        self.assertEqual(w["capacity"], 27.0)  # ср, чт, пт

    def test_absence_removes_days_from_capacity_and_distribution(self):
        self.config["employees"][0]["absences"] = [{"from": "2026-11-09", "to": "2026-11-10", "kind": "vacation"}]
        self.task("a", 27, deadline="2026-11-13", start="2026-11-09")  # доступны 11, 12, 13 → 9 ч в день
        w = self.person(self.build())["weeks"][2]
        self.assertEqual((w["capacity"], w["hours"], w["percent"], w["absent_days"]), (27.0, 27.0, 100, 2))

    def test_overdue_task_lands_on_today(self):
        self.task("a", 5, deadline="2026-10-20")
        w = self.person(self.build())["weeks"][0]
        self.assertEqual((w["hours"], w["peak"]), (5.0, 56))

    def test_spent_time_is_subtracted(self):
        self.task("a", 10, deadline="2026-10-30", start="2026-10-28", spent=4)  # осталось 6 ч на ср–чт
        self.assertEqual(self.person(self.build())["weeks"][0]["hours"], 6.0)

    def test_no_estimate_and_no_deadline_go_to_lists_not_to_load(self):
        self.task("a", 0, deadline="2026-11-13")
        self.task("a", 3)
        d = self.build()
        p = self.person(d)
        self.assertEqual((p["no_estimate"], p["no_deadline"], p["planned"]), (1, 1, 0))
        self.assertEqual(sum(w["hours"] for w in p["weeks"]), 0)
        self.assertEqual([t["task_id"] for t in d["no_estimate"]], ["1"])
        self.assertEqual([t["task_id"] for t in d["no_deadline"]], ["2"])

    def test_parent_with_open_subtasks_is_not_counted(self):
        parent = self.task("a", 100, deadline="2026-11-13", start="2026-11-09")
        self.task("a", 9, deadline="2026-11-13", start="2026-11-09", parent=parent)
        self.assertEqual(self.person(self.build())["weeks"][2]["hours"], 9.0)

    def test_extra_workday_is_counted(self):
        self.config["workdays"] = ["2026-11-14"]  # рабочая суббота
        w = self.person(self.build())["weeks"][2]
        self.assertEqual(w["capacity"], 54.0)

    def test_percent_when_no_capacity(self):
        self.config["employees"][0]["absences"] = [{"from": "2026-11-09", "to": "2026-11-15", "kind": "sick"}]
        self.task("a", 5, deadline="2026-11-13", start="2026-11-09")  # все дни недели недоступны → уходит на первый доступный
        w = self.person(self.build())["weeks"][2]
        self.assertEqual(w["capacity"], 0)
        self.assertEqual(w["hours"], 0)
        self.assertEqual(self.person(self.build())["weeks"][3]["hours"], 5.0)

    def test_per_task_weeks_in_detail(self):
        self.task("a", 18, deadline="2026-11-13", start="2026-11-09")
        t = self.person(self.build())["tasks"][0]
        self.assertEqual(t["per_week"][2], 18.0)

    def test_members_and_include_list(self):
        self.assertEqual(workload.members(self.config), {27: "a", 28: "b"})
        self.config["workload"] = {"include_b24_ids": [28]}
        self.assertEqual(workload.members(self.config), {28: "b"})
        self.assertEqual([p["login"] for p in self.build()["people"]], ["b"])

    def test_non_manager_sees_only_self(self):
        self.task("a", 9, deadline="2026-11-13", start="2026-11-09")
        self.task("b", 9, deadline="2026-11-13", start="2026-11-09")
        d = self.build(logins={"b"})
        self.assertEqual([p["login"] for p in d["people"]], ["b"])
        self.assertEqual([t["login"] for t in d["no_estimate"]], [])

    def test_empty_table_has_no_data(self):
        d = self.build()
        self.assertFalse(d["has_data"])
        self.assertEqual(len(d["people"]), 2)

    def test_settings_defaults_and_overrides(self):
        self.assertEqual(workload.settings({})["day_hours"], 9.0)
        self.config["workload"] = {"day_hours": 8, "weeks": 3}
        d = self.build()
        self.assertEqual((d["day_hours"], len(d["mondays"])), (8.0, 3))


if __name__ == "__main__":
    unittest.main()
