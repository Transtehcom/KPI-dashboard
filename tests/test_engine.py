"""Тесты расчёта KPI. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import demo, manual, sync  # noqa: E402
from app.common import (connect, load_json, load_rules, month_add, month_bounds, replace_rows,  # noqa: E402
                        weeks_of_month, working_days)
from app.engine import Engine  # noqa: E402
from app.funnel import deal_funnel  # noqa: E402

TODAY = dt.date(2026, 10, 20)
EXAMPLE_RULES_DEP = load_rules()["departments"]["ОП"]  # «Отдел продаж (ОП)»
EXAMPLE = load_json("config.example.json")
# секции воронки сделок по названиям стадий (прежний вид настройки) — для проверки самого механизма секций
BY_NAMES = {**EXAMPLE, "deal_funnel": {"include_closed": False, "sections": [
    {"key": "work", "title": "В работе", "category_ids": [0, 4], "stage_names": ["Подготовка КП", "В работе"]},
    {"key": "precontract", "title": "Преддоговорные сделки", "stage_names": ["Преддоговорные сделки", "Подписание договора"]},
    {"key": "contract", "title": "Договорные сделки", "stage_names": ["Ожидает оплаты"]},
    {"key": "overdue", "title": "ПДЗ", "stage_names": ["Просроченная оплата", "Дебиторская задолженность"]}]}}
CONFIG = {
    "holidays": EXAMPLE["holidays"],
    "employees": [
        {"login": "boss", "name": "Руководитель", "role": "manager"},
        {"login": "lead", "name": "Ведущий", "position": "tender_lead", "role": "employee"},
        {"login": "tender", "name": "Тендерный", "position": "tender", "role": "employee"},
        {"login": "pm", "name": "ПМ", "position": "pm", "role": "employee"},
        {"login": "supply", "name": "Снабженец", "position": "supply", "role": "employee"},
    ],
}


def insert(con, table, rows):
    replace_rows(con, table, rows, emps=["__none__"])


def call(i, emp, day, lpr=1, secretary=0, next_step=1, duration=180, contact=None):
    return {"id": f"c{emp}{i}", "emp": emp, "dt": f"{day}T10:00:00", "kind": "call", "direction": "out",
            "duration": duration, "is_lpr": lpr, "secretary": secretary, "next_step": next_step, "status": "done",
            "contact": contact or f"CONTACT:{emp}{i}"}


def order(i, dt_, amount, **kw):
    row = {"id": f"o{i}", "emp": "supply", "doc": f"ЗП-{i}", "dt": dt_, "supplier": "Поставщик", "lot": None,
           "amount_novat": amount, "kp_count": 3, "relevant": 1, "evidence": 1, "affiliated": 0, "single_source": 0,
           "emergency": 0, "justified": 0, "deferred_share": 0, "deferral_days": 0, "customer_order": None}
    row.update(kw)
    return row


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.engine = Engine(self.con, self.rules, CONFIG)
        patcher = mock.patch("app.engine.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def params(self, position, code):
        return next(i["params"] for i in self.rules["positions"][position]["items"] if i["code"] == code)

    def calc(self, login, code, month, **params):
        e = self.engine.employee(login)
        return getattr(self.engine, "calc_" + code)(e, self.engine._ctx(month), {**self.params(e["position"], code), **params})

    def item(self, login, code, month):
        return next(i for i in self.engine.compute(login, month)["items"] if i["code"] == code)

    # ---------------------------------------------------------------- даты
    def test_each_week_belongs_to_one_month(self):
        months = [month_add("2026-01", k) for k in range(12)]
        weeks = [w for m in months for w in weeks_of_month(m)]
        self.assertEqual(len(weeks), len(set(weeks)))
        self.assertEqual(weeks_of_month("2026-10"), ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"])
        self.assertEqual(weeks_of_month("2026-09")[-1], "2026-09-21")

    def test_working_days_with_transferred_workday(self):
        first, last = month_bounds("2026-11")
        self.assertEqual(working_days(first, last, ["2026-11-04"]), 20)
        self.assertEqual(working_days(first, last, ["2026-11-04"], ["2026-11-07"]), 21)

    # ---------------------------------------------------------------- звонки
    def test_pm_calls_do_not_require_lpr_but_tender_calls_do(self):
        insert(self.con, "activities", [call(1, "pm", "2026-09-10", lpr=0), call(2, "pm", "2026-09-11", lpr=1),
                                        call(3, "pm", "2026-09-11", secretary=1),
                                        call(4, "tender", "2026-09-10", lpr=0), call(5, "tender", "2026-09-11", lpr=1)])
        pm = self.calc("pm", "operational_activity", "2026-09")["components"][0]
        self.assertEqual(pm["fact"], 2)
        tender = self.calc("tender", "base_development", "2026-09")["components"][0]
        self.assertEqual(tender["fact"], 1)

    def test_lead_calls_plain_touches_need_lpr_and_next_step(self):
        insert(self.con, "activities", [call(1, "lead", "2026-09-10", lpr=0, next_step=0), call(2, "lead", "2026-09-11"),
                                        call(3, "lead", "2026-09-11", secretary=1), call(4, "lead", "2026-09-12", duration=60)])
        r = self.calc("lead", "base_development", "2026-09", touches={"default": 70}, result_calls_per_day=3,
                      calls_plain=True)  # прежние нормы ведущего (с 2026-10-19 не действуют, но расчёт остаётся)
        comps = {c["name"]: c["fact"] for c in r["components"]}
        self.assertEqual(comps["Результативные касания"], 1)  # только звонок с ЛПР и следующим шагом
        calls = next(c["fact"] for c in r["components"] if c["name"].startswith("Звонки от 2 мин"))
        self.assertEqual(calls, 2)  # ЛПР и следующий шаг для звонков не нужны

    def test_lead_base_development_is_only_active_clients(self):
        insert(self.con, "activities", [call(1, "lead", "2026-10-05")])
        insert(self.con, "clients", [{"id": f"c{i}", "emp": "lead", "name": f"Клиент {i}", "need": 1, "strategy": int(i < 7)}
                                     for i in range(9)])
        r = self.calc("lead", "base_development", "2026-10")
        self.assertEqual([(c["name"], c["fact"], c["target"]) for c in r["components"]],
                         [("Активные клиенты с потребностью и стратегией", 7, 7)])
        self.assertEqual(len(r["details"]["rows"]), 7)
        self.assertIsNone(r["funnel"])

    def test_calls_one_per_contact_per_day(self):
        insert(self.con, "activities", [call(1, "tender", "2026-09-10", contact="CONTACT:7"),
                                        call(2, "tender", "2026-09-10", contact="CONTACT:7"),
                                        call(3, "tender", "2026-09-11", contact="CONTACT:7"),
                                        call(4, "tender", "2026-09-10", contact="CONTACT:8")])
        r = self.calc("tender", "base_development", "2026-09")
        self.assertEqual(r["components"][0]["fact"], 3)  # повторный звонок тому же контакту в тот же день не в счёт

    def test_call_funnel_for_period_follows_kpi_rules(self):
        insert(self.con, "activities", [call(1, "pm", "2026-10-05", lpr=0), call(2, "pm", "2026-10-06", duration=30),
                                        call(3, "pm", "2026-10-20", next_step=0),
                                        call(4, "tender", "2026-10-05", contact="CONTACT:7"),
                                        call(5, "tender", "2026-10-05", contact="CONTACT:7"),
                                        call(6, "tender", "2026-10-06", lpr=0),
                                        call(7, "lead", "2026-10-06", lpr=0)])
        d1, d2 = dt.date(2026, 10, 1), dt.date(2026, 10, 19)
        pm = self.engine.call_funnel("pm", d1, d2)
        self.assertEqual(pm["steps"], [("Исходящие звонки", 2), ("От 2 мин", 1), ("Не секретарь", 1),
                                       ("Зафиксирован след. шаг", 1)])  # ЛПР не нужен; звонок 20-го вне периода
        self.assertTrue(pm["kpi"].startswith("KPI "))
        tender = self.engine.call_funnel("tender", d1, d2)
        self.assertEqual(tender["steps"][0], ("Исходящие звонки", 2))  # два звонка контакту 7 в один день — один
        self.assertEqual(tender["steps"][-2:], [("Есть информация о ЛПР", 1), ("Зафиксирован след. шаг", 1)])
        lead = self.engine.call_funnel("lead", d1, d2)
        self.assertIsNone(lead["kpi"])  # у ведущего звонков в KPI нет — для справки, без требования ЛПР
        self.assertEqual(lead["steps"][-1], ("Зафиксирован след. шаг", 1))
        self.assertIsNone(self.engine.call_funnel("nobody", d1, d2))

    def test_exec_discipline_removed_from_october_weight_moved_to_receivables(self):
        for login, code in (("pm", "receivables_pm"), ("tender", "tender_receivables"), ("lead", "tender_receivables")):
            sep = self.engine.compute(login, "2026-09")["items"]
            octo = self.engine.compute(login, "2026-10")["items"]
            self.assertIn("exec_discipline", [i["code"] for i in sep])  # прошлые месяцы — по правилам того времени
            self.assertNotIn("exec_discipline", [i["code"] for i in octo])
            rec_sep = next(i for i in sep if i["code"] == code)
            rec_oct = next(i for i in octo if i["code"] == "receivables_overdue")  # с октября — шкала по просрочке
            self.assertEqual((rec_sep["n"], rec_sep["weight"], rec_sep["amount"]), (6, 0.10, 1000))
            if login == "pm":
                self.assertEqual((rec_oct["n"], rec_oct["weight"], rec_oct["amount"]), (5, 0.20, 2000))
            else:  # тендерный отдел: 10% — шкала по просрочке, ещё 10% — «Обеспечение и штрафы» (возвращено 26.10.2026)
                self.assertEqual((rec_oct["n"], rec_oct["weight"], rec_oct["amount"]), (5, 0.10, 1000))
                pen = next(i for i in octo if i["code"] == "tender_receivables")
                self.assertEqual((pen["n"], pen["weight"], pen["amount"], pen["title"]), (6, 0.10, 1000, "Обеспечение и штрафы"))
            self.assertAlmostEqual(sum(i["weight"] for i in octo), 1.0)
        supply = [i["code"] for i in self.engine.compute("supply", "2026-10")["items"]]
        self.assertIn("exec_discipline", supply)  # у снабженца пункт остаётся
        cols = next(g for g in self.engine.overview("2026-10")["groups"] if g["position"] == "pm")["columns"]
        self.assertEqual([c["n"] for c in cols], [1, 2, 3, 4, 5])

    def test_receivables_overdue_scale_from_october(self):
        # просрочка в рабочих днях → доля выплаты: до 7 — 100%, 8–14 — 50%, от 15 — 0%; только этот пункт
        cases = [("2026-10-01", "2026-10-01", 0, 1.0, "met"),  # оплачено в срок
                 ("2026-10-01", "2026-10-12", 7, 1.0, "met"),
                 ("2026-10-01", "2026-10-13", 8, 0.5, "partial"),
                 ("2026-09-30", "2026-10-20", 14, 0.5, "partial"),
                 ("2026-09-29", "2026-10-20", 15, 0.0, "progress_fail")]  # октябрь ещё идёт (сегодня 20.10)
        for login in ("pm", "tender", "lead"):
            for due, paid, days, share, status in cases:
                with self.subTest(login=login, days=days):
                    replace_rows(self.con, "sales", [{"id": f"s-{login}", "emp": login, "dt": "2026-09-15", "doc": "РТУ-1",
                                                      "client": "Клиент", "revenue": 1000, "due_date": due, "paid_date": paid}],
                                 emps=[login])
                    r = self.engine.compute(login, "2026-10")
                    it = next(i for i in r["items"] if i["code"] == "receivables_overdue")
                    amount = 2000 if login == "pm" else 1000  # у тендерного отдела вторая половина — «Обеспечение и штрафы»
                    self.assertEqual((it["n"], it["amount"], it["components"][0]["fact"]), (5, amount, days))
                    self.assertEqual((it["pct"], it["payout"], it["status"]), (share, round(amount * share), status))
                    self.assertEqual(r["zero_all"], [])  # весь KPI не обнуляется
        # сентябрь — по прежним пунктам (у менеджера ОП просрочка > 14 дней обнуляла весь KPI)
        self.assertEqual([i["code"] for i in self.engine.compute("tender", "2026-09")["items"]][-1], "tender_receivables")
        self.assertEqual([i["code"] for i in self.engine.compute("pm", "2026-09")["items"]][-1], "receivables_pm")

    def test_receivables_overdue_dates_entered_by_manager(self):
        from app.common import month_bounds as mb
        replace_rows(self.con, "sales", [{"id": "s1", "emp": "tender", "dt": "2026-10-05", "doc": "РТУ-7", "client": "К",
                                          "revenue": 500}], emps=["tender"])
        manual.set_value(self.con, "sales", "s1", "due_date", "2026-10-06", "boss")
        self.assertEqual(manual.entry(self.con, "receivables_overdue", "tender", *mb("2026-10"))["rows"][0]["values"]["due_date"],
                         "2026-10-06")

    def test_receivables_overdue_without_due_dates_is_no_data(self):
        it = next(i for i in self.engine.compute("pm", "2026-10")["items"] if i["code"] == "receivables_overdue")
        self.assertEqual((it["pct"], it["payout"]), (None, 0))

    def test_tender_conversion_by_report_columns_from_october(self):
        # («КП → Заказ» + «Выигран») / («КП» + «Валидные заявки») по столбцам отчёта 1С «Отчёт ОП», за месяц;
        # «КП» отчёта = КП менеджера + все его тендеры, «КП → Заказ» = КП и тендеры с заказом клиента
        for emp in ("tender", "lead"):
            insert(self.con, "kp", [{"id": f"{emp}k1", "emp": emp, "dt": "2026-10-03", "ordered": 1},
                                    {"id": f"{emp}k2", "emp": emp, "dt": "2026-10-04", "ordered": 0},
                                    {"id": f"{emp}k3", "emp": emp, "dt": "2026-10-05", "ordered": 0},
                                    {"id": f"{emp}k0", "emp": emp, "dt": "2026-09-30", "ordered": 1}])  # другой месяц
            insert(self.con, "tenders_1c", [{"id": f"{emp}t1", "emp": emp, "dt": "2026-10-02", "status": "Действует", "ordered": 0},
                                            {"id": f"{emp}t2", "emp": emp, "dt": "2026-10-06", "status": "Выиграно", "ordered": 1},
                                            {"id": f"{emp}t3", "emp": emp, "dt": "2026-10-07", "status": "Отменено", "ordered": 0},
                                            {"id": f"{emp}t4", "emp": emp, "dt": "2026-10-08", "status": "Черновик", "ordered": 0}])
            it = self.item(emp, "conversion_kp_tenders", "2026-10")
            self.assertEqual((it["n"], it["weight"]), (1, 0.25))
            # КП 3 + тендеров 4 = 7; КП → Заказ 1 + 1 = 2; валидные 2; выигран 1 → (2 + 1) / (7 + 2)
            self.assertAlmostEqual(it["components"][0]["fact"], 3 / 9)
            self.assertEqual(it["components"][0]["target"], 0.10)
            ex = {x["label"]: x["value"] for x in it["extra"]}
            self.assertEqual((ex["КП, шт."], ex["КП → Заказ, шт."], ex["Валидные заявки, шт."], ex["Выиграно, шт."]), (7, 2, 2, 1))
            self.assertEqual(self.engine.compute(emp, "2026-09")["items"][0]["code"], "conversion_tender")  # сентябрь — как было

    def test_incoming_calls_do_not_count(self):
        insert(self.con, "activities", [call(1, "tender", "2026-09-10"), {**call(2, "tender", "2026-09-11"), "direction": "in"}])
        r = self.calc("tender", "base_development", "2026-09")
        self.assertEqual(r["components"][0]["fact"], 1)  # входящий звонок в KPI не идёт

    def test_calls_norm_is_monthly_not_every_day(self):
        sup = next(e for e in CONFIG["employees"] if e["login"] == "tender")
        self.addCleanup(sup.pop, "absences", None)
        sup["absences"] = [{"from": "2026-09-21", "to": "2026-09-30", "kind": "vacation"}]
        # 42 звонка за 2 дня: в остальные дни звонков нет, но месячная норма 3 × 14 раб. дн. выполнена
        insert(self.con, "activities", [call(f"{d}{k}", "tender", d) for d in ("2026-09-01", "2026-09-02") for k in range(21)])
        c = self.calc("tender", "base_development", "2026-09")["components"][0]
        self.assertEqual((c["fact"], c["target"]), (42, 42))  # 22 рабочих дня минус 8 дней отпуска
        self.assertEqual(c["pct"], 1.0)

    def test_pm_calls_target_is_per_working_day(self):
        comp = self.calc("pm", "operational_activity", "2026-01")["components"][0]
        wd = self.engine._ctx("2026-01")["wd"]
        self.assertEqual(comp["target"], 15 * wd)
        self.assertLess(comp["target"], 320)

    def test_pm_norms_exclude_vacation_days(self):
        pm = next(e for e in CONFIG["employees"] if e["login"] == "pm")
        self.addCleanup(pm.pop, "absences", None)
        pm["absences"] = [{"from": "2026-09-21", "to": "2026-09-30", "kind": "vacation"}]
        calls, kp = self.calc("pm", "operational_activity", "2026-09")["components"][:2]
        self.assertEqual((calls["target"], kp["target"]), (15 * 14, 15 * 14))

    def test_truancy_does_not_reduce_monthly_norm(self):
        emp = next(e for e in CONFIG["employees"] if e["login"] == "tender")
        self.addCleanup(emp.pop, "absences", None)
        emp["absences"] = [{"from": "2026-09-21", "to": "2026-09-30", "kind": "vacation"},
                           {"from": "2026-09-01", "to": "2026-09-02", "kind": "truancy"}]
        c = self.calc("tender", "base_development", "2026-09")["components"][0]
        self.assertEqual(c["target"], 3 * 14)  # отпуск (8 дней) вычтен, прогул (2 дня) — нет
        r = self.engine.compute("tender", "2026-09")
        self.assertEqual((r["absence"]["days"], r["absence"]["truancy_days"]), (8, 2))
        self.assertTrue(r["absence"]["periods"][0].startswith("Отпуск 21.09"))

    def test_kp_needs_letter_in_b24(self):
        insert(self.con, "kp", [{"id": f"k{i}", "emp": "pm", "dt": "2026-09-10", "confirmed": 1} for i in range(5)])
        insert(self.con, "kp_letters", [{"id": f"m{i}", "emp": "pm", "dt": "2026-09-10T12:00:00"} for i in range(3)])
        comp = self.calc("pm", "operational_activity", "2026-09")["components"][1]
        self.assertEqual(comp["fact"], 3)

    # ---------------------------------------------------------------- валовая прибыль
    def test_gross_profit_counts_in_month_of_signing(self):
        insert(self.con, "sales", [
            {"id": "s1", "emp": "pm", "dt": "2026-09-25", "doc": "УПД-1", "revenue": 1000, "cost": 600, "signed": 1,
             "signed_date": "2026-10-03"},
            {"id": "s2", "emp": "pm", "dt": "2026-09-05", "doc": "УПД-2", "revenue": 500, "cost": 400, "signed": 1,
             "signed_date": "2026-09-08"},
            {"id": "s3", "emp": "pm", "dt": "2026-09-06", "doc": "УПД-3", "revenue": 900, "cost": 100, "signed": 0},
        ])
        sep = self.calc("pm", "gross_profit", "2026-09", metric="gross_profit")
        self.assertEqual(sep["components"][0]["fact"], 100)
        self.assertEqual(len(sep["details"]["rows"]), 3)  # 1 засчитан + 2 ждут подписи на конец сентября
        self.assertEqual(self.calc("pm", "gross_profit", "2026-10", metric="gross_profit")["components"][0]["fact"], 400)
        # по регламенту сейчас считается выручка без НДС: в октябре подписан УПД-1 на 1000
        self.assertEqual(self.calc("pm", "gross_profit", "2026-10")["components"][0]["fact"], 1000)

    def test_gross_profit_without_cost_is_no_data(self):
        # себестоимость в 1С не рассчитана: ВП не должна совпасть с выручкой
        insert(self.con, "sales", [{"id": "s1", "emp": "pm", "dt": "2026-09-05", "doc": "УПД-1", "revenue": 1000,
                                    "cost": None, "signed": 1}])
        r = self.calc("pm", "gross_profit", "2026-09", metric="gross_profit")
        self.assertIsNone(r["components"][0]["fact"])
        self.assertIsNone(r["components"][0]["pct"])

    def test_receivables_without_1c_data_is_no_data(self):
        r = self.calc("pm", "receivables_pm", "2026-09")
        self.assertEqual([c["fact"] for c in r["components"]], [None, None])
        pen = next(c for c in self.calc("lead", "tender_receivables", "2026-09")["components"] if "Штрафы" in c["name"])
        self.assertIsNone(pen["fact"])

    def test_manual_payment_dates_feed_receivables_and_survive_sync(self):
        insert(self.con, "sales", [{"id": "s1", "emp": "pm", "dt": "2026-08-20", "doc": "УПД-1", "revenue": 1000, "signed": 1}])
        manual.set_value(self.con, "sales", "s1", "due_date", "2026-09-05", "boss")
        manual.set_value(self.con, "sales", "s1", "paid_date", "2026-09-10", "boss")
        # синхронизация заменяет таблицу, ручные значения накладываются заново
        replace_rows(self.con, "sales", [{"id": "s1", "emp": "pm", "dt": "2026-08-20", "doc": "УПД-1", "revenue": 1000,
                                          "signed": 1}], emps=["pm"])
        manual.apply(self.con)
        r = self.calc("pm", "receivables_pm", "2026-09")
        self.assertEqual([c["fact"] for c in r["components"]], [0.0, 5])
        with self.assertRaises(ValueError):
            manual.set_value(self.con, "sales", "s1", "due_date", "2026-13-01", "boss")

    def test_manual_penalties(self):
        def fact():
            comps = self.calc("lead", "tender_receivables", "2026-09")["components"]
            return next(c["fact"] for c in comps if "Штрафы" in c["name"])
        self.assertIsNone(fact())  # ничего не внесено — ждёт ввода
        manual.set_penalty_check(self.con, "lead", "2026-09", True, "boss")
        self.assertEqual(fact(), 0)
        manual.add_penalty(self.con, "lead", "2026-09-12", "5000", "просрочка оплаты", "boss")
        self.assertEqual(fact(), 1)
        pid = self.con.execute("SELECT id FROM manual_penalties").fetchone()["id"]
        with self.assertRaises(ValueError):
            manual.delete_penalty(self.con, "tender", pid)  # чужой штраф
        manual.delete_penalty(self.con, "lead", pid)
        manual.set_penalty_check(self.con, "lead", "2026-09", False, "boss")
        self.assertIsNone(fact())

    def test_supply_kpi_awaits_manual_flags(self):
        insert(self.con, "supplier_orders", [order(1, "2026-09-03", 600000, deferred_share=None, deferral_days=None,
                                                   relevant=None, evidence=None, kp_count=3)])
        self.assertEqual(self.item("supply", "deferred_payment", "2026-09")["status"], "awaiting")
        for f, v in (("deferred_share", "30"), ("deferral_days", "14"), ):
            manual.set_value(self.con, "supplier_orders", "o1", f, v, "boss")
        self.assertEqual(self.calc("supply", "deferred_payment", "2026-09")["components"][0]["fact"], 1.0)

    # ---------------------------------------------------------------- активные клиенты
    def test_active_clients_past_month_uses_snapshot(self):
        insert(self.con, "clients", [{"id": f"cl{i}", "emp": "lead", "name": f"К{i}", "need": 1, "strategy": 1,
                                      "updated": "2026-10-19T10:00:00"} for i in range(8)])
        self.con.executemany("INSERT INTO clients_monthly VALUES ('2026-09', ?, 'lead', ?, 1, ?)",
                             [(f"cl{i}", f"К{i}", int(i < 5)) for i in range(8)])
        sep = [c for c in self.calc("lead", "base_development", "2026-09")["components"] if "Активные" in c["name"]][0]
        octo = [c for c in self.calc("lead", "base_development", "2026-10")["components"] if "Активные" in c["name"]][0]
        self.assertEqual(sep["fact"], 5)
        self.assertEqual(octo["fact"], 8)

    def test_snapshot_clients(self):
        insert(self.con, "clients", [{"id": "a", "emp": "lead", "name": "А", "need": 1, "strategy": 1}])
        sync.snapshot_clients(self.con, "2026-10")
        sync.snapshot_clients(self.con, "2026-10")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM clients_monthly").fetchone()[0], 1)

    # ---------------------------------------------------------------- снабжение
    def test_competitive_purchases_counts_requests_with_three_suppliers(self):
        insert(self.con, "supply_funnel", [
            {"id": "f1", "emp": "supply", "dt": "2026-09-02", "doc": "З1", "stage": "ordered", "suppliers": 3},
            {"id": "f2", "emp": "supply", "dt": "2026-09-03", "doc": "З2", "stage": "done", "suppliers": 2},
            {"id": "f3", "emp": "supply", "dt": "2026-09-04", "doc": "З3", "stage": "done", "suppliers": 7},
            {"id": "f4", "emp": "supply", "dt": "2026-09-05", "doc": "З4", "stage": "open", "suppliers": 1},
            {"id": "f5", "emp": "supply", "dt": "2026-10-01", "doc": "З5", "stage": "done", "suppliers": 0}])
        r = self.calc("supply", "competitive_purchases", "2026-09")
        self.assertAlmostEqual(r["components"][0]["fact"], 2 / 3)
        self.assertIsNone(self.calc("supply", "competitive_purchases", "2026-08")["components"][0]["fact"])

    def test_split_orders_raise_warning(self):
        insert(self.con, "supplier_orders", [order(1, "2026-09-02", 300000, lot="L1"),
                                             order(2, "2026-09-03", 300000, lot="L1"),
                                             order(3, "2026-09-04", 100000, customer_order="ЗК-1"),
                                             order(4, "2026-09-05", 100000, customer_order="ЗК-1")])
        r = self.engine.compute("supply", "2026-09")
        self.assertEqual(len(r["warnings"]), 1)
        alerts = self.engine.overview("2026-09")["alerts"]
        self.assertTrue(any(a["level"] == "warning" and a["login"] == "supply" for a in alerts))

    # ---------------------------------------------------------------- обнуление и выплата
    def test_order_number_matching(self):
        from app.bitrix24 import order_key
        self.assertEqual({order_key(x) for x in ("00ЦБ-000067", "ЦБ-67", "67", " 00ЦБ-000067 ")}, {"67"})
        self.assertIsNone(order_key("без номера"))

    def test_tender_conversion_from_1c_report(self):
        statuses = ["Действует"] * 6 + ["Выиграно"] + ["Проиграно"] * 9 + ["Отменено"] * 5 + ["Черновик"]
        insert(self.con, "tenders_1c", [{"id": f"t{i}", "emp": "tender", "dt": "2026-09-10", "status": s}
                                        for i, s in enumerate(statuses)])
        r = self.calc("tender", "conversion_tender", "2026-09")
        self.assertAlmostEqual(r["components"][0]["fact"], 1 / 16)  # как у Пепеляевой в «Отчёте ОП»: 6,25%
        extra = {x["label"]: x["value"] for x in r["extra"]}
        self.assertEqual((extra["Валидные заявки"], extra["Действует"]), (16, 6))
        self.assertAlmostEqual(extra["Win-rate"], 0.1)
        self.assertAlmostEqual(self.calc("tender", "conversion_tender", "2026-09", basis="decided")["components"][0]["fact"], 0.1)

    def test_pm_conversion_kp_to_customer_order(self):
        insert(self.con, "kp", [{"id": f"k{i}", "emp": "pm", "dt": "2026-09-10", "ordered": int(i < 2)} for i in range(61)])
        r = self.calc("pm", "conversion_sales", "2026-09")
        self.assertAlmostEqual(r["components"][0]["fact"], 2 / 61)  # 3,28%, как в «Отчёте ОП»
        self.assertEqual(r["components"][0]["target"], 0.07)

    def test_pm_conversion_target_is_10_percent_from_october(self):
        self.con.execute("INSERT OR REPLACE INTO baselines VALUES ('pm', 'conversion_last_year', 0.2)")
        insert(self.con, "kp", [{"id": f"k{i}", "emp": "pm", "dt": "2026-10-10", "ordered": int(i < 1)} for i in range(10)])
        r = self.item("pm", "conversion_sales", "2026-10")
        self.assertEqual(r["components"][0]["target"], 0.10)  # норма прошлого года больше не действует
        self.assertFalse(r["baseline"])
        self.assertEqual(r["target_text"], "Конверсия 10%")
        self.assertAlmostEqual(self.item("pm", "conversion_sales", "2026-09")["components"][0]["target"], 0.22)

    def test_company_need_and_strategy_text_fields(self):
        from app.bitrix24 import Bitrix24
        yes = ["Да", " да ", "ДА.", "да, проект модернизации", "Y", "1", True]
        no = ["Нет", "нет", "не выявлена", "", None, "N", "-", "пока нет"]
        self.assertEqual([Bitrix24.yes(v) for v in yes], [1] * len(yes))
        self.assertEqual([Bitrix24.yes(v) for v in no], [0] * len(no))
        self.assertEqual([Bitrix24.filled(v) for v in ["Ежемесячные КП по БПЛА", "звонок раз в неделю"]], [1, 1])
        self.assertEqual([Bitrix24.filled(v) for v in ["", None, "-", " — ", "нет", "Нет."]], [0] * 6)

    def test_next_step_is_any_activity_except_telephony_calls(self):
        from app.bitrix24 import Bitrix24
        f = Bitrix24.next_step_activity
        self.assertFalse(f({"ID": "5", "PROVIDER_ID": "CRM_TODO"}, "5"))  # сам звонок
        self.assertFalse(f({"ID": "6", "TYPE_ID": "2", "PROVIDER_ID": "VOXIMPLANT_CALL"}, "5"))  # повторный звонок
        for provider in ("CRM_TODO", "CRM_TASKS_TASK", "CRM_EMAIL", None):
            self.assertTrue(f({"ID": "7", "PROVIDER_ID": provider}, "5"), provider)

    def test_lpr_field_ignores_dashes_plus_and_placeholder(self):
        from app.bitrix24 import Bitrix24
        no = ["", None, "-", "—", ".", "+", " + ", "нет", "Нет", "ФИО лица", "ф.и.о. лица", False, "0"]
        yes = ["Иванов Иван Петрович", "Иванов И.И., директор", "1", True, ["5"]]
        self.assertEqual([Bitrix24.lpr(v) for v in no], [0] * len(no))
        self.assertEqual([Bitrix24.lpr(v) for v in yes], [1] * len(yes))

    def test_lead_qualified_once_and_lost_leads_skipped(self):
        full = {"need": 1, "budget": 1, "lpr": 1, "solvency": 1, "next_step": 1}
        rows = [{"id": "l1", "emp": "pm", **full, "lost": False}, {"id": "l2", "emp": "pm", **full, "lost": True},
                {"id": "l3", "emp": "pm", **full, "next_step": 0, "lost": False}]
        out = sync.qualify_leads(self.con, rows)
        self.assertEqual([(r["id"], bool(r.get("qualified_date"))) for r in out], [("l1", True), ("l3", False)])
        first = out[0]["qualified_date"]
        # позже условие пропало и лид проигран — месяц квалификации сохраняется
        again = sync.qualify_leads(self.con, [{"id": "l1", "emp": "pm", **full, "need": 0, "lost": True}])
        self.assertEqual((again[0]["qualified_date"], again[0]["need"]), (first, 1))

    def test_failed_realization_deals_norm_zero(self):
        self.assertEqual(self.item("tender", "tender_operations", "2026-09")["status"], "met")
        insert(self.con, "deal_failures", [
            {"id": "f1", "emp": "tender", "title": "Поставка", "dt": "2026-09-10", "reason": "Сделка провалена при подготовке предложения"},
            {"id": "f2", "emp": "tender", "title": "Другая", "dt": "2026-08-31"},   # прошлый месяц
            {"id": "f3", "emp": "lead", "title": "Чужая", "dt": "2026-09-12"}])
        it = self.item("tender", "tender_operations", "2026-09")
        self.assertEqual((it["components"][0]["fact"], it["status"]), (1, "fail"))
        self.assertEqual(it["details"]["rows"][0][3], "Сделка провалена при подготовке предложения")

    def test_team_revenue_plan_split_between_sales_and_tender_staff(self):
        # 150 млн на месяц делятся на pm, lead, tender из CONFIG; свой план руководителя важнее
        for login in ("pm", "lead", "tender"):
            self.assertEqual(self.calc(login, "gross_profit", "2026-09")["plan"]["value"], 50_000_000)
        self.con.execute("INSERT INTO plans VALUES ('tender', '2026-09', 'revenue', 7000000)")
        self.assertEqual(self.calc("tender", "gross_profit", "2026-09")["plan"]["value"], 7_000_000)

    def test_pm_overdue_over_14_days_fails_only_receivables(self):
        # до октября 2026 сроки и даты оплаты реализаций вводит руководитель; дебиторка не обнуляет весь KPI
        insert(self.con, "sales", [{"id": "s1", "emp": "pm", "doc": "УПД", "dt": "2026-08-01", "due_date": "2026-09-01",
                                    "paid_date": "2026-09-20", "revenue": 1}])
        r = self.engine.compute("pm", "2026-09")
        self.assertEqual(r["zero_all"], [])
        self.assertEqual(next(i for i in r["items"] if i["code"] == "receivables_pm")["status"], "fail")

    def test_kp_from_1c_plus_letters_without_double_count(self):
        from app.engine import match_kp_letters, org_key
        self.assertEqual(org_key('ПАО "КОРПОРАЦИЯ ВСМПО-АВИСМА"'), org_key("КОРПОРАЦИЯ ВСМПО-АВИСМА ПАО"))
        insert(self.con, "kp", [{"id": "k1", "emp": "pm", "dt": "2026-10-07", "doc": "00ЦБ-000959", "client": 'АО "АБАКАНСКАЯ ТЭЦ"'},
                                {"id": "k2", "emp": "pm", "dt": "2026-10-20", "doc": "00ЦБ-001001", "client": "ГОРТЕПЛОЭНЕРГО МУП"},
                                {"id": "k3", "emp": "pm", "dt": "2026-10-20", "doc": "00ЦБ-001009", "client": "КАМАЗ ПАО"}])
        insert(self.con, "kp_letters", [
            {"id": "l1", "emp": "pm", "dt": "2026-10-02", "deal": 'АО "АБАКАНСКАЯ ТЭЦ"', "subject": "Письмо о защите БПЛА"},
            {"id": "l2", "emp": "pm", "dt": "2026-10-02", "deal": 'ООО "КАМА"', "subject": "Письмо о защите БПЛА"},
            {"id": "l3", "emp": "pm", "dt": "2026-10-03", "deal": 'МУП "Гортеплоэнерго"', "subject": "Письмо о сотрудничестве"}])
        # КП 20.10 и письмо 03.10 в «Гортеплоэнерго» — дальше 7 дней: разные КП; «Кама» ≠ «КАМАЗ»
        self.assertEqual(set(match_kp_letters(self.engine.q("SELECT * FROM kp"), self.engine.q("SELECT * FROM kp_letters"), 7)), {"k1"})
        it = self.item("pm", "operational_activity", "2026-10")
        kp = next(c for c in it["components"] if c["name"].startswith("КП"))
        self.assertEqual(kp["fact"], 5)  # 3 КП в 1С + 3 письма − 1 совпадение
        it = self.item("tender", "base_development", "2026-10")
        self.assertEqual(next(c for c in it["components"] if c["name"].startswith("КП вне тендера"))["fact"], 0)

    def test_secretary_by_call_analysis_from_october(self):
        insert(self.con, "activities", [call(1, "pm", "2026-10-05"), call(2, "pm", "2026-10-06"), call(3, "pm", "2026-10-07"),
                                        call(4, "pm", "2026-09-07")])
        for rid, secr in (("cpm1", 1), ("cpm2", 1), ("cpm4", 1)):  # Claude: только секретарь
            self.con.execute("INSERT INTO call_analysis (id, version, result, secretary) VALUES (?, 4, '{}', ?)", (rid, secr))
        self.con.execute("INSERT INTO call_marks VALUES ('cpm2', 0, 'boss', '2026-10-08')")  # руководитель: не секретарь
        steps = dict(self.engine.call_funnel("pm", dt.date(2026, 10, 1), dt.date(2026, 10, 31))["steps"])
        self.assertEqual(steps["Не секретарь"], 2)  # cpm1 отсеян по разбору, cpm2 вернул руководитель
        steps = dict(self.engine.call_funnel("pm", dt.date(2026, 9, 1), dt.date(2026, 9, 30))["steps"])
        self.assertEqual(steps["Не секретарь"], 1)  # сентябрь — только по должности в Битрикс24

    def test_receivables_overdue_from_1c_customer_orders(self):
        insert(self.con, "receivables", [{"id": "r1", "emp": "pm", "doc": "00ЦБ-000098", "client": "Кедр",
                                          "ship_date": "2026-07-14", "due_date": "2026-10-13", "paid_date": "2026-10-16",
                                          "amount": 50153.16}])
        it = self.item("pm", "receivables_overdue", "2026-10")
        self.assertEqual(it["components"][0]["fact"], 3)  # срок 13.10, оплата 16.10: 3 рабочих дня
        self.assertEqual((it["status"], it["details"]["columns"][0]), ("met", "Заказ клиента"))
        self.assertEqual(self.calc("pm", "receivables_pm", "2026-09")["components"][0]["fact"], None)  # сентябрь — без 1С
        it = self.item("lead", "receivables_overdue", "2026-10")  # в выгрузке 1С заказов сотрудника нет — просрочки нет
        self.assertEqual((it["components"][0]["fact"], it["status"]), (0, "met"))

    def test_proportional_close_threshold_follows_min_pct(self):
        self.rules["payout"] = {"mode": "proportional", "min_pct": 0.9}
        insert(self.con, "leads", [{"id": f"l{i}", "emp": "pm", "qualified_date": "2026-09-10T10:00:00", "need": 1,
                                    "budget": 1, "lpr": 1, "solvency": 1, "next_step": 1} for i in range(8)])
        it = self.item("pm", "qualified_leads", "2026-09")
        self.assertEqual(it["status"], "fail")  # 80% < min_pct 90%
        self.assertEqual(it["payout"], 0)

    def test_supply_funnel_requests_to_customer_orders(self):
        stages = ["ordered", "ordered", "done", "done", "done", "open", "cancelled"]
        insert(self.con, "supply_funnel", [{"id": f"f{i}", "emp": "supply", "dt": "2026-09-05", "doc": f"ЗС-{i}",
                                            "customer_order": "12" if s == "ordered" else None, "stage": s}
                                           for i, s in enumerate(stages)] +
               [{"id": "old", "emp": "supply", "dt": "2026-08-31", "doc": "ЗС-0", "stage": "ordered"}])
        r = self.calc("supply", "supply_funnel", "2026-09")
        c = r["components"][0]
        self.assertAlmostEqual(c["fact"], 2 / 5)  # 2 заявки с заказом из 5 отработанных
        self.assertAlmostEqual(c["target"], 0.10)  # конверсия 10% — 100% плана
        self.assertEqual([n for _, n in r["funnel"]["steps"]], [7, 5, 2])
        self.assertEqual(self.item("supply", "supply_funnel", "2026-09")["status"], "met")

    def test_absence_whole_month_not_counted(self):
        sup = next(e for e in CONFIG["employees"] if e["login"] == "supply")
        self.addCleanup(sup.pop, "absences", None)
        sup["absences"] = [{"from": "2026-06-01", "to": "2026-09-15", "reason": "не работала"}]
        r = self.engine.compute("supply", "2026-07")
        self.assertTrue(r["absent"])
        self.assertEqual({i["status"] for i in r["items"]}, {"absent"})
        self.assertEqual(r["payout"], 0)
        part = self.engine.compute("supply", "2026-09")  # часть месяца — KPI считается как обычно
        self.assertFalse(part["absent"])
        self.assertEqual(part["absence"]["days"], 11)
        ov = self.engine.overview("2026-07")
        self.assertNotIn("supply", [a["login"] for a in ov["alerts"]])
        self.assertEqual(ov["totals"]["employees"], 3)  # отсутствовавший весь месяц — вне итогов

    # ---------------------------------------------------------------- воронка сделок
    def test_deal_funnel_sections_and_filters(self):
        def row(i, stage, cat="0", emp="pm", semantic="P", entity="deal"):
            return {"id": f"{entity}{i}", "entity": entity, "emp": emp, "responsible_id": "14" if emp else "25",
                    "responsible": "ПМ" if emp else "Другой", "category_id": cat, "category": f"Воронка {cat}",
                    "stage_id": f"S{i}", "stage": stage, "stage_sort": 10, "semantic": semantic, "title": f"Запись {i}",
                    "amount": 100, "stage_since": "2026-10-01T09:00:00", "snapshot": "2026-10-20T09:00:00"}
        replace_rows(self.con, "crm_deals", [
            row(1, "Выявление потребности"),                       # стадии нет в секциях
            row(2, "подготовка кп"),                               # название без учёта регистра
            row(3, "В работе", cat="4", emp=None),
            row(4, "Подготовка КП", cat="3"),                      # «В работе» берёт только воронки 0 и 4
            row(5, "Подписание договора", cat="2"), row(6, "Ожидает оплаты", cat="3"),
            row(7, "Просроченная оплата", cat="2"), row(8, "Сделка успешна", semantic="S"),
            row(9, "Распределённые", cat="lead", entity="lead"), row(10, "Ожидают звонка", cat="lead", entity="lead"),
            row(11, "Поиск контактов", cat="lead", entity="lead"), row(12, "Новому менеджеру", cat="lead", entity="lead"),
        ])
        f = deal_funnel(self.con, BY_NAMES)
        self.assertEqual([(s["title"], s["count"]) for s in f["sections"]],
                         [("В работе", 2), ("Преддоговорные сделки", 1), ("Договорные сделки", 1), ("ПДЗ", 1)])
        self.assertEqual(f["total"], 5)  # закрытая сделка и лиды в воронку сделок не входят
        self.assertEqual({o["stage"] for o in f["other_stages"]}, {"Выявление потребности", "Подготовка КП"})
        self.assertEqual(f["sections"][0]["share_of_total"], 0.4)
        other = deal_funnel(self.con, BY_NAMES, login="id:25")
        self.assertEqual([s["count"] for s in other["sections"]], [1, 0, 0, 0])
        self.assertEqual(len(other["filters"]["responsibles"]), 2)  # фильтр не сужает список вариантов
        both = deal_funnel(self.con, BY_NAMES, login=["id:25", "pm"])  # несколько ответственных складываются
        self.assertEqual([s["count"] for s in both["sections"]], [s["count"] for s in f["sections"]])
        self.assertEqual(both["applied"]["logins"], ["id:25", "pm"])
        leads = deal_funnel(self.con, EXAMPLE, kind="leads")
        self.assertEqual([(s["title"], s["count"]) for s in leads["sections"]],
                         [("Поступило", 3), ("Не обработано", 1), ("В работе", 2), ("Запросили ТКП", 0)])
        self.assertEqual(leads["sections"][0]["share_of_total"], 1.0)  # «Поступило» — сумма трёх этапов ниже
        self.assertEqual(leads["total_amount"], 300)
        self.assertEqual(leads["other_stages"], [{"category": "Воронка lead", "stage": "Новому менеджеру", "count": 1}])

    def test_deal_funnel_sections_by_stage_ids_of_two_funnels(self):
        def row(i, stage_id, cat):
            return {"id": f"deal{i}", "entity": "deal", "emp": "pm", "responsible_id": "14", "responsible": "ПМ", "category_id": cat,
                    "category": "Воронка", "stage_id": stage_id, "stage": stage_id, "stage_sort": 10, "semantic": "P", "title": f"З{i}",
                    "amount": 10, "stage_since": "2026-10-01T09:00:00", "snapshot": "2026-10-20T09:00:00"}
        ids = ["C5:NEW", "C5:UC_39RB33", "C5:UC_JMQI3Q", "C5:UC_E8DPVA", "C6:FINAL_INVOICE", "C6:UC_1FIL5J", "C6:UC_S23MPV", "C5:WON"]
        replace_rows(self.con, "crm_deals", [row(i, sid, sid[1]) for i, sid in enumerate(ids)])
        f = deal_funnel(self.con, EXAMPLE)
        self.assertEqual([(s["title"], s["count"]) for s in f["sections"]],
                         [("В работе", 2), ("Преддоговорные сделки", 1), ("Договорные сделки", 2), ("ДЗ", 1), ("ПДЗ", 1)])

    def test_deal_funnel_fact_counts_movement_in_period(self):
        rec = lambda i, created, stage, sem="P", emp="pm": {
            "id": f"deal{i}", "entity": "deal", "emp": emp, "responsible_id": "14", "responsible": "ПМ", "category_id": "0",
            "category": "Общая", "stage_id": stage, "stage": stage, "stage_sort": 1, "semantic": sem, "title": f"Сделка {i}",
            "amount": 100, "created": created, "stage_since": created, "snapshot": "2026-10-20T09:00:00"}
        mv = lambda i, n, stage, ts, sem="P": {"id": f"deal{i}:{n}", "entity": "deal", "rid": f"deal{i}", "category_id": "0",
                                               "category": "Общая", "stage_id": stage, "stage": stage, "stage_sort": 1,
                                               "semantic": sem, "ts": ts}
        replace_rows(self.con, "fact_records", [
            rec(1, "2026-09-05T10:00:00", "WON", "S"),        # создана и выиграна в сентябре
            rec(2, "2026-08-20T10:00:00", "PREPARATION"),     # создана раньше, в сентябре перешла в подготовку
            rec(3, "2026-10-02T10:00:00", "NEW"),             # октябрьская
        ])
        replace_rows(self.con, "stage_moves", [
            mv(1, 1, "NEW", "2026-09-05T10:00:00"), mv(1, 2, "PREPARATION", "2026-09-06T10:00:00"), mv(1, 3, "WON", "2026-09-20T10:00:00", "S"),
            mv(2, 1, "NEW", "2026-08-20T10:00:00"), mv(2, 2, "PREPARATION", "2026-09-10T10:00:00"),
            mv(3, 1, "NEW", "2026-10-02T10:00:00")])
        f = deal_funnel(self.con, {"employees": []}, date_from="2026-09-01", date_to="2026-09-30", mode="fact")
        self.assertEqual(f["mode"], "fact")
        self.assertEqual({s["title"]: s["count"] for s in f["sections"]},
                         {"Создано в периоде": 1, "В работе": 2, "Преддоговорные сделки": 2, "Договорные сделки": 0,
                          "Закрыто успешно": 1, "Закрыто неуспешно": 0})
        self.assertEqual(f["total"], 2)  # октябрьская сделка в сентябре не участвует
        oct_ = deal_funnel(self.con, {"employees": []}, date_from="2026-10-01", date_to="2026-10-31", mode="fact")
        self.assertEqual({s["title"]: s["count"] for s in oct_["sections"]}["Создано в периоде"], 1)
        self.assertEqual(f["other_stages"], [])

    def test_funnel_department_filter(self):
        def row(i, emp, rid):
            return {"id": f"d{i}", "entity": "deal", "emp": emp, "responsible_id": rid, "responsible": f"Сотрудник {i}",
                    "category_id": "0", "category": "Отдел продаж", "stage_id": "PREPARATION", "stage": "Подготовка КП",
                    "semantic": "P", "amount": 1, "created": "2026-10-01T10:00:00", "stage_since": "2026-10-01T10:00:00"}
        replace_rows(self.con, "crm_deals", [row(1, "pm", "14"), row(2, "tender", "15"),
                                             row(3, None, "25"),  # в дашборде без KPI — узнаётся по ID в Б24
                                             row(4, None, "99")])  # нет в разделе «Сотрудники»
        cfg = {**BY_NAMES, "employees": [{"login": "pm", "name": "ПМ", "department": "ОП", "b24_user_id": 14},
                                        {"login": "tender", "name": "Т", "department": "ОП", "b24_user_id": 15},
                                        {"login": "boss", "name": "Р", "department": "Администрация", "b24_user_id": 25}]}
        total = lambda **kw: deal_funnel(self.con, cfg, **kw)["total"]
        self.assertEqual(total(), 4)
        self.assertEqual(total(departments=["ОП"]), 2)
        self.assertEqual(total(departments=["Администрация"]), 1)
        self.assertEqual(total(departments=[""]), 1)  # «Без отдела»
        self.assertEqual(total(departments=["ОП", "Администрация"]), 3)
        f = deal_funnel(self.con, cfg, departments=["ОП"])
        self.assertEqual({d["value"]: d["count"] for d in f["filters"]["departments"]}, {"ОП": 2, "Администрация": 1, "": 1})
        names = {d["value"]: d["name"] for d in f["filters"]["departments"]}
        self.assertEqual((names["ОП"], names[""]), (EXAMPLE_RULES_DEP, "Без отдела"))  # полное название из kpi_rules.json
        self.assertEqual(f["filters"]["departments"][-1]["value"], "")  # «Без отдела» — последним
        self.assertEqual(sorted(r["value"] for r in f["filters"]["responsibles"]), ["pm", "tender"])  # ответственные отдела
        self.assertEqual(total(departments=["ОП"], login="pm"), 1)  # с выбором ответственного — пересечение
        self.assertEqual(f["applied"]["departments"], ["ОП"])

    def test_funnel_period_filters_by_created_or_stage_date(self):
        def row(i, created, since):
            return {"id": f"d{i}", "entity": "deal", "emp": "pm", "responsible_id": "14", "responsible": "ПМ",
                    "category_id": "0", "category": "Отдел продаж", "stage_id": "PREPARATION", "stage": "Подготовка КП",
                    "semantic": "P", "amount": 1, "created": created, "stage_since": since}
        replace_rows(self.con, "crm_deals", [row(1, "2026-09-30T23:59:00", "2026-10-05T10:00:00"),
                                             row(2, "2026-10-01T00:00:00", "2026-10-01T09:00:00"),
                                             row(3, "2026-10-31T18:00:00", "2026-11-02T09:00:00")])
        count = lambda **kw: deal_funnel(self.con, BY_NAMES, **kw)["sections"][0]["count"]
        self.assertEqual(count(), 3)
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-31"), 2)  # границы включительно
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-01"), 1)
        self.assertEqual(count(date_to="2026-09-30"), 1)
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-31", date_field="stage"), 2)
        f = deal_funnel(self.con, BY_NAMES, date_from="2027-01-01")
        self.assertEqual((f["total"], f["filters"]["responsibles"][0]["count"]), (0, 0))  # вариант фильтра остаётся в списке

    # ---------------------------------------------------------------- демо целиком
    def test_demo_overview_runs_for_all_months(self):
        cfg = {**CONFIG, "employees": EXAMPLE["employees"]}
        con = connect(":memory:")
        self.addCleanup(con.close)
        demo.generate(con, cfg)
        engine = Engine(con, self.rules, cfg)
        with mock.patch("app.engine.today", return_value=dt.date.today()):
            cm = f"{dt.date.today():%Y-%m}"
            for k in range(7):
                o = engine.overview(month_add(cm, -k))
                self.assertLessEqual(o["totals"]["payout"], o["totals"]["fund"])
                self.assertEqual(o["totals"]["employees"], 8)


class ProxyTest(unittest.TestCase):
    def handler(self, peer, xff, trust):
        from types import SimpleNamespace
        from app.server import Handler
        h = Handler.__new__(Handler)
        h.client_address = (peer, 50000)
        h.headers = {"X-Forwarded-For": xff} if xff else {}
        h.app = SimpleNamespace(config={"server": {"trust_proxy": trust}})
        return h

    def test_client_ip_behind_proxy(self):
        # адрес, дописанный прокси, — последний; первые клиент может подставить сам
        self.assertEqual(self.handler("127.0.0.1", "6.6.6.6, 203.0.113.5", True).client_ip(), "203.0.113.5")
        self.assertEqual(self.handler("127.0.0.1", None, True).client_ip(), "127.0.0.1")
        # без trust_proxy или не с локального адреса заголовку не верим
        self.assertEqual(self.handler("127.0.0.1", "203.0.113.5", False).client_ip(), "127.0.0.1")
        self.assertEqual(self.handler("198.51.100.7", "203.0.113.5", True).client_ip(), "198.51.100.7")


class MeetingDetectionTest(unittest.TestCase):
    def test_meeting_by_subject(self):
        from app.bitrix24 import is_meeting
        todo = lambda s: {"TYPE_ID": "6", "PROVIDER_ID": "CRM_TODO", "SUBJECT": s}
        for s in ("Встреча с Транснефть", "Рабочая встреча с ВСМПО", "ВКС Химсталькон", "ЗВКС с заказчиком",
                  "Видеоконференция", "Переговоры в офисе", "ООО Ромашка, визит к клиенту", "Телемост с АО X"):
            self.assertTrue(is_meeting(todo(s)), s)
        for s in ("Назначить встречу с ЛПР", "ООО Ромашка — согласовать ВКС", "Перенос встречи", "Позвонить клиенту",
                  "Изумрудный город"):
            self.assertFalse(is_meeting(todo(s)), s)
        self.assertTrue(is_meeting({"TYPE_ID": "1", "SUBJECT": "Без темы"}))
        self.assertFalse(is_meeting({"TYPE_ID": "4", "PROVIDER_ID": "CRM_EMAIL", "SUBJECT": "Встреча"}))

    def test_protocol_from_calendar_event(self):
        from app.bitrix24 import Bitrix24
        b = Bitrix24({"webhook": "https://example.test/rest/1/x/"}, log=lambda *a: None)
        answers = {
            "calendar.event.get": {"result": [
                {"ID": "1869", "PARENT_ID": "1868", "NAME": "Рабочая встреча  с ВСМПО", "DATE_FROM": "15.10.2026 09:00:00"},
                {"ID": "1845", "PARENT_ID": "1844", "NAME": "Встреча с Ромашкой", "DATE_FROM": "12.10.2026 14:30:00"}]},
            "calendar.event.getbyid": lambda p: {"result": {"UF_WEBDAV_CAL_EVENT": [770] if p["id"] == "1868" else []}},
        }
        with mock.patch.object(Bitrix24, "call", lambda self, m, p=None:
                               answers[m](p) if callable(answers[m]) else answers[m]):
            files = b._meeting_calendar_files("2026-10-01")
            self.assertEqual(files(56, "Рабочая встреча с ВСМПО", "2026-10-15"), [770])
            self.assertIsNone(files(56, "Встреча с Ромашкой", "2026-10-12"))
            self.assertIsNone(files(56, "Рабочая встреча с ВСМПО", "2026-10-16"))


if __name__ == "__main__":
    unittest.main()


class SupplierDeferralTest(unittest.TestCase):
    def test_deferral_from_payment_rules(self):
        from app.onec_supply import deferral
        row = lambda kind, pct, days, base="ОтДатыЗаказа": {"ВариантОтсчета": base, "ВариантОплаты": kind,
                                                             "ПроцентПлатежа": pct, "Сдвиг": str(days)}
        # 00ЦБ-000659: 70% «до подтверждения» через 3 дня, 30% «до отгрузки» через 35 дней
        self.assertEqual(deferral([row("АвансДоПодтверждения", 70, 3), row("ПредоплатаДоПоступления", 30, 35)]), (0.3, 35))
        self.assertEqual(deferral([row("КредитСдвиг", 100, 30)]), (1.0, 30))  # «Оплата после отгрузки»
        # 00ЦБ-000617, 00ЦБ-000680: «Оплата независимо от отгрузки» — предоплата, не отсрочка
        self.assertEqual(deferral([row("КредитПослеПоступления", 100, 0)]), (0.0, 0))
        self.assertEqual(deferral([row("КредитПослеПоступления", 70, 0), row("КредитПослеПоступления", 30, 20)]), (0.0, 0))
        self.assertEqual(deferral([row("КредитСдвиг", 100, 30, base="ОтДатыСогласования")]), (0.0, 0))
        self.assertEqual(deferral([]), (None, None))
