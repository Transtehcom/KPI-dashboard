"""Тесты панели «Метрики → Отдел продаж» (формулы листов «Реестр» и «Действия» из «Метрики ОП.xlsx»)."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import b24_sales_register as b24  # noqa: E402
from app import onec_sales_register as reg  # noqa: E402
from app import sales_panel as sp  # noqa: E402
from app.common import connect  # noqa: E402

NOW = dt.datetime(2026, 10, 26, 9, 0)
TEAM = {"m1": "Менеджер 1", "ts": "Тендерный специалист"}


class SalesPanelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def obj(self, **kw):
        sp.save_object(self.con, {"type": "Сделка", "channel": "Тендеры", "emp": "ts", **kw}, TEAM, "x")

    def act(self, **kw):
        sp.save_action(self.con, {"dept": "Продажи", "status": "Открыто", **kw}, "x")

    def test_calculation_lists_missing_parts(self):
        self.obj(id="С-1", stage="Расчёт", f_req="Готово", f_plant="Нет", f_log="Не требуется")
        o = sp.build(self.con, TEAM, now=NOW)["objects"][0]
        self.assertEqual(o["need"], "КП завода; допрасходы; цена продажи; ")

    def test_all_not_required_asks_to_check(self):
        self.obj(id="С-1", stage="Расчёт", **{f: "Не требуется" for f, _, _ in sp.FLAGS})
        self.assertEqual(sp.build(self.con, TEAM, now=NOW)["objects"][0]["need"], "Проверить применимость расчёта")

    def test_critical_action_and_rank(self):
        self.obj(id="С-1", stage="Подача", due="2026-10-27T18:00")
        self.act(id="Д-1", obj_id="С-1", what="Подать", kind="Подача", due="2026-10-27T12:00")
        self.act(id="Д-2", obj_id="С-1", what="Позвонить", kind="Контакт", due="2026-10-25T10:00")
        self.act(id="Д-3", obj_id="С-1", what="Закрыто", kind="Контакт", due="2026-10-20T10:00", status="Закрыто")
        p = sp.build(self.con, TEAM, now=NOW)["panel"]
        self.assertEqual((p["tiles"]["hard"], p["tiles"]["sales_late"]), (1, 1))
        self.assertEqual([c["tag"] for c in p["critical"]], ["Жёсткий срок", "Просрочка продаж"])

    def test_closed_object_leaves_queue_and_actions_deactivate(self):
        self.obj(id="С-1", stage="Закрыто успешно")
        self.act(id="Д-1", obj_id="С-1", due="2026-10-25T10:00")
        d = sp.build(self.con, TEAM, now=NOW)
        self.assertEqual(d["panel"]["objects"], 0)
        self.assertFalse(d["actions"][0]["active"])

    def test_action_without_object_is_flagged(self):
        self.act(id="Д-1", obj_id="нет")
        self.assertEqual(sp.build(self.con, TEAM, now=NOW)["panel"]["control"]["no_link"], 1)

    def test_debt_from_1c_by_linked_order(self):
        self.con.execute("INSERT INTO receivables (id, emp, doc, client, due_date, paid_date, amount) "
                         "VALUES ('a','m1','00ЦБ-1','К','2026-10-20',NULL,500)")
        self.obj(id="И-1", type="Исполнение", stage="Приёмка и оплата", docs="Принято", order_1c="00ЦБ-1")
        o = sp.build(self.con, TEAM, now=NOW)["objects"][0]
        self.assertEqual((o["debt"], o["debt_late"], o["need"]), (500, 500, "Оплата клиента"))

    def test_rejects_unknown_stage(self):
        with self.assertRaises(ValueError):
            self.obj(id="С-1", stage="Выдумка")


    def row(self, **kw):
        return {"key": "k", "source": "tender", "id": "Т-1", "type": "Сделка", "channel": "Тендеры", "client": "К", "emp": "ts",
                "status_1c": "Действует", "amount": 100.0, "entered": "2026-03-01T10:00", "stage": None, "order_1c": None, **kw}

    def test_1c_sync_keeps_manual_stage_and_fields(self):
        reg.upsert(self.con, [self.row()])
        o = sp.build(self.con, TEAM, now=NOW)["objects"][0]
        self.assertEqual((o["stage"], o["no_stage"], o["status_1c"]), (None, True, "Действует"))
        self.obj(id="Т-1", stage="Расчёт", client="К", due="2026-10-30T10:00")  # руководитель выбрал этап и срок
        reg.upsert(self.con, [self.row(status_1c="Выиграно", amount=250.0, stage="Закрыто успешно")])
        o = sp.build(self.con, TEAM, now=NOW)["objects"][0]
        self.assertEqual((o["stage"], o["due"], o["status_1c"], o["amount"], o["source"]), ("Расчёт", "2026-10-30T10:00", "Выиграно", 250.0, "tender"))

    def test_1c_sync_updates_auto_stage_and_closes_vanished(self):
        reg.upsert(self.con, [self.row(source="kp", id="КП 1", stage="КП"), self.row(source="kp", id="КП 2", stage="КП", key="k2")])
        reg.upsert(self.con, [self.row(source="kp", id="КП 1", stage="Закрыто успешно")])
        stages = {o["id"]: o["stage"] for o in sp.build(self.con, TEAM, now=NOW)["objects"]}
        self.assertEqual(stages, {"КП 1": "Закрыто успешно", "КП 2": "Закрыто без результата"})

    def crm(self, id, entity, category, stage, title, emp="ts"):
        self.con.execute("INSERT INTO crm_deals (id, emp, category, stage, semantic, title, client, amount, created, entity) "
                         "VALUES (?,?,?,?,'P',?,?,?,?,?)", (id, emp, category, stage, title, "Клиент", 10.0, "2026-05-01T10:00:00", entity))

    def test_bitrix_deals_and_leads_join_registry_without_duplicating_1c(self):
        self.con.execute("INSERT INTO employees (login, name, department) VALUES ('ts', 'ТС', 'ОП')")
        reg.upsert(self.con, [self.row(id="Т-000822"), self.row(source="order", id="00ЦБ-000813", type="Исполнение", stage="Исполнение",
                                                            order_1c="00ЦБ-000813", key="o")])
        self.crm("deal1", "deal", "Поиск тендеров", "Ожидает результатов", "т822-Опоры")  # тендер 1С: берёт только этап
        self.crm("deal2", "deal", "Отдел продаж", "КП отправлено клиенту", "00ЦБ-000813")  # заказ 1С уже в реестре
        self.crm("deal3", "deal", "Отдел продаж", "Подготовка КП", "Новая сделка")
        self.crm("lead4", "lead", "Лиды", "Ожидают звонка", "Лид")
        self.crm("deal5", "deal", "Поиск тендеров", "В работе", "Т-новый")  # стадия неоднозначна — этап не ставится
        reg.upsert(self.con, b24.from_bitrix(self.con))
        o = {x["id"]: x for x in sp.build(self.con, TEAM, now=NOW)["objects"]}
        self.assertEqual(o["Т-000822"]["stage"], "Результат")
        self.assertEqual({k: (v["type"], v["stage"], v["channel"]) for k, v in o.items() if k.startswith("Б24")},
                         {"Б24-С-3": ("Сделка", "КП", "Прямые"), "Б24-Л-4": ("Лид", "Лиды", "Прямые"),
                          "Б24-С-5": ("Сделка", None, "Тендеры")})


if __name__ == "__main__":
    unittest.main()
