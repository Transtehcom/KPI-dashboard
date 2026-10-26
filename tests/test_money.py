"""Тесты вкладки «Деньги» (расчёты из «вкладка _Деньги_.xlsx»)."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import money  # noqa: E402
from app.common import connect  # noqa: E402

ASOF = dt.date(2026, 10, 23)


class MoneyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))
        money.save_settings(self.con, {"asof": "2026-10-23", "start": "8000000"})

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def item(self, kind, **kw):
        money.save_item(self.con, {"kind": kind, "party": "X", "amount": 1000, **kw}, "t")

    def test_calendar_balance_and_shortfall(self):
        self.item("in", amount=6000000, owed=6000000, contract="2026-10-16", plan="2026-10-28", type="Отгрузка")
        self.item("out", amount=9000000, plan="2026-10-26", approval="Согласовано")
        self.item("out", amount=5000000, plan="2026-10-26", approval="Не согласовано")  # не в календаре
        d = money.build(self.con)
        self.assertEqual([c["end"] for c in d["calendar"]][:4], [8000000, 8000000, 8000000, -1000000])
        self.assertEqual(d["calendar"][3]["out"], 9000000)
        self.assertEqual(d["tiles"]["low"], -1000000)  # 8 млн − 9 млн, поступление придёт только 28.10
        self.assertEqual((d["tiles"]["shortfall"], d["tiles"]["low_date"]), (1000000, "2026-10-26"))
        self.assertEqual(d["calendar"][-1]["end"], 5000000)

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
        self.assertEqual((rows["a"]["status"], rows["a"]["plan"]), ("Не обещано", None))  # срок прошёл, дата не обещана
        self.assertEqual((rows["b"]["plan"], d["owe_late"]), ("2026-10-25", 700))
        money.save_expect(self.con, "a", "2026-10-24")
        a = {o["id"]: o for o in money.build(self.con)["owe"]}["a"]
        self.assertEqual((a["status"], a["plan"]), ("Обещание", "2026-10-24"))

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.item("in", plan="не дата")
        with self.assertRaises(ValueError):
            self.item("out", approval="Может быть")


if __name__ == "__main__":
    unittest.main()
