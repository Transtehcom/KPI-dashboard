"""Тесты вкладки «Загрузка» (app/workload.py): сроки, переносы, автоматические замечания, проверка и возвраты, права."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import workload  # noqa: E402
from app import sync  # noqa: E402
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

    def test_late_interval_survives_a_creator_extension_after_deadline(self):
        new = "2026-10-20T10:00:00"
        a = an(task(deadline=new, history=[mv("2026-10-12T09:00:00", DL, new)]))
        self.assertEqual(a.get("late_intervals"), [{"start": DL, "end": "2026-10-12T09:00:00"}])

    def test_extension_before_deadline_does_not_create_a_late_interval(self):
        new = "2026-10-20T10:00:00"
        a = an(task(deadline=new, history=[mv("2026-10-09T09:00:00", DL, new)]))
        self.assertEqual(a.get("late_intervals"), [])

    def test_completion_and_review_submission_end_late_intervals(self):
        cases = [task(status="5", closed="2026-10-11T12:00:00"),
                 task(status="4", control=1, history=[st("2026-10-11T12:00:00", "3", "4")])]
        for row in cases:
            with self.subTest(status=row["status"]):
                self.assertEqual(an(row).get("late_intervals"), [{"start": DL, "end": "2026-10-11T12:00:00"}])

    def test_open_late_interval_reaches_now_and_new_deadline_can_add_another(self):
        new = "2026-10-14T10:00:00"
        a = an(task(deadline=new, history=[mv("2026-10-12T09:00:00", DL, new)]))
        self.assertEqual(a.get("late_intervals"), [{"start": DL, "end": "2026-10-12T09:00:00"},
                                                   {"start": new, "end": NOW}])

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


def onec_document(ref="a", emp="anna", entity="Document_Order", posted=0):
    return {"id": entity + ":" + ref + ":" + emp, "entity": entity, "ref": ref, "emp": emp,
            "number": "001", "date": "2026-10-01T10:00:00", "posted": posted,
            "deadline": "2026-10-10T23:59:59", "closed_at": None,
            "deadline_source": "Срок", "owner_field": "Менеджер_Key", "url": None}


class OneCWorkloadTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.config = {"employees": [{"login": "anna", "name": "Анна", "b24_user_id": ANNA},
                                      {"login": "boss", "name": "Руководитель", "b24_user_id": BOSS}]}

    def store(self, rows=(), entities=("Document_Order",), errors=None, at=NOW):
        workload.store_onec(self.con, {"rows": list(rows), "entities": list(entities),
                                      "errors": errors or {}, "since": "2026-01-01"}, updated=at)

    def test_missing_snapshot_is_distinct_from_successful_empty_snapshot(self):
        result = workload.build(self.con, self.config)
        self.assertEqual(result.get("documents"), [])
        self.assertFalse(result["onec"]["loaded"])
        self.assertIsNone(result["onec"]["updated"])
        self.store()
        result = workload.build(self.con, self.config)
        self.assertEqual(result["documents"], [])
        self.assertTrue(result["onec"]["loaded"])
        self.assertEqual(result["onec"]["since"], "2026-01-01")

    def test_posted_flags_and_document_date_preserve_unknown_completion(self):
        self.store([onec_document(), onec_document("b", posted=1)])
        docs = {row["ref"]: row for row in workload.build(self.con, self.config)["documents"]}
        self.assertEqual((docs["a"]["uid"], docs["a"]["posted"], docs["a"]["open"]), (ANNA, False, True))
        self.assertEqual((docs["b"]["posted"], docs["b"]["open"], docs["b"]["closed_at"]), (True, False, None))
        self.assertEqual(docs["a"]["date"], "2026-10-01T10:00:00")

    def test_current_login_association_and_uid_scope_are_enforced(self):
        self.store([onec_document(), onec_document("b", emp="boss"), onec_document("c", emp="removed")])
        self.config["employees"][0]["b24_user_id"] = 80
        result = workload.build(self.con, self.config, uids={80})
        self.assertEqual([(d["ref"], d["uid"]) for d in result["documents"]], [("a", 80)])
        self.assertEqual(workload.build(self.con, self.config, uids={ANNA})["documents"], [])
        self.assertEqual(workload.build(self.con, self.config, uids=set())["documents"], [])

    def test_partial_failure_keeps_failed_entity_snapshot_and_records_error(self):
        self.store([onec_document(), onec_document("b", entity="Document_Supply")],
                   entities=("Document_Order", "Document_Supply"))
        self.store([onec_document("new")], errors={"Document_Supply": "HTTP 401"}, at="2026-10-16T10:00:00")
        result = workload.build(self.con, self.config)
        self.assertEqual({d["ref"] for d in result["documents"]}, {"new", "b"})
        self.assertEqual(result["onec"]["errors"], {"Document_Supply": "HTTP 401"})
        self.store([], entities=("Document_Supply",))
        self.assertEqual(workload.build(self.con, self.config)["onec"]["errors"], {})

    def test_failed_first_snapshot_has_no_successful_coverage(self):
        self.store(entities=(), errors={"Document_Order": "unavailable"})
        result = workload.build(self.con, self.config)
        self.assertFalse(result["onec"]["loaded"])
        self.assertEqual(result["onec"]["errors"], {"Document_Order": "unavailable"})

    def test_recovered_metadata_and_user_catalog_errors_clear(self):
        self.store(entities=(), errors={"$metadata": "HTTP 503", "Catalog_Пользователи": "HTTP 401"})
        self.store([onec_document()])
        self.assertEqual(workload.build(self.con, self.config)["onec"]["errors"], {})

    def test_store_can_be_rolled_back_with_sync_transaction(self):
        self.store([onec_document()])
        self.con.rollback()
        self.assertEqual(workload.build(self.con, self.config)["documents"], [])
        self.assertFalse(workload.build(self.con, self.config)["onec"]["loaded"])


class OneCWorkloadSyncTest(unittest.TestCase):
    def test_all_config_employees_are_loaded_even_when_kpi_fetch_fails(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        config = {"mode": "real", "holidays": [], "employees": [
            {"login": "anna", "name": "Анна", "b24_user_id": ANNA, "onec_manager": "Анна 1С"}],
            "onec": {"base_url": "https://example.test", "datasets": {}}}

        def fetch(onec, current_config, date_from):
            rows = [onec_document(emp=e["login"]) for e in current_config["employees"] if e.get("onec_manager")]
            return {"rows": rows, "entities": ["Document_Order"], "errors": {}, "since": "2026-01-01"}

        with mock.patch("app.sync.OneC") as client, \
             mock.patch("app.sync.supplier_directory.refresh", return_value="ok"), \
             mock.patch("app.sync.onec_sales_register.fetch", return_value=[]), \
             mock.patch("app.sync.onec_money.fetch", return_value=None), \
             mock.patch.object(sync, "onec_workload", SimpleNamespace(fetch=fetch), create=True):
            client.return_value.fetch.side_effect = RuntimeError("KPI unavailable")
            sync.run(con, config)
        docs = workload.build(con, config)["documents"]
        self.assertEqual([(r["emp"], r["uid"]) for r in docs], [("anna", ANNA)])

    def test_source_failure_keeps_snapshot_and_marks_coverage_error(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        config = {"mode": "real", "employees": [], "holidays": [],
                  "onec": {"base_url": "https://example.test", "datasets": {}}}
        workload.store_onec(con, {"rows": [onec_document()], "entities": ["Document_Order"],
                                  "errors": {}, "since": "2026-01-01"}, updated=NOW)
        con.commit()
        def fail(onec, current_config):
            raise RuntimeError("unavailable")
        with mock.patch("app.sync.OneC") as client, \
             mock.patch("app.sync.supplier_directory.refresh", return_value="ok"), \
             mock.patch("app.sync.onec_sales_register.fetch", return_value=[]), \
             mock.patch("app.sync.onec_money.fetch", return_value=None), \
             mock.patch.object(sync, "onec_workload", SimpleNamespace(fetch=fail), create=True):
            client.return_value.fetch.return_value = {}
            result = sync.run(con, config)
        self.assertEqual(con.execute("SELECT ref FROM workload_onec_documents").fetchone()[0], "a")
        self.assertEqual(result["status"], "error")
        self.assertTrue(workload.build(con, config)["onec"]["errors"])


if __name__ == "__main__":
    unittest.main()
