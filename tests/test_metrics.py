"""Тесты раздела «Метрики». Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import metrics  # noqa: E402
from app.common import connect, replace_rows  # noqa: E402

TODAY = dt.date(2026, 10, 22)
NOW = "2026-10-22T12:00:00"


def req(i, status, created, closed=None, due=None, order_dt=None, customer_order=None, emp="sup"):
    return {"id": f"r{i}", "emp": emp, "dt": created[:10], "doc": f"З-{i}", "status": status, "created_at": created,
            "closed_at": closed, "due_at": due, "order_dt": order_dt, "customer_order": customer_order,
            "stage": "open"}


def order(i, placed, plan=None, fact=None, customer_order=None, emp="sup"):
    return {"id": f"o{i}", "emp": emp, "doc": f"ЗП-{i}", "dt": placed, "supplier": "Завод", "amount_novat": 1000.0,
            "planned_date": plan, "actual_date": fact, "complete": 1, "customer_order": customer_order}


class SupplyMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.tmp.name, "kpi.sqlite"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def run_supply(self, reqs, orders, date_from="2026-10-01", date_to="2026-10-31"):
        replace_rows(self.con, "supply_funnel", reqs)
        replace_rows(self.con, "supplier_orders", orders)
        return metrics.supply(self.con, {"sup": "Снабженец"}, date_from, date_to, today=TODAY, now=NOW)

    def stage(self, res, key):
        return next(s for s in res["stages"] if s["key"] == key)

    def test_open_request_is_on_calc_and_overdue_by_deadline(self):
        res = self.run_supply([req(1, "ВРаботе", "2026-10-10T09:00:00", due="2026-10-20T18:00:00"),
                               req(2, "Поступила", "2026-10-21T09:00:00", due="2026-10-24T18:00:00")], [])
        calc = self.stage(res, "calc")
        self.assertEqual((calc["now"], calc["came"], calc["left"], calc["overdue"], calc["max_days"]), (2, 2, 0, 1, 12))
        self.assertEqual([r["late_days"] for r in res["requests"]], [2, 0])
        self.assertEqual(res["tiles"]["overdue"], 1)

    def test_request_moves_through_handed_and_approve_one_stage_at_a_time(self):
        res = self.run_supply([req(1, "Отработана", "2026-10-01T09:00:00", closed="2026-10-03T10:00:00",
                                   order_dt="2026-10-06", customer_order="ЗК-1")], [])
        self.assertEqual([self.stage(res, k)["now"] for k in ("calc", "handed", "approve")], [0, 0, 1])
        self.assertEqual(self.stage(res, "handed")["left"], 1)
        self.assertEqual(res["tiles"]["done_calc"], 1)
        # появился заказ поставщику — заявка уходит с «Закупка согласуется», дальше идёт заказ
        res = self.run_supply([req(1, "Отработана", "2026-10-01T09:00:00", closed="2026-10-03T10:00:00",
                                   order_dt="2026-10-06", customer_order="ЗК-1")],
                              [order(1, "2026-10-08", plan="2026-12-01", customer_order="ЗК-1")])
        self.assertEqual([self.stage(res, k)["now"] for k in ("approve", "placed", "shipping")], [0, 1, 0])
        self.assertEqual(res["tiles"]["in_work"], 1)

    def test_handed_request_without_order_leaves_queue_after_window(self):
        res = self.run_supply([req(1, "Отработана", "2026-08-01T09:00:00", closed="2026-08-02T10:00:00")], [])
        self.assertEqual(self.stage(res, "handed")["now"], 0)
        self.assertEqual(res["tiles"]["in_work"], 0)

    def test_cancelled_request_is_not_in_queue(self):
        res = self.run_supply([req(1, "Отменена", "2026-10-01T09:00:00", closed="2026-10-02T10:00:00")], [])
        self.assertEqual(res["tiles"]["in_work"], 0)
        self.assertEqual(self.stage(res, "calc")["left"], 1)

    def test_supplier_order_stages_by_planned_date(self):
        res = self.run_supply([], [order(1, "2026-10-01", plan="2026-11-30"),          # далеко — размещён
                                   order(2, "2026-10-01", plan="2026-10-25"),          # скоро — ожидается отгрузка
                                   order(3, "2026-10-01", plan="2026-10-15"),          # срок прошёл — просрочен
                                   order(4, "2026-10-01", plan="2026-10-15", fact="2026-10-14"),  # поставлен
                                   order(5, "2026-10-01")])                            # без даты — размещён
        self.assertEqual([self.stage(res, k)["now"] for k in ("placed", "shipping")], [2, 2])
        self.assertEqual(self.stage(res, "shipping")["overdue"], 1)
        self.assertEqual(res["tiles"]["done_orders"], 1)
        late = {o["doc"]: o["late_days"] for o in res["orders"]}
        self.assertEqual((late["ЗП-3"], late["ЗП-4"], late["ЗП-5"]), (7, 0, None))

    def test_unmapped_responsible_shows_1c_name(self):
        res = self.run_supply([req(1, "ВРаботе", "2026-10-20T09:00:00", emp="1c:Момот Руслан")], [])
        self.assertEqual([p["name"] for p in res["people"]], ["Момот Руслан"])

    def test_old_rows_without_status_use_funnel_stage(self):
        r = req(1, None, "2026-10-20T09:00:00")
        r.update(stage="cancelled", created_at=None)
        res = self.run_supply([r], [])
        self.assertEqual(res["tiles"]["in_work"], 0)


class LogisticsMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.tmp.name, "kpi.sqlite"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_layout_like_xlsx_with_calc_stage_from_status_history(self):
        import json
        h = lambda *xs: json.dumps([list(x) for x in xs], ensure_ascii=False)
        rows = [
            # отработана за 3 часа: вошла и вышла в октябре
            {"id": "t1", "emp": "1c:Ваганова Наталия", "dt": "2026-10-20", "created_at": "2026-10-20T14:07:00",
             "doc": "000002338", "status": "Отработана", "posted": 1, "deleted": 0, "route": "Миасс → Когалым",
             "carrier": "да транс", "cost": 81200,
             "history": h(("Поступила", "2026-10-20T11:00:00"), ("Отработана", "2026-10-20T14:07:00"))},
            # в работе с 19.10 — в очереди
            {"id": "t2", "emp": "1c:", "dt": "2026-10-21", "created_at": "2026-10-19T09:00:00", "doc": "ЦБ-000990",
             "status": "ВРаботе", "posted": 0, "deleted": 0,
             "history": h(("Поступила", "2026-10-19T09:00:00"), ("ВРаботе", "2026-10-20T10:00:00"))},
            # копия заявки: история из исходной, 2024 год — вход на этап считается с даты заявки
            {"id": "t6", "emp": "1c:СтажерЛогистика", "dt": "2026-07-14", "created_at": "2026-07-14T11:35:50",
             "doc": "Тест 3", "status": "Поступила", "posted": 1, "deleted": 0,
             "history": h(("Поступила", "2024-09-10T11:04:09"))},
            # помечена на удаление: во «всего за период», но не в очереди и не в реестре
            {"id": "t3", "emp": "ivanov", "dt": "2026-10-05", "created_at": "2026-10-05T10:43:11", "doc": "000002287",
             "status": "Поступила", "posted": 1, "deleted": 1, "history": h(("Поступила", "2026-10-05T10:43:11"))},
            # сентябрьская, но всё ещё «Поступила» — в очереди на сегодня
            {"id": "t4", "emp": "1c:Якимова Алёна", "dt": "2026-09-24", "created_at": "2026-09-24T10:30:46",
             "doc": "000002256", "status": "Поступила", "posted": 1, "deleted": 0, "history": None},
            # старше окна синхронизации — не обновляется, в очередь не попадает
            {"id": "t5", "emp": "1c:Якимова Алёна", "dt": "2026-03-01", "created_at": "2026-03-01T10:00:00",
             "doc": "000001000", "status": "Поступила", "posted": 1, "deleted": 0},
        ]
        replace_rows(self.con, "transport_requests", rows)
        fwd = lambda i, status, ship=None, emp="1c:Ваганова Наталия", deleted=0, delivery=None: {
            "id": f"f{i}", "emp": emp, "dt": "2026-10-15", "created_at": "2026-10-15T10:00:00", "doc": f"ЦБ-0000019{i}",
            "status": status, "posted": 1, "deleted": deleted, "ship_date": ship, "delivery_date": delivery, "plan_sum": 60000}
        replace_rows(self.con, "forwarder_orders", [
            fwd(1, "ПоискТС", "2026-10-20"),                                   # плановая отгрузка прошла — просрочено
            fwd(2, "ГотовКОтгрузке", "2026-10-22", emp="1c:Якимова Алёна"),   # к отгрузке сегодня
            fwd(3, "Выдан", "2026-10-10"),                                     # доставлено, закрыто — поручение периода
            fwd(7, "ГотовКВыдачи", "2026-10-12", emp="1c:СтажерЛогистика"),    # доставлено, не закрыто
            fwd(4, "НазначенаМашина", "2026-10-01", deleted=1),                # помечено на удаление
            fwd(5, "ВПути", "2026-10-16", delivery="2026-10-21"),              # плановая поставка прошла — просрочено
            fwd(6, "ВПути", "2026-10-17", emp="1c:Якимова Алёна", delivery="2026-10-22")])  # к доставке сегодня
        r = metrics.logistics(self.con, {"ivanov": "Роман Иванов"}, "2026-10-01", "2026-10-31", since="2026-04-01",
                              today=TODAY)
        self.assertEqual(r["tiles"], {"total": 3, "deleted": 1, "in_work": 8, "overdue": 2, "ship_today": 1,
                                      "deliver_today": 1})
        self.assertEqual(r["statuses"], [["Поступила", 1], ["В работе", 1], ["Запрос информации", 0], ["Отработана", 1]])
        self.assertEqual([s["title"] for s in r["stages"]], ["На расчёте", "Подготовка отгрузки", "В пути",
                                                            "Доставлено, не закрыто", "Доставлено, закрыто"])
        calc, prep, transit = r["stages"][0], r["stages"][1], r["stages"][2]
        self.assertEqual((calc["now"], calc["came"], calc["left"], calc["max_days"]), (3, 2, 1, 100))  # «Тест 3» — 100 дней с даты заявки, а не с 2024 года
        self.assertEqual((prep["now"], prep["came"], prep["left"], prep["max_days"], prep["overdue"]), (2, None, None, 7, 1))
        self.assertEqual((transit["now"], transit["overdue"]), (2, 1))
        self.assertEqual([s["sourced"] for s in r["stages"]], [True] * 5)
        delivered, closed = r["stages"][3], r["stages"][4]
        self.assertEqual((delivered["now"], delivered["overdue"], closed["now"], closed["final"]), (1, None, 1, True))
        self.assertEqual([(p["name"], p["total"], p["overdue"]) for p in r["people"]], [("Логист", 8, 2)])  # логисты — одной строкой
        # все поручения периода во всех статусах (кроме помеченных на удаление), новые сверху
        self.assertEqual([(x["doc"], x["status"], x["logist"]) for x in r["forwarder_orders"]],
                         [("ЦБ-00000197", "Готов к выдаче", "Логист"), ("ЦБ-00000196", "В пути", "Логист"),
                          ("ЦБ-00000195", "В пути", "Логист"), ("ЦБ-00000193", "Выдан", "Логист"),
                          ("ЦБ-00000192", "Готов к отгрузке", "Логист"), ("ЦБ-00000191", "Поиск ТС", "Логист")])
        self.assertEqual([(x["doc"], x["route"], x["plan"]) for x in r["registry"]],
                         [("000002338", "Миасс → Когалым", 81200), ("ЦБ-000990", None, None)])

    def test_registry_customer_and_closed_date(self):
        h = lambda *x: json.dumps([list(i) for i in x], ensure_ascii=False)
        replace_rows(self.con, "transport_requests", [
            {"id": "r1", "emp": "ivanov", "dt": "2026-10-05", "created_at": "2026-10-05T10:00:00", "doc": "000002300",
             "status": "Отработана", "posted": 1, "deleted": 0, "sales_manager": "Петров П.", "due_at": "2026-10-06T00:00:00",
             "history": h(("Поступила", "2026-10-05T10:00:00"), ("ВРаботе", "2026-10-06T09:00:00"), ("Отработана", "2026-10-07T15:30:00"))},
            {"id": "r2", "emp": "ivanov", "dt": "2026-10-06", "created_at": "2026-10-06T10:00:00", "doc": "000002301",
             "status": "ВРаботе", "posted": 1, "deleted": 0, "sales_manager": "Сидоров С.", "history": h(("Поступила", "2026-10-06T10:00:00"))}])
        r = metrics.logistics(self.con, {}, "2026-10-01", "2026-10-31", since="2026-04-01", today=TODAY)
        got = {x["doc"]: (x["customer"], x["closed"], x["late_delta"]) for x in r["registry"]}
        self.assertEqual(got, {"000002300": ("Петров П.", "2026-10-07T15:30:00", -1), "000002301": ("Сидоров С.", None, None)})

    def test_forwarder_order_plan_minus_fact_days(self):
        row = {"id": "f1", "emp": "1c:Ваганова Наталия", "dt": "2026-10-15", "created_at": "2026-10-15T10:00:00",
               "doc": "ЦБ-1", "status": "ВПути", "posted": 1, "deleted": 0, "delivery_date": "2026-10-23",
               "dop": "2026-10-20", "dof": "2026-10-21", "dpp": "2026-10-23", "dfp": "2026-10-22"}
        empty = {**row, "id": "f2", "doc": "ЦБ-2", "dof": None, "dfp": None}
        replace_rows(self.con, "forwarder_orders", [row, empty])
        r = metrics.logistics(self.con, {}, "2026-10-01", "2026-10-31", since="2026-04-01", today=TODAY)
        got = {x["doc"]: (x["ship_delta"], x["delivery_delta"]) for x in r["forwarder_orders"]}
        self.assertEqual(got, {"ЦБ-1": (-1, 1), "ЦБ-2": (None, None)})  # ДОП−ДОФ, ДПП−ДФП; без факта — пусто

    def test_delivery_fact_from_supplier_order_lines(self):
        import datetime as dt
        from app.onec_logistics import delivery_facts

        class FakeOneC:  # заказ клиента 00ЦБ-000052: опоры (товар), «Транспортные расходы» ИТЕКО, аккредитив
            def _odata(self, entity, params):
                if entity == "Catalog_Номенклатура":
                    return [{"Ref_Key": "pole", "Description": "Опора освещения граненая"},
                            {"Ref_Key": "trans", "Description": "Транспортные расходы"},
                            {"Ref_Key": "paint", "Description": "Грунт-эмаль RAL 1023 транспортно-желтый"},
                            {"Ref_Key": "bank", "Description": "Аккредитив"}]
                return [{"ТТК_ЗаказКлиента_Key": "co52", "Товары": [
                            {"Номенклатура_Key": "pole", "СуммаСНДС": 180195}, {"Номенклатура_Key": "pole", "СуммаСНДС": 180195}]},
                        {"ТТК_ЗаказКлиента_Key": "co52", "Товары": [{"Номенклатура_Key": "trans", "СуммаСНДС": 165000},
                                                                     {"Номенклатура_Key": "trans", "СуммаСНДС": 9, "Отменено": True}]},
                        {"ТТК_ЗаказКлиента_Key": "co52", "Товары": [{"Номенклатура_Key": "bank", "СуммаСНДС": 23537.17},
                                                                     {"Номенклатура_Key": "paint", "СуммаСНДС": 500}]}]

        self.assertEqual(delivery_facts(FakeOneC(), dt.date(2026, 1, 1)), {"co52": 165000})

    def test_route_from_flagged_results(self):
        from app.onec_logistics import _route
        res = [{"Флаг": True, "АдресОтправки": "Миасс", "АдресДоставки": "г. Когалым\nдо 28.02", "ТранспортнаяКомпания": "да транс",
                "СтоимостьСНДС": 81200},
               {"Флаг": True, "АдресОтправки": "Челябинск", "АдресДоставки": "г. Когалым", "ТранспортнаяКомпания": "кит ",
                "СтоимостьСНДС": 11800},
               {"Флаг": False, "АдресОтправки": "Челябинск", "АдресДоставки": "г. Когалым", "ТранспортнаяКомпания": "байкал",
                "СтоимостьСНДС": 18800}]
        self.assertEqual(_route(res), ("Миасс → г. Когалым до 28.02; Челябинск → г. Когалым", "да транс, кит", 93000))
        self.assertEqual(_route([]), (None, None, None))


if __name__ == "__main__":
    unittest.main()
