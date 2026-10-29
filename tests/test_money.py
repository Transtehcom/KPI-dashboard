"""Тесты вкладки «Деньги»: расчёты по «вкладка _Деньги_.xlsx» и загрузка из 1С по «Краткому ТЗ»."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import money, onec_money  # noqa: E402
from app.common import connect  # noqa: E402


class MoneyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))
        money.save_settings(self.con, {"asof": "2026-10-23", "adjust": "0"})
        self.cash(8000000)

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def cash(self, amount, currency="руб.", account="Р/с"):
        self.con.execute("INSERT INTO money_cash VALUES (?,?,?,?)", (account, currency, amount, "2026-10-22T18:00"))

    def item(self, kind, **kw):
        money.save_item(self.con, {"kind": kind, "party": "X", "amount": 1000, **kw}, "t")

    def plan(self, id, **kw):
        row = {"id": id, "kind": "out", "source": "request", "doc": "З-1", "doc_date": "2026-10-20", "party": "Завод", "basis": "З-1",
               "purpose": "Оплата поставщику", "detail": "", "amount": 1000, "currency": "руб.", "contract": "2026-10-26",
               "plan": "2026-10-26", "approval": "Согласовано", "priority": "Средний", **kw}
        self.con.execute(f"INSERT INTO money_plan ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))

    def test_start_balance_comes_from_1c_minus_unavailable(self):
        money.save_settings(self.con, {"asof": "2026-10-23", "adjust": "500000.5"})
        t = money.build(self.con)["tiles"]
        self.assertEqual(t["start"], 7499999.5)
        # 0,01 ₽ к остатку увеличивает каждый дневной остаток на 0,01 ₽
        self.con.execute("UPDATE money_cash SET amount = amount + 0.01")
        end = [c["end"] for c in money.build(self.con)["calendar"]]
        self.assertTrue(all(abs(e - 7499999.51) < 0.001 for e in end))

    def test_calendar_balance_and_shortfall(self):
        self.item("in", amount=6000000, owed=6000000, contract="2026-10-16", plan="2026-10-28", type="Отгрузка")
        self.plan("a", amount=9000000, plan="2026-10-26")
        self.plan("b", amount=5000000, plan="2026-10-26", approval="Не согласовано")  # не в календаре
        d = money.build(self.con)
        self.assertEqual([c["end"] for c in d["calendar"]][:4], [8000000, 8000000, 8000000, -1000000])
        self.assertEqual((d["tiles"]["low"], d["tiles"]["shortfall"], d["tiles"]["low_date"]), (-1000000, 1000000, "2026-10-26"))
        self.assertEqual(d["calendar"][-1]["end"], 5000000)
        self.assertEqual((len(d["pay"]), d["pay_other_total"]), (1, 5000000))  # несогласованные — отдельно, с суммой

    def test_undated_and_unapproved_payments_listed_separately(self):
        self.plan("a", plan=None)
        self.plan("b", approval="Не согласовано")
        d = money.build(self.con)
        self.assertEqual((len(d["pay"]), len(d["pay_other"]), d["pay_other_total"]), (0, 2, 2000))
        self.assertEqual(d["tiles"]["out"], 0)

    def test_moving_planned_date_moves_payment_between_days(self):
        self.plan("a", amount=300, plan="2026-10-26")
        self.assertEqual(money.build(self.con)["calendar"][3]["out"], 300)
        money.save_override(self.con, "a", "2026-10-28", None)
        d = money.build(self.con)
        self.assertEqual((d["calendar"][3]["out"], d["calendar"][5]["out"]), (0, 300))
        self.assertEqual(d["pay"][0]["contract"], "2026-10-26")  # договорная дата не меняется
        money.save_override(self.con, "a", "", "Не согласовано")
        self.assertEqual(money.build(self.con)["tiles"]["out"], 0)

    def test_payments_beyond_horizon_kept_in_detail(self):
        self.plan("a", amount=700, plan="2026-11-20", contract="2026-11-20")
        d = money.build(self.con)
        self.assertEqual((d["tiles"]["out"], d["pay_beyond"], len(d["pay"])), (0, 700, 1))

    def test_other_currencies_are_not_mixed_into_rubles(self):
        self.cash(1000, currency="USD")
        self.plan("a", amount=50, currency="USD")
        d = money.build(self.con)
        self.assertEqual(d["tiles"]["start"], 8000000)
        self.assertEqual(d["foreign"], [{"currency": "USD", "cash": 1000, "in": 0.0, "out": 50}])

    def test_overdue_from_contract_date_and_status(self):
        self.item("in", amount=100, owed=100, contract="2026-10-16", plan="2026-10-28")
        self.item("in", amount=50, owed=0, contract="2026-10-23", plan="2026-10-23", type="Аванс")
        owe = {o["amount"]: o for o in money.build(self.con)["owe"]}
        self.assertEqual((owe[100]["late_days"], owe[100]["status"]), (7, "Обещание"))
        self.assertEqual((owe[50]["late_days"], owe[50]["status"]), (0, "По договору"))

    def test_1c_receivables_and_promised_date(self):
        self.con.execute("INSERT INTO receivables (id, emp, doc, client, due_date, paid_date, amount) VALUES "
                         "('a','e','З-1','Клиент А','2026-10-20',NULL,700), ('b','e','З-2','Клиент Б','2026-10-25',NULL,300), "
                         "('c','e','З-3','Клиент В','2026-10-25','2026-10-24',900)")
        d = money.build(self.con)
        rows = {o["id"]: o for o in d["owe"]}
        self.assertEqual(set(rows), {"a", "b"})  # оплаченный этап не показывается
        self.assertEqual((rows["a"]["status"], rows["a"]["plan"]), ("По договору", "2026-10-20"))  # просроченная дата платежа тоже показывается
        self.assertEqual((rows["b"]["plan"], d["owe_late"]), ("2026-10-25", 700))
        money.save_expect(self.con, "a", "2026-10-24")
        a = {o["id"]: o for o in money.build(self.con)["owe"]}["a"]
        self.assertEqual((a["status"], a["plan"]), ("Обещание", "2026-10-24"))

    def test_customer_advances_are_shown_separately(self):
        self.plan("cp:1", kind="in", source="customer_plan", amount=2000000, purpose="Аванс", contract="2026-10-27", plan="2026-10-27",
                  approval=None)
        d = money.build(self.con)
        self.assertEqual((d["owe_advance"], d["owe_total"], d["calendar"][4]["in"]), (2000000, 0, 2000000))

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.item("in", plan="не дата")
        with self.assertRaises(ValueError):
            self.item("out", approval="Может быть")


class OneCMoneyTest(unittest.TestCase):
    def test_cash_balance_by_account_and_currency(self):
        rec = lambda t, s, k, p: {"RecordType": t, "Сумма": s, "БанковскийСчет_Key": k, "Period": p}
        res = onec_money.cash_balances(
            [rec("Receipt", 100, "a", "2026-10-01T10:00:00"), rec("Expense", 30.5, "a", "2026-10-02T11:00:00"),
             rec("Receipt", 5, "b", "2026-10-01T10:00:00"), rec("Receipt", 7, "c", "2026-10-01T10:00:00"), rec("Expense", 7, "c", "2026-10-03T10:00:00")],
            {"a": {"name": "Р/с 1", "currency_key": "r"}, "b": {"name": "Валютный", "currency_key": "u"}}, {"r": "руб.", "u": "USD"})
        self.assertEqual(res, [{"account": "Р/с 1", "currency": "руб.", "amount": 69.5, "last": "2026-10-02T11:00"},
                               {"account": "Валютный", "currency": "USD", "amount": 5, "last": "2026-10-01T10:00"}])  # нулевой остаток не показывается

    def test_open_plan_nets_receipts_and_expenses(self):
        def r(t, s, d, v="КредитПослеПоступления"):
            return {"RecordType": t, "КОплате": s, "ОбъектРасчетов_Key": "o", "ДокументПлан": "p", "ДатаПлановогоПогашения": d + "T00:00:00",
                    "ВариантОплаты": v, "Валюта_Key": "r"}
        open_ = onec_money.open_plan([r("Receipt", 100, "2026-10-30"), r("Expense", 40, "2026-10-30"),  # остаток 60
                                      r("Receipt", 50, "2026-11-05"), r("Expense", 50, "2026-11-05")])  # закрыт
        self.assertEqual([(x["date"], x["amount"]) for x in open_], [("2026-10-30", 60)])

    def test_client_debt_sums_positive_balances_of_orders(self):
        st = "ТТК_ВЗакуп"
        orders = [{"ОбъектРасчетов_Key": k, "Статус": st} for k in "abcd"] + [
            {"ОбъектРасчетов_Key": "e", "Статус": "НеСогласован"}, {"ОбъектРасчетов_Key": "f", "Статус": "Закрыт"}]  # другие статусы не считаются
        balance = [{"ОбъектРасчетов_Key": "a", "СуммаBalance": 17445.59}, {"ОбъектРасчетов_Key": "b", "СуммаBalance": 0},
                   {"ОбъектРасчетов_Key": "c", "СуммаBalance": -500}, {"ОбъектРасчетов_Key": "x", "СуммаBalance": 999},  # не заказ клиента
                   {"ОбъектРасчетов_Key": "d", "СуммаBalance": 100}, {"ОбъектРасчетов_Key": "d", "СуммаBalance": 50.01},
                   {"ОбъектРасчетов_Key": "e", "СуммаBalance": 4199536.76}, {"ОбъектРасчетов_Key": "f", "СуммаBalance": 701362.61}]  # разные валюты/аналитики
        self.assertEqual(onec_money.client_debt(orders, balance), {"amount": 17595.6, "orders": 2})

    def test_request_rows(self):
        req = {"Ref_Key": "k", "Number": "00ЦБ-1 ", "Date": "2026-10-20T10:00:00", "Статус": "КОплате", "ХозяйственнаяОперация": "ПеречислениеВБюджет",
               "СуммаДокумента": 572335.83, "ЖелательнаяДатаПлатежа": "2026-10-27T00:00:00", "ДатаПлатежа": "2026-10-28T00:00:00",
               "ПриоритетОплаты_Key": "p", "Валюта_Key": "r", "НазначениеПлатежа": "НДС"}
        row = onec_money.request_row(req, None, {"p": "Высокий"}, {"r": "руб."}, "2026-10-26")
        self.assertEqual((row["approval"], row["plan"], row["contract"], row["priority"], row["purpose"], row["party"]),
                         ("Согласовано", "2026-10-28", "2026-10-27", "Высокий", "Налоги и взносы", "Налоги и взносы"))
        self.assertEqual(onec_money.request_row({**req, "Статус": "НеСогласована"}, "Завод", {}, {}, "2026-10-26")["approval"], "Не согласовано")
        self.assertIsNone(onec_money.request_row({**req, "ДатаПлатежа": "2026-10-20T00:00:00"}, None, {}, {}, "2026-10-26"))  # дата раньше среза
        self.assertIsNone(onec_money.request_row({**req, "ХозяйственнаяОперация": "ПеречислениеНаДругойСчет"}, None, {}, {}, "2026-10-26"))

    def test_error_keeps_previous_amounts_and_shows_reason(self):
        with tempfile.TemporaryDirectory() as d:
            con = connect(os.path.join(d, "t.sqlite"))
            onec_money.store(con, {"cash": [{"account": "Р/с", "currency": "руб.", "amount": 123.45, "last": "2026-10-20T10:00"}], "plan": []},
                             now=dt.datetime(2026, 10, 26, 12, 0))
            onec_money.store_error(con, "1С недоступна")
            out = money.build(con)
            self.assertEqual((out["tiles"]["start"], out["sync"]["at"], out["sync"]["error"]),
                             (123.45, "2026-10-26T12:00:00", "1С недоступна"))
            con.close()


if __name__ == "__main__":
    unittest.main()
