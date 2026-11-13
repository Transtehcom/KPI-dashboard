import copy
import unittest

from app import logistics_cargo
from app.common import connect, SYNC_TABLES


def entries():
    return [{"criterion": key, "cases": None, "damage": None, "comment": ""}
            for key, _ in logistics_cargo.CRITERIA]


class LogisticsCargoTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)

    def test_month_employee_and_criterion_are_isolated_and_zero_is_preserved(self):
        rows = entries()
        rows[0].update(cases=2, damage=100.255, comment="Повреждена упаковка груза")
        rows[1].update(cases=0, damage=0, comment="Проверено")
        with self.con:
            logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
        report = logistics_cargo.report(self.con, "logist", "2026-11")
        self.assertEqual((report["cases_total"], report["damage_total"], report["entered"]), (2, 100.26, 2))
        self.assertEqual(report["rows"][1]["cases"], 0)
        self.assertEqual(report["rows"][1]["damage"], 0)
        self.assertIsNone(logistics_cargo.report(self.con, "other", "2026-11")["damage_total"])
        self.assertIsNone(logistics_cargo.report(self.con, "logist", "2026-10")["damage_total"])
        self.assertEqual(self.con.execute("SELECT author FROM logistics_cargo_checks LIMIT 1").fetchone()[0], "boss")
        self.assertNotIn("logistics_cargo_checks", SYNC_TABLES)

    def test_invalid_final_row_cannot_partially_replace_saved_values(self):
        original = entries()
        original[0].update(cases=1, damage=200, comment="Исходное значение")
        logistics_cargo.save(self.con, "logist", "2026-11", original, "boss")
        for field, value in (("cases", -1), ("cases", 1.5), ("cases", True), ("cases", 2**63),
                             ("damage", -1), ("damage", float("nan")), ("damage", float("inf")),
                             ("damage", True), ("damage", "invalid")):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(original)
                changed[0]["damage"] = 999
                changed[-1][field] = value
                with self.assertRaises(ValueError):
                    logistics_cargo.save(self.con, "logist", "2026-11", changed, "boss")
                self.assertEqual(logistics_cargo.report(self.con, "logist", "2026-11")["damage_total"], 200)

    def test_shape_duplicates_and_unknown_criteria_are_rejected(self):
        for rows in (None, {}, entries()[:-1], entries() + entries()[:1]):
            with self.assertRaises(ValueError):
                logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
        rows = entries()
        rows[-1]["criterion"] = rows[0]["criterion"]
        with self.assertRaises(ValueError):
            logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
        rows[-1]["criterion"] = "unknown"
        with self.assertRaises(ValueError):
            logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")

    def test_comment_only_row_survives_and_empty_values_clear_it(self):
        rows = entries()
        rows[0]["comment"] = "Причина проверяется"
        logistics_cargo.save(self.con, "logist", "2026-11", rows, "boss")
        report = logistics_cargo.report(self.con, "logist", "2026-11")
        self.assertEqual(report["rows"][0]["comment"], "Причина проверяется")
        self.assertIsNone(report["cases_total"])
        self.assertIsNone(report["damage_total"])
        logistics_cargo.save(self.con, "logist", "2026-11", entries(), "boss")
        self.assertEqual(self.con.execute("SELECT count(*) FROM logistics_cargo_checks").fetchone()[0], 0)
