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
EXAMPLE = load_json("config.example.json")
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
        r = self.calc("lead", "base_development", "2026-09")
        comps = {c["name"]: c["fact"] for c in r["components"]}
        self.assertEqual(comps["Результативные касания"], 1)  # только звонок с ЛПР и следующим шагом
        calls = next(c["fact"] for c in r["components"] if c["name"].startswith("Звонки от 2 мин"))
        self.assertEqual(calls, 2)  # ЛПР и следующий шаг для звонков не нужны

    def test_calls_one_per_contact_per_day(self):
        insert(self.con, "activities", [call(1, "tender", "2026-09-10", contact="CONTACT:7"),
                                        call(2, "tender", "2026-09-10", contact="CONTACT:7"),
                                        call(3, "tender", "2026-09-11", contact="CONTACT:7"),
                                        call(4, "tender", "2026-09-10", contact="CONTACT:8")])
        r = self.calc("tender", "base_development", "2026-09")
        self.assertEqual(r["components"][0]["fact"], 3)  # повторный звонок тому же контакту в тот же день не в счёт

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
        self.assertEqual(self.item("supply", "competitive_purchases", "2026-09")["status"], "awaiting")
        for f, v in (("deferred_share", "30"), ("deferral_days", "14"), ("relevant", "1"), ("evidence", "1")):
            manual.set_value(self.con, "supplier_orders", "o1", f, v, "boss")
        self.assertEqual(self.calc("supply", "deferred_payment", "2026-09")["components"][0]["fact"], 1.0)
        self.assertEqual(self.calc("supply", "competitive_purchases", "2026-09")["components"][0]["fact"], 1.0)

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
    def test_lot_spanning_months_counted_once_in_first_month(self):
        insert(self.con, "supplier_orders", [order(1, "2026-08-28", 300000, lot="L1", kp_count=1),
                                             order(2, "2026-09-02", 300000, lot="L1", kp_count=3)])
        aug = self.calc("supply", "competitive_purchases", "2026-08")
        sep = self.calc("supply", "competitive_purchases", "2026-09")
        self.assertEqual(aug["components"][0]["fact"], 1.0)  # 3 КП в лоте, сумма 600 000
        self.assertIsNone(sep["components"][0]["fact"])

    def test_justification_does_not_cover_affiliation(self):
        insert(self.con, "supplier_orders", [order(1, "2026-09-02", 600000, kp_count=1, justified=1),
                                             order(2, "2026-09-03", 600000, affiliated=1, justified=1)])
        r = self.calc("supply", "competitive_purchases", "2026-09")
        self.assertEqual(r["components"][0]["fact"], 0.5)
        self.assertIn("аффилированность", r["details"]["rows"][1][4])

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

    def test_company_need_and_strategy_text_fields(self):
        from app.bitrix24 import Bitrix24
        yes = ["Да", " да ", "ДА.", "да, проект модернизации", "Y", "1", True]
        no = ["Нет", "нет", "не выявлена", "", None, "N", "-", "пока нет"]
        self.assertEqual([Bitrix24.yes(v) for v in yes], [1] * len(yes))
        self.assertEqual([Bitrix24.yes(v) for v in no], [0] * len(no))
        self.assertEqual([Bitrix24.filled(v) for v in ["Ежемесячные КП по БПЛА", "звонок раз в неделю"]], [1, 1])
        self.assertEqual([Bitrix24.filled(v) for v in ["", None, "-", " — ", "нет", "Нет."]], [0] * 6)

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

    def test_rejected_tenders_norm_zero(self):
        self.assertEqual(self.item("tender", "tender_operations", "2026-09")["status"], "met")
        insert(self.con, "tenders", [{"id": "t1", "emp": "tender", "title": "Т", "result": "lost", "result_date": "2026-09-10",
                                      "rejected": 1}])
        it = self.item("tender", "tender_operations", "2026-09")
        self.assertEqual((it["components"][0]["fact"], it["status"]), (1, "fail"))

    def test_team_revenue_plan_split_between_sales_and_tender_staff(self):
        # 150 млн на месяц делятся на pm, lead, tender из CONFIG; свой план руководителя важнее
        for login in ("pm", "lead", "tender"):
            self.assertEqual(self.calc(login, "gross_profit", "2026-09")["plan"]["value"], 50_000_000)
        self.con.execute("INSERT INTO plans VALUES ('tender', '2026-09', 'revenue', 7000000)")
        self.assertEqual(self.calc("tender", "gross_profit", "2026-09")["plan"]["value"], 7_000_000)

    def test_pm_overdue_over_14_days_zeroes_all_kpi(self):
        insert(self.con, "receivables", [{"id": "r1", "emp": "pm", "doc": "УПД", "due_date": "2026-09-01",
                                          "paid_date": "2026-09-20", "amount": 1}])
        self.assertTrue(self.engine.compute("pm", "2026-09")["zero_all"])

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
        f = deal_funnel(self.con, EXAMPLE)
        self.assertEqual([(s["title"], s["count"]) for s in f["sections"]],
                         [("В работе", 2), ("Преддоговорные сделки", 1), ("Договорные сделки", 1), ("ПДЗ", 1)])
        self.assertEqual(f["total"], 5)  # закрытая сделка и лиды в воронку сделок не входят
        self.assertEqual({o["stage"] for o in f["other_stages"]}, {"Выявление потребности", "Подготовка КП"})
        self.assertEqual(f["sections"][0]["share_of_total"], 0.4)
        other = deal_funnel(self.con, EXAMPLE, login="id:25")
        self.assertEqual([s["count"] for s in other["sections"]], [1, 0, 0, 0])
        self.assertEqual(len(other["filters"]["responsibles"]), 2)  # фильтр не сужает список вариантов
        both = deal_funnel(self.con, EXAMPLE, login=["id:25", "pm"])  # несколько ответственных складываются
        self.assertEqual([s["count"] for s in both["sections"]], [s["count"] for s in f["sections"]])
        self.assertEqual(both["applied"]["logins"], ["id:25", "pm"])
        leads = deal_funnel(self.con, EXAMPLE, kind="leads")
        self.assertEqual([(s["title"], s["count"]) for s in leads["sections"]],
                         [("Поступило", 3), ("Не обработано", 1), ("В работе", 2), ("Запросили ТКП", 0)])
        self.assertEqual(leads["sections"][0]["share_of_total"], 1.0)  # «Поступило» — сумма трёх этапов ниже
        self.assertEqual(leads["total_amount"], 300)
        self.assertEqual(leads["other_stages"], [{"category": "Воронка lead", "stage": "Новому менеджеру", "count": 1}])

    def test_funnel_period_filters_by_created_or_stage_date(self):
        def row(i, created, since):
            return {"id": f"d{i}", "entity": "deal", "emp": "pm", "responsible_id": "14", "responsible": "ПМ",
                    "category_id": "0", "category": "Отдел продаж", "stage_id": "PREPARATION", "stage": "Подготовка КП",
                    "semantic": "P", "amount": 1, "created": created, "stage_since": since}
        replace_rows(self.con, "crm_deals", [row(1, "2026-09-30T23:59:00", "2026-10-05T10:00:00"),
                                             row(2, "2026-10-01T00:00:00", "2026-10-01T09:00:00"),
                                             row(3, "2026-10-31T18:00:00", "2026-11-02T09:00:00")])
        count = lambda **kw: deal_funnel(self.con, EXAMPLE, **kw)["sections"][0]["count"]
        self.assertEqual(count(), 3)
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-31"), 2)  # границы включительно
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-01"), 1)
        self.assertEqual(count(date_to="2026-09-30"), 1)
        self.assertEqual(count(date_from="2026-10-01", date_to="2026-10-31", date_field="stage"), 2)
        f = deal_funnel(self.con, EXAMPLE, date_from="2027-01-01")
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


if __name__ == "__main__":
    unittest.main()
