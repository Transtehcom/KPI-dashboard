"""Тесты вкладки «Поставщики»: закупки = поступления минус возвраты, цены без НДС, альтернативы."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import onec_suppliers as sup  # noqa: E402
from app.common import connect  # noqa: E402

PARTNERS = {"pa": {"name": "Поставщик А", "inn": "111"}, "pb": {"name": "Поставщик Б", "inn": "222"}}
ITEMS = {"rope": {"name": "Трос 6 мм", "unit": "м"}, "clamp": {"name": "Зажим", "unit": "шт"}}
ZERO = "00000000-0000-0000-0000-000000000000"


def doc(ref, number, date, partner, rows, vat_in=False):
    return {"Ref_Key": ref, "Number": number, "Date": date, "Партнер_Key": partner, "ЦенаВключаетНДС": vat_in,
            "Товары": [{"LineNumber": str(i + 1), "Номенклатура_Key": it, "Характеристика_Key": ZERO, "Количество": q, "Сумма": s, "СуммаНДС": nds}
                       for i, (it, q, s, nds) in enumerate(rows)]}


class SuppliersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.dir.name, "t.sqlite"))
        lines = sup.lines_from_docs([
            doc("d1", "П-1", "2026-07-10T10:00:00", "pa", [("rope", 100, 4650, 837), ("clamp", 10, 320, 0)]),
            doc("d2", "П-2", "2026-09-18T10:00:00", "pa", [("rope", 100, 4980, 0)]),
            doc("d3", "П-3", "2026-09-18T10:00:00", "pb", [("rope", 50, 2395, 0)]),
        ], "in", PARTNERS, ITEMS) + sup.lines_from_docs([
            doc("r1", "В-1", "2026-09-20T10:00:00", "pa", [("rope", 20, 996, 0)])], "ret", PARTNERS, ITEMS)
        sup.store(self.con, lines, dt.datetime(2026, 10, 1, 9, 0))

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_net_purchases_with_returns_and_prices(self):
        d = sup.build(self.con, "pa", "2026-07-01", "2026-09-30")
        rope = next(i for i in d["items"] if i["key"] == "rope")
        self.assertEqual((rope["qty"], rope["amount"]), (180, 4650 + 4980 - 996))
        self.assertEqual((rope["first"], rope["last"], rope["avg"]), (46.5, 49.8, round(8634 / 180, 2)))
        self.assertAlmostEqual(rope["change"], 49.8 / 46.5 - 1, places=3)
        self.assertEqual(d["tiles"]["amount"], 8634 + 320)
        self.assertEqual((d["tiles"]["items"], d["tiles"]["growth"]), (2, 1))

    def test_vat_included_price_is_cut(self):
        l = sup.lines_from_docs([doc("d9", "П-9", "2026-09-01T00:00:00", "pa", [("rope", 10, 120, 20)], vat_in=True)], "in", PARTNERS, ITEMS)
        self.assertEqual(l[0]["amount"], 100)

    def test_alternatives_and_no_alternative(self):
        d = sup.build(self.con, "pa", "2026-07-01", "2026-09-30", "rope")
        self.assertEqual([(a["name"], a["last"]) for a in d["alternatives"]], [("Поставщик Б", 47.9)])
        self.assertEqual([m["month"] for m in d["dynamics"]], ["2026-07", "2026-09"])
        self.assertEqual(d["tiles"]["no_alt"], 1)  # зажим: других поставщиков нет

    def test_empty_period_is_none_not_zero(self):
        d = sup.build(self.con, "pa", "2026-01-01", "2026-02-01")
        self.assertEqual((d["tiles"]["amount"], d["tiles"]["items"], d["items"]), (None, None, []))

    def test_find_partner_by_inn_and_list(self):
        self.assertEqual(sup.find_partner(self.con, inn="222"), "pb")
        self.assertIsNone(sup.find_partner(self.con, inn="999"))
        self.assertEqual([s["name"] for s in sup.suppliers(self.con, "2026-07-01", "2026-09-30")], ["Поставщик А", "Поставщик Б"])

    def test_stale_and_error_keeps_data(self):
        self.assertFalse(sup.is_stale(self.con, dt.datetime(2026, 10, 1, 9, 30)))
        self.assertTrue(sup.is_stale(self.con, dt.datetime(2026, 10, 1, 11, 0)))
        sup.store_error(self.con, "нет связи")
        self.assertEqual(sup.meta(self.con), {"synced_at": "2026-10-01T09:00:00", "error": "нет связи"})
        self.assertTrue(sup.suppliers(self.con, "2026-07-01", "2026-09-30"))


if __name__ == "__main__":
    unittest.main()
