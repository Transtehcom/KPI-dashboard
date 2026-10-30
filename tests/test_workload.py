"""Тесты вкладки «Загрузка» (app/workload.py): сроки, переносы, автоматические замечания, проверка и возвраты, права."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import workload  # noqa: E402
from app.common import connect, replace_rows  # noqa: E402

NOW = "2026-10-15T12:00:00"
BOSS, ANNA = 25, 27
DL = "2026-10-10T10:00:00"


def task(tid="1", status="2", control=0, creator=BOSS, uid=ANNA, deadline=DL, closed=None, history=(), tags=(), parent=None):
    return {"id": tid, "uid": uid, "title": "Задача " + tid, "status": status, "priority": "1", "project": None,
            "tags": json.dumps(list(tags), ensure_ascii=False), "creator": creator, "control": control,
            "created": "2026-10-01T09:00:00", "deadline": deadline, "closed": closed, "parent": parent,
            "history": json.dumps([list(h) for h in history], ensure_ascii=False)}


def st(at, a, b, who=ANNA):
    return [at, "STATUS", a, b, who]


def mv(at, a, b, who=BOSS):
    return [at, "DEADLINE", a, b, who]


def an(t):
    return workload.analyze(t, NOW, {"блокировка"}, {"помощь"})


class TaskRulesTest(unittest.TestCase):
    def test_simple_done_exactly_at_deadline_is_on_time(self):
        a = an(task(status="5", closed=DL, history=[st(DL, "2", "5")]))
        self.assertEqual((a["mode"], a["on_time"], a["remarks"], a["quality"], a["open"]), ("simple", True, [], "none", False))

    def test_overdue_without_request_gives_one_deadline_remark(self):
        a = an(task())
        self.assertEqual(a["overdue_days"], 6)
        self.assertEqual([(r["type"], r["at"], r["review"]) for r in a["remarks"]], [("срок", DL, False)])
        self.assertIn("не запросил перенос", a["remarks"][0]["reason"])

    def test_executor_moving_deadline_is_only_a_request(self):
        a = an(task(deadline="2026-10-20T10:00:00", history=[mv("2026-10-09T09:00:00", DL, "2026-10-20T10:00:00", who=ANNA)]))
        self.assertEqual(a["deadline"], DL)  # согласованный срок прежний
        self.assertEqual((a["self_moves"], a["overdue_days"]), (["2026-10-09T09:00:00"], 6))
        self.assertEqual([(r["review"], r["reason"]) for r in a["remarks"]], [(True, "Запрос подан вовремя, решение не получено")])

    def test_creator_moving_before_deadline_is_agreed(self):
        new = "2026-10-20T10:00:00"
        a = an(task(status="5", deadline=new, closed="2026-10-14T10:00:00",
                    history=[mv("2026-10-09T09:00:00", DL, new), st("2026-10-14T10:00:00", "2", "5")]))
        self.assertEqual((a["deadline_orig"], a["deadline"], a["moves_ok"], a["moves_late"]), (DL, new, ["2026-10-09T09:00:00"], []))
        self.assertEqual((a["on_time"], a["remarks"]), (True, []))

    def test_agreement_after_deadline_keeps_remark(self):
        new = "2026-10-20T10:00:00"
        a = an(task(deadline=new, history=[mv("2026-10-12T09:00:00", DL, new)]))
        self.assertEqual((a["deadline"], a["moves_late"], a["overdue_days"]), (new, ["2026-10-12T09:00:00"], 0))
        self.assertEqual([r["at"] for r in a["remarks"]], [DL])

    def test_return_and_resubmit_after_deadline(self):
        h = [st("2026-10-09T10:00:00", "3", "4"), st("2026-10-09T15:00:00", "4", "2", BOSS),
             st("2026-10-11T10:00:00", "2", "4"), st("2026-10-12T10:00:00", "4", "5", BOSS)]
        a = an(task(status="5", control=1, closed="2026-10-12T10:00:00", history=h))
        self.assertEqual((a["mode"], a["checked"], a["exec_at"], a["accepted"]), ("check", True, "2026-10-11T10:00:00", "2026-10-12T10:00:00"))
        self.assertEqual((a["returns"], a["on_time"], a["quality"]), (["2026-10-09T15:00:00"], False, "remarks"))
        self.assertEqual(sorted(r["type"] for r in a["remarks"]), ["качество", "срок"])

    def test_accepted_first_time_is_clean(self):
        h = [st("2026-10-09T10:00:00", "3", "4"), st("2026-10-12T10:00:00", "4", "5", BOSS)]
        a = an(task(status="5", control=1, closed="2026-10-12T10:00:00", history=h))
        self.assertEqual((a["on_time"], a["quality"], a["remarks"]), (True, "clean", []))

    def test_check_task_closed_without_review_is_not_rated(self):
        a = an(task(status="5", control=1, closed="2026-10-09T10:00:00", history=[st("2026-10-09T10:00:00", "2", "5", BOSS)]))
        self.assertEqual((a["checked"], a["quality"], a["accepted"]), (False, "not_rated", None))

    def test_late_review_is_reviewer_delay_not_overdue(self):
        a = an(task(status="4", control=1, history=[st("2026-10-09T10:00:00", "3", "4")]))
        self.assertEqual((a["overdue_days"], a["review_delay_days"], a["remarks"], a["on_review"]), (0, 6, [], True))

    def test_rework_status_and_tags(self):
        h = [st("2026-10-09T10:00:00", "3", "4"), st("2026-10-09T15:00:00", "4", "2", BOSS)]
        a = an(task(control=1, history=h, tags=["Блокировка", "помощь"], deadline="2026-10-30T10:00:00"))
        self.assertEqual((a["on_rework"], a["status_label"], a["blocked"], a["help"]), (True, "На доработке", True, True))

    def test_self_task_moves_are_agreed(self):
        new = "2026-10-20T10:00:00"
        a = an(task(creator=ANNA, deadline=new, history=[mv("2026-10-09T09:00:00", DL, new, who=ANNA)]))
        self.assertEqual((a["deadline"], a["moves_ok"], a["remarks"]), (new, ["2026-10-09T09:00:00"], []))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))
        replace_rows(self.con, "task_people", [{"id": "u16", "uid": 16, "name": "Беляев Артем (Б24)"}])
        self.config = {"employees": [  # раздел «Сотрудники»
            {"login": "sh", "name": "Валентина Шарлаимова", "title": "Исполнительный директор", "department": "Администрация", "b24_user_id": BOSS},
            {"login": "an", "name": "Анна Мерзлякова", "title": "Специалист", "department": "ОП", "b24_user_id": ANNA},
            {"login": "x", "name": "Без Б24", "department": "ОП"}]}
        replace_rows(self.con, "task_ledger", [
            task("1"), task("2", status="7"), task("3"), task("4", parent="3"), task("5", uid=16), task("6", uid=BOSS, creator=16)])

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_excludes_declined_containers_and_untracked(self):
        import datetime as dt
        d = workload.build(self.con, self.config, now=dt.datetime(2026, 10, 15, 12), dept_titles={"ОП": "Отдел продаж (ОП)"})
        self.assertEqual(sorted(t["id"] for t in d["tasks"]), ["1", "4", "6"])
        self.assertEqual(d["director"], BOSS)
        self.assertEqual([(p["name"], p["dept"], p["position"]) for p in d["people"]],
                         [("Анна Мерзлякова", "Отдел продаж (ОП)", "Специалист"),
                          ("Валентина Шарлаимова", "Администрация", "Исполнительный директор")])
        self.assertEqual(next(t for t in d["tasks"] if t["id"] == "6")["creator_name"], "Беляев Артем (Б24)")

    def test_employee_sees_only_own_tasks(self):
        d = workload.build(self.con, self.config, uids={ANNA})
        self.assertEqual(({t["uid"] for t in d["tasks"]}, [p["uid"] for p in d["people"]]), ({ANNA}, [ANNA]))
        self.assertEqual(workload.build(self.con, self.config, uids=set())["tasks"], [])


if __name__ == "__main__":
    unittest.main()
