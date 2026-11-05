"""Customer deliveries must not be multiplied by linked supplier orders."""
import datetime as dt
import unittest
from unittest.mock import patch

from app import manual
from app.common import connect, load_rules
from app.engine import Engine
from app.onec_supply import fetch_supplier_orders
from app.onec import OneC
from app.supply_delivery import delivery_deviation
from test_engine import CONFIG, insert, order


class SupplyDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.engine = Engine(self.con, load_rules(), CONFIG)

    def add(self, i, key="customer-148", **kw):
        insert(self.con, "supplier_orders", [order(i, "2026-09-29", 100,
            customer_order="00ЦБ-000148", customer_order_key=key, customer_order_date="2026-09-23",
            planned_date="2026-11-01", customer_actual_date=kw.pop("fact", "2026-11-02"),
            actual_date="2026-10-23", complete=1, **kw)])

    def calc(self):
        return self.engine.calc_on_time_delivery({"login": "supply"},
            {"first": dt.date(2026, 11, 1), "last": dt.date(2026, 11, 30),
             "asof": dt.date(2026, 11, 3)}, {"late_share_max": .2})

    def test_one_customer_counts_once_and_uses_customer_actual_date(self):
        self.add(675)
        self.add(676)
        result = self.calc()
        self.assertEqual(result["extra"][0]["value"], 1)
        self.assertEqual(result["components"][0]["fact"], 1)
        self.assertEqual(result["details"]["rows"][0][0], "00ЦБ-000148")
        self.assertIn("опоздание 1 дн.", result["details"]["rows"][0][-1])

    def test_missing_customer_date_does_not_fall_back_to_receipt(self):
        self.add(675, fact=None)
        self.assertEqual(self.calc()["extra"][1]["value"], 0)

    def test_undelivered_order_becomes_overdue_only_after_its_deadline(self):
        self.add(675, fact=None)
        self.con.execute("UPDATE supplier_orders SET planned_date='2026-11-10' WHERE id='o675'")
        for day, counted, deviation in ((7,0,None), (10,0,None), (11,1,-1)):
            current = dt.date(2026,11,day)
            ctx = {"first":dt.date(2026,11,1), "last":dt.date(2026,11,30), "asof":current}
            with self.subTest(day=day), patch("app.engine.today", return_value=current):
                result = self.engine.calc_on_time_delivery({"login":"supply"},ctx,{"late_share_max":.2})
                self.assertEqual(result["extra"][0]["value"],1)
                self.assertEqual(len(result["details"]["rows"]),counted)
                self.assertEqual(result["components"][0]["fact"],counted)
                self.assertEqual(result["extra"][3]["value"],1-counted)
                self.assertEqual(delivery_deviation({"planned_date":"2026-11-10","customer_actual_date":None},current),deviation)

    def test_monthly_percentage_includes_future_deadlines_without_marking_them_late(self):
        for i, day in enumerate((1,2,9,11,19), 1):
            self.add(i, key=f"customer-{i}", fact=None)
            self.con.execute("UPDATE supplier_orders SET planned_date=? WHERE id=?", (f"2026-11-{day:02d}",f"o{i}"))
        with patch("app.engine.today", return_value=dt.date(2026,11,5)):
            result = self.calc()
        self.assertEqual(result["components"][0]["fact"], .4)
        self.assertEqual([x["value"] for x in result["extra"]], [5,0,2,3])
        self.assertEqual(len(result["details"]["rows"]),2)

    def test_delivery_payout_is_zero_when_every_order_has_a_violation(self):
        for i in range(1,6):
            self.add(i, key=f"customer-{i}", fact="2026-11-01")
        for late, expected_pct, expected_pay in ((5,0,0), (2,.75,2250), (1,1,3000), (0,1,3000)):
            self.con.execute("UPDATE supplier_orders SET customer_actual_date='2026-11-01'")
            for i in range(1,late+1):
                self.con.execute("UPDATE supplier_orders SET customer_actual_date='2026-11-02' WHERE id=?", (f"o{i}",))
            with self.subTest(late=late), patch("app.engine.today", return_value=dt.date(2026,11,3)):
                item = next(i for i in self.engine.compute("supply","2026-11")["items"] if i["code"] == "on_time_delivery")
                self.assertAlmostEqual(item["pct"], expected_pct)
                self.assertEqual(item["payout"], expected_pay)

    def test_equal_date_is_on_time_and_quality_failures_remain(self):
        self.add(675, fact="2026-11-01")
        self.add(676, fact="2026-11-01")
        self.assertEqual(self.calc()["extra"][1]["value"], 1)
        self.con.execute("UPDATE supplier_orders SET defect=1 WHERE id='o676'")
        self.assertEqual(self.calc()["extra"][1]["value"], 0)

    def test_same_number_different_customer_keys_not_merged(self):
        self.add(675)
        self.add(676, key="customer-other-year")
        self.assertEqual(self.calc()["extra"][0]["value"], 2)

    def test_entry_groups_children_with_original_manual_refs(self):
        self.add(675)
        self.add(676)
        en = manual.entry(self.con, "on_time_delivery", "supply", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        self.assertEqual([f["key"] for f in en["fields"]][:2], ["planned_date", "customer_actual_date"])
        self.assertTrue(en["fields"][1]["parent_edit"])
        self.assertNotIn("mis_sort", [f["key"] for f in en["fields"]])
        self.assertEqual(len(en["groups"]), 1)
        self.assertEqual(en["groups"][0]["cells"][0], "00ЦБ-000148")
        self.assertEqual([r["ref"] for r in en["groups"][0]["rows"]], ["o675", "o676"])
        manual.set_value(self.con, "supplier_orders", "o676", "defect", "1", "boss")
        self.assertEqual(self.con.execute("SELECT defect FROM supplier_orders WHERE id='o676'").fetchone()[0], 1)

    def test_entry_month_uses_customer_deadline_and_keeps_all_linked_suppliers(self):
        self.add(675)
        self.add(676)
        self.add(677, key="october")
        self.add(678, key="undated")
        self.con.execute("UPDATE supplier_orders SET dt='2026-11-03',planned_date='2026-10-31' WHERE id='o677'")
        self.con.execute("UPDATE supplier_orders SET dt='2026-11-03',planned_date=NULL WHERE id='o678'")
        self.con.execute("UPDATE supplier_orders SET planned_date='2026-11-30' WHERE id IN ('o675','o676')")
        entry = manual.entry(self.con, "on_time_delivery", "supply", dt.date(2026,11,1), dt.date(2026,11,30))
        self.assertEqual([g["ref"] for g in entry["groups"]], ["customer-148"])
        self.assertEqual([r["ref"] for r in entry["groups"][0]["rows"]], ["o675", "o676"])
        self.assertTrue(entry["fields"][0]["readonly"])
        self.assertEqual([f["key"] for f in entry["fields"]][:3],
                         ["planned_date", "customer_actual_date", "delivery_delta"])
        with self.assertRaises(ValueError):
            manual.set_value(self.con, "supplier_orders", "o675", "planned_date", "2026-10-31", "boss")

    def test_signed_delivery_deviation_and_unfinished_orders_use_period_cutoff(self):
        group = {"planned_date": "2026-11-03", "customer_actual_date": None}
        cutoff = dt.date(2026,11,5)
        for actual, expected in (("2026-11-01",2), ("2026-11-03",0), ("2026-11-05",-2),
                                 (None,-2), ("2026-11-10",-7)):
            with self.subTest(actual=actual):
                self.assertEqual(delivery_deviation({**group,"customer_actual_date":actual},cutoff),expected)
        self.assertIsNone(delivery_deviation({**group,"planned_date":"2026-11-06"},cutoff))
        self.assertIsNone(delivery_deviation({**group,"planned_date":None},cutoff))

    def test_manual_actual_survives_sync_and_reset_uses_latest_source(self):
        self.add(675)
        self.add(676)
        manual.set_value(self.con, "supplier_orders", "o675", "customer_actual_date", "2026-11-01", "boss")
        self.assertEqual(self.calc()["extra"][1]["value"], 1)
        # Source dates must stay intact; replacing source rows simulates the normal sync.
        self.assertEqual(self.con.execute("SELECT customer_actual_date FROM supplier_orders WHERE id='o675'").fetchone()[0], "2026-11-02")
        self.add(675, fact="2026-11-03")
        self.add(676, fact="2026-11-03")
        self.add(720, fact="2026-11-03")
        manual.apply(self.con)
        self.assertEqual(self.calc()["extra"][1]["value"], 1)
        en = manual.entry(self.con, "on_time_delivery", "supply", dt.date(2026,11,1), dt.date(2026,11,30))
        group = en["groups"][0]
        self.assertEqual(group["values"]["customer_actual_date"], "2026-11-01")
        self.assertEqual(group["source_values"]["customer_actual_date"], "2026-11-03")
        self.assertIn("customer_actual_date", group["manual"])
        self.assertEqual({r["values"]["customer_actual_date"] for r in group["rows"]}, {"2026-11-01"})
        # Any linked supplier ref addresses the same customer-level override.
        manual.set_value(self.con, "supplier_orders", "o720", "customer_actual_date", "", "boss")
        self.assertEqual(self.calc()["extra"][1]["value"], 0)
        self.assertEqual(self.calc()["details"]["rows"][0][3], "2026-11-03")

    def test_manual_actual_isolated_by_customer_key_and_validates_dates(self):
        self.add(675)
        self.add(676, key="other-customer")
        manual.set_value(self.con, "supplier_orders", "o675", "customer_actual_date", "2026-11-01", "boss")
        self.assertEqual(self.calc()["extra"][1]["value"], 1)
        with self.assertRaises(ValueError):
            manual.set_value(self.con, "supplier_orders", "o675", "customer_actual_date", "0202-11-02", "boss")
        self.assertEqual(self.calc()["extra"][1]["value"], 1)

    def test_defect_correction_deadline_and_removed_mis_sort(self):
        self.add(675, fact="2026-11-01", defect=1, mis_sort=1)
        self.add(676, fact="2026-11-01")
        self.assertEqual(self.calc()["extra"][1]["value"], 0)
        for fixed, good in (("2026-10-31",1),("2026-11-01",1),("2026-11-02",0)):
            with self.subTest(fixed=fixed):
                manual.set_value(self.con,"supplier_orders","o675","defect_fixed_date",fixed,"boss")
                self.assertEqual(self.calc()["extra"][1]["value"],good)
        manual.set_value(self.con,"supplier_orders","o675","defect","0","boss")
        self.assertEqual(self.calc()["extra"][1]["value"],1, "removed mis-sort must not penalize KPI")

    def test_author_owner_override_uses_creator_not_manager(self):
        class Source:
            def _odata(self,entity,params):
                if entity == 'Document_ЗаказПоставщику':
                    return [{'Ref_Key':str(i),'Number':str(i),'Date':'2026-11-01',
                             'Автор_Key':author,'Менеджер_Key':'different-manager'}
                            for i,author in enumerate(('creator-a','creator-b'))]
                return []
        rows=fetch_supplier_orders(Source(),{'author_owners':{'creator-a':'Шляков Юрий Михайлович'}},dt.date(2026,11,1),dt.date(2026,11,3))
        self.assertEqual(rows[0]['manager'],'Шляков Юрий Михайлович')
        self.assertIsNone(rows[1]['manager'])
        self.assertEqual(rows[0].get('author_key'),'creator-a')

    def test_sync_refreshes_old_copies_when_source_date_is_cleared(self):
        self.add(675)
        self.con.execute("UPDATE supplier_orders SET id='1c-supplier_orders-old',dt='2025-09-29'")
        class Source(OneC):
            def _load(self,ds,date_from,date_to):
                self.loaded_from=date_from
                return [{'id':'old','manager':'source manager','doc':'old','dt':'2025-09-29',
                         'customer_order_key':'customer-148','customer_actual_date':None}]
        source=Source({'datasets':{'supplier_orders':{'source':'supply_requests'}}})
        rows=source.fetch(self.con,{'source manager':'supply'},dt.date(2026,5,1),dt.date(2026,11,3))['supplier_orders']
        self.assertEqual(source.loaded_from,dt.date(2025,9,29))
        from app.onec import supplier_date_from
        self.assertEqual(supplier_date_from(self.con,dt.date(2026,5,1)),dt.date(2025,9,29))
        self.assertIsNone(rows[0]['customer_actual_date'])

    def test_import_preserves_customer_key_and_empty_actual_date(self):
        class Source:
            def _odata(self, entity, params):
                if entity == "Document_ЗаказПоставщику":
                    return [{"Ref_Key": "s1", "Number": "675", "Date": "2026-09-29T00:00:00",
                             "ТТК_ЗаказКлиента_Key": "c148"}]
                if entity == "Document_ЗаказКлиента":
                    return [{"Ref_Key": "c148", "Number": "148", "Date": "2026-09-23T00:00:00",
                             "ДокументОснование": "base", "ТТК_КонечнаяДатаПоставки": "2026-11-01T00:00:00",
                             "ТТК_ФактическаяДатаПоставки": "0001-01-01T00:00:00"}]
                return []
        row = fetch_supplier_orders(Source(), {}, dt.date(2026, 9, 1), dt.date(2026, 11, 3))[0]
        self.assertEqual(row.get("customer_order_key"), "c148")
        self.assertEqual(row.get("customer_order_date"), "2026-09-23")
        self.assertEqual(row.get("planned_date"), "2026-11-01")
        self.assertIsNone(row.get("customer_actual_date"))
