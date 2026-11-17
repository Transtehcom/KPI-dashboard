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

    def test_workload_group_preserves_individual_people(self):
        data = workload.build(self.con, self.config)
        groups = {person["uid"]: person["group"] for person in data["people"]}
        self.assertEqual(groups[ANNA], "ОП")
        self.assertIsNone(groups[BOSS])
        self.assertEqual(len(data["people"]), 2)


class EmployeeGroupsTest(unittest.TestCase):
    def test_departments_and_specific_roles_follow_the_supplied_mapping(self):
        examples = [
            ({"department":"ОП"}, "ОП"),
            ({"department":"Отдел продаж (ОП)"}, "ОП"),
            ({"department":"ОС"}, "ОС"),
            ({"department":"Отдел снабжения (ОС)"}, "ОС"),
            ({"department":"Отдел логистики"}, "ОЛ"),
            ({"department":"Отдел маркетинга"}, "Маркетолог"),
            ({"department":"Офис собственника", "title":"Финансовый менеджер"}, "ФО"),
            ({"department":"Офис собственника", "title":"Юрист"}, "ЮО"),
            ({"department":"Отдел управления персоналом", "title":"HR-директор"}, "HR"),
            ({"department":"Администрация", "title":"IT-специалист"}, "IT"),
            ({"login":"shashmurina", "department":"ОС"}, "Помощники руководителя"),
            ({"login":"petrovaaa", "department":"Офис собственника"}, "Помощники руководителя"),
        ]
        for employee, expected in examples:
            with self.subTest(employee=employee):
                self.assertEqual(workload.employee_group(employee), expected)

    def test_other_people_in_shared_departments_do_not_match_role_categories(self):
        for department, title in [("Администрация", "Исполнительный директор"),
                                  ("Транстехком", "Руководитель компании"),
                                  ("Офис собственника", "Бухгалтер")]:
            self.assertIsNone(workload.employee_group({"department":department,"title":title}))


class MemberSettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "members.sqlite")
        self.con = connect(self.path)
        self.addCleanup(lambda: self.con.close())
        self.config = {"employees": [
            {"login": "anna", "name": "Анна", "department": "ОС", "b24_user_id": ANNA},
            {"login": "boss", "name": "Директор", "title": "Исполнительный директор", "b24_user_id": BOSS},
            {"login": "petrovaaa", "name": "Помощник", "department": "ОС", "b24_user_id": 80},
            {"login": "without-id", "name": "Без Б24", "department": "ОП"}]}
        replace_rows(self.con, "task_ledger", [task(), task("boss", uid=BOSS)])
        workload.store_onec(self.con, {"rows": [onec_document()], "entities": ["Document_Order"]})
        self.con.commit()

    def configuration(self):
        return {member["login"]: member for member in workload.build(self.con, self.config)["configuration"]["members"]}

    def test_defaults_keep_matched_people_active_and_director_available(self):
        result = workload.build(self.con, self.config)
        members = self.configuration()
        self.assertEqual(result["configuration"]["directions"],
                         ["ОП", "ОС", "ОЛ", "Маркетолог", "ФО", "ЮО", "HR", "IT", "Помощники руководителя"])
        self.assertEqual(set(members), {"anna", "boss", "petrovaaa"})
        self.assertEqual(members["anna"], {"login": "anna", "name": "Анна", "uid": ANNA,
                                          "direction": None, "default_group": "ОС", "group": "ОС", "included": True})
        self.assertFalse(members["boss"]["included"])
        self.assertIsNone(members["boss"]["group"])
        self.assertTrue(members["petrovaaa"]["included"])
        self.assertEqual({p["uid"]: p["included"] for p in result["people"]}, {ANNA: True, BOSS: False, 80: True})

    def test_exclusion_retains_raw_tasks_documents_and_other_member_settings(self):
        workload.save_members(self.con, self.config, [{"login": "boss", "included": True, "direction": "IT"}], "admin")
        saved_boss = dict(self.con.execute("SELECT * FROM workload_member_settings WHERE login='boss'").fetchone())
        workload.save_members(self.con, self.config, [{"login": "anna", "included": False, "direction": "ОП"}], "admin2")
        result = workload.build(self.con, self.config)
        anna = next(p for p in result["people"] if p["uid"] == ANNA)
        self.assertEqual((anna["included"], anna["group"]), (False, "ОП"))
        self.assertEqual({t["uid"] for t in result["tasks"]}, {ANNA, BOSS})
        self.assertEqual([d["emp"] for d in result["documents"]], ["anna"])
        self.assertEqual(dict(self.con.execute("SELECT * FROM workload_member_settings WHERE login='boss'").fetchone()), saved_boss)
        self.assertEqual(self.configuration()["boss"]["group"], "IT")
        row = self.con.execute("SELECT * FROM workload_member_settings WHERE login='anna'").fetchone()
        self.assertEqual(row["updated_by"], "admin2")
        self.assertTrue(row["updated_at"])

    def test_override_survives_reload_and_source_changes_and_can_reset_to_auto(self):
        workload.save_members(self.con, self.config, [{"login": "anna", "included": True, "direction": "ОП"}], "admin")
        self.con.close()
        self.con = connect(self.path)
        self.config["employees"][0]["department"] = "ОЛ"
        self.assertEqual((self.configuration()["anna"]["group"], self.configuration()["anna"]["default_group"]), ("ОП", "ОЛ"))
        for direction in (None, ""):
            workload.save_members(self.con, self.config, [{"login": "anna", "included": True, "direction": direction}], "admin")
            self.assertEqual((self.configuration()["anna"]["group"], self.configuration()["anna"]["direction"]), ("ОЛ", None))
        workload.save_members(self.con, self.config, [{"login": "anna", "included": True, "direction": "ОП"}], "admin")
        self.assertEqual(self.config["employees"][0]["department"], "ОЛ")

    def test_direction_changes_do_not_change_author_only_identity_rule(self):
        workload.save_members(self.con, self.config, [{"login": "petrovaaa", "included": True, "direction": "ОП"}], "admin")
        self.assertEqual(self.configuration()["petrovaaa"]["group"], "ОП")
        self.assertIn("petrovaaa", workload.AUTHOR_ONLY)
        self.assertNotIn("anna", workload.AUTHOR_ONLY)

    def test_invalid_batch_is_rejected_before_any_writes(self):
        valid = {"login": "anna", "included": False, "direction": "ОП"}
        invalid_rows = [None, {}, [], {"login": "missing", "included": True, "direction": "ОП"},
                        {"login": "without-id", "included": True, "direction": "ОП"},
                        {"login": "boss", "included": True, "direction": "unknown"},
                        {"login": "boss", "included": True, "direction": []},
                        {"login": "boss", "included": 1, "direction": "IT"},
                        {"login": "boss", "included": "false", "direction": "IT"},
                        {"login": "boss", "direction": "IT"},
                        {"login": "boss", "included": True, "direction": None},
                        {"login": "boss", "included": True, "direction": ""}, valid]
        for invalid in invalid_rows:
            with self.subTest(row=invalid), self.assertRaises(ValueError):
                workload.save_members(self.con, self.config, [valid, invalid], "admin")
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM workload_member_settings").fetchone()[0], 0)
        for invalid in (None, {}, "anna"):
            with self.subTest(batch=invalid), self.assertRaises(ValueError):
                workload.save_members(self.con, self.config, invalid, "admin")

    def test_uid_scope_also_scopes_configuration(self):
        result = workload.build(self.con, self.config, uids={ANNA})
        self.assertEqual([m["login"] for m in result["configuration"]["members"]], ["anna"])
        self.assertEqual({t["uid"] for t in result["tasks"]}, {ANNA})


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

    def test_other_instant_document_types_have_no_timing_assessment(self):
        entities = [
            'Document_ПоступлениеБезналичныхДенежныхСредств',
            'Document_РеализацияТоваровУслуг',
            'Document_ОжидаемоеПоступлениеДенежныхСредств',
            'Document_СписаниеБезналичныхДенежныхСредств',
            'Document_СчетНаОплатуКлиенту',
            'Document_ДоверенностьВыданная',
        ]
        rows = [onec_document(str(i), entity=entity, posted=0) for i, entity in enumerate(entities)]
        control = onec_document('control', entity='Document_ЗаявкаНаРасходованиеДенежныхСредств', posted=0)
        self.store([*rows, control], entities=(*entities, control['entity']))
        documents = workload.build(self.con, self.config)['documents']
        for row in documents:
            with self.subTest(entity=row['entity']):
                if row['entity'] == control['entity']:
                    self.assertTrue(row['open'])
                    self.assertTrue(row['timing_applicable'])
                else:
                    self.assertTrue(row['is_closed'])
                    self.assertEqual(row['closed_at'], row['date'])
                    self.assertIsNone(row['deadline'])
                    self.assertFalse(row['timing_applicable'])

    def test_payment_request_uses_rejection_or_payment_date_instead_of_posted(self):
        entity = 'Document_ЗаявкаНаРасходованиеДенежныхСредств'
        rows = [
            {**onec_document('open', entity=entity, posted=1), 'status': 'КОплате'},
            {**onec_document('unapproved', entity=entity, posted=1), 'status': 'НеСогласована'},
            {**onec_document('rejected', entity=entity), 'status': 'Отклонена'},
            {**onec_document('paid', entity=entity), 'status': 'КОплате', 'closed_at': '2026-10-10T11:12:13'},
        ]
        self.store(rows, entities=(entity,))
        documents = {row['ref']: row for row in workload.build(self.con, self.config)['documents']}
        self.assertEqual({ref for ref, row in documents.items() if row['open']}, {'open', 'unapproved'})
        self.assertEqual({ref for ref, row in documents.items() if row['is_closed']}, {'rejected', 'paid'})
        self.assertIsNone(documents['rejected']['closed_at'])
        self.assertEqual(documents['paid']['closed_at'], '2026-10-10T11:12:13')
        self.assertTrue(all(row['timing_applicable'] for row in documents.values()))

    def test_goods_receipt_legacy_rows_are_completed_at_document_date_without_timing(self):
        entity = "Document_ПриобретениеТоваровУслуг"
        self.store([onec_document("receipt", entity=entity, posted=0)], entities=(entity,))
        row = workload.build(self.con, self.config)['documents'][0]
        self.assertTrue(row['is_closed'])
        self.assertFalse(row['open'])
        self.assertEqual(row['closed_at'], row['date'])
        self.assertIsNone(row['deadline'])
        self.assertFalse(row['timing_applicable'])

    def test_transport_only_processed_status_is_closed(self):
        entity = "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов"
        states = ['Поступила', 'ВРаботе', 'ЗапросИнформации', 'Отработана']
        self.store([{**onec_document(str(i), entity=entity, posted=1), 'status': status}
                    for i, status in enumerate(states)], entities=(entity,))
        rows = workload.build(self.con, self.config)['documents']
        self.assertEqual({row['status'] for row in rows if row['is_closed']}, {'Отработана'})
        self.assertEqual(sum(row['open'] for row in rows), 3)

    def test_forwarder_dfp_controls_closure_independently_of_status(self):
        entity = "Document_ПоручениеЭкспедитору"
        rows = [{**onec_document("issued", entity=entity, posted=1), "status": "Выдан", "closed_at": "2026-11-17T13:15:30"},
                {**onec_document("in-transit", entity=entity, posted=1), "status": "ВПути"},
                {**onec_document("completed-no-dfp", entity=entity, posted=0), "status": "Завершено"},
                {**onec_document("no-status", entity=entity, posted=1), "status": None, "closed_at": "2026-11-17T13:15:30"}]
        self.store(rows, entities=(entity,))
        documents = {row['ref']: row for row in workload.build(self.con, self.config)['documents']}
        self.assertEqual({ref for ref, row in documents.items() if row['is_closed']}, {'issued', 'no-status'})
        self.assertTrue(documents['in-transit']['open'])
        self.assertTrue(documents['completed-no-dfp']['open'])

    def test_supply_status_and_legacy_closure_date_control_open_state(self):
        entity = "Document_ТТК_ЗаявкаВОтделСнабжения"
        rows = [{**onec_document("912", entity=entity, posted=1), "status": "ВРаботе", "closed_at": None},
                {**onec_document("finished", entity=entity, posted=0), "status": "Отработана", "closed_at": "2026-11-17T10:00:00"},
                {**onec_document("legacy-open", entity=entity, posted=1), "closed_at": None},
                {**onec_document("legacy-done", entity=entity, posted=1), "closed_at": "2026-11-17T10:00:00"}]
        self.store(rows, entities=(entity,))
        documents = {row['ref']: row for row in workload.build(self.con, self.config)['documents']}
        self.assertTrue(documents['912']['open'])
        self.assertFalse(documents['912']['is_closed'])
        self.assertTrue(documents['finished']['is_closed'])
        self.assertTrue(documents['legacy-open']['open'])
        self.assertTrue(documents['legacy-done']['is_closed'])

    def test_tender_status_controls_workload_state_independently_of_posted(self):
        entity = "Document_ТТК_Тендер"
        self.store([{**onec_document("open", entity=entity, posted=1), "status": "Действует"},
                    {**onec_document("closed", entity=entity, posted=0), "status": "Выиграно"}], entities=(entity,))
        rows = {row['ref']: row for row in workload.build(self.con, self.config)['documents']}
        self.assertTrue(rows['open']['posted'])
        self.assertFalse(rows['open']['is_closed'])
        self.assertTrue(rows['open']['open'])
        self.assertFalse(rows['closed']['posted'])
        self.assertTrue(rows['closed']['is_closed'])
        self.assertFalse(rows['closed']['open'])

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
