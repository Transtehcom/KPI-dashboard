import copy
import unittest

from app.onec_operations import fetch, identity


def guid(number):
    return f"00000000-0000-0000-0000-{number:012d}"


class Source:
    base = "https://example.test/trade/"
    cfg = {}

    def __init__(self, datasets=None):
        self.datasets = datasets or {}
        self.calls = []
        self.failure = None

    def _odata(self, entity, params):
        self.calls.append((entity, params.copy()))
        if entity == self.failure:
            raise RuntimeError("private request URL and credentials")
        rows = copy.deepcopy(self.datasets.get(entity, []))
        if entity.startswith("Catalog_") and params.get("$filter"):
            rows = [r for r in rows if r["Ref_Key"] in params["$filter"]]
        start = int(params.get("$skip", 0))
        return rows[start:start + int(params.get("$top", len(rows) or 1))]


class OperationsSourceTests(unittest.TestCase):
    def test_supply_best_price_total_uses_published_line_amounts(self):
        source=Source({'Document_ТТК_ЗаявкаВОтделСнабжения':[{'Ref_Key':guid(1),'ТаблицаТовары':[
            {'LineNumber':'1','Количество':10,'ЛучшаяЦена':139344.26,'ЛучшаяСумма':1393442.6,'СуммаТекущегоПоставщика':1393442.6,'Примечание':'Confirmed'},
            {'LineNumber':'2','ЛучшаяСумма':37030739.01}]}]})
        result=fetch(source,{})
        row=result['entities'][0]
        self.assertEqual(row['best_price_total'],38424181.61)
        self.assertIsNone(row['vat_included'])
        self.assertEqual(row['items'][0]['best_price'],139344.26)
        self.assertEqual(row['items'][0]['note'],'Confirmed')

    def test_identity_includes_base_kind_and_normalized_guid(self):
        source = Source()
        key = identity(source, "kp", guid(1).upper())
        self.assertRegex(key, r"^onec:[a-f0-9]+:kp:" + guid(1) + "$")
        self.assertNotEqual(key, identity(source, "tender", guid(1)))
        other = Source()
        other.base = "https://example.test/another/"
        self.assertNotEqual(key, identity(other, "kp", guid(1)))
        source.base = "https://someone:secret@example.test/trade/"
        self.assertEqual(key, identity(source, "kp", guid(1)))

    def test_deadlines_vat_and_typed_links_remain_separate(self):
        kp = {"Ref_Key": guid(1), "Number": "КП", "Date": "2026-10-23T09:51:55",
              "ТТК_СрокПодачи": "2026-10-29T00:00:00", "СрокДействия": "2026-12-31T00:00:00",
              "СуммаДокумента": 122, "ЦенаВключаетНДС": False,
              "Товары": [{"Сумма": 100, "СуммаНДС": 22, "СуммаСНДС": 122}]}
        tender = {"Ref_Key": guid(2), "Date": "2026-10-13T16:39:26", "СуммаДокумента": 0,
                  "ТекущаяПереторжка": 100, "ЦенаВключаетНДС": False,
                  "СрокДействия": "2026-10-20T09:00:00"}
        transport = {"Ref_Key": guid(3), "ДокументОснование": guid(1),
                     "ДокументОснование_Type": "StandardODATA.Document_КоммерческоеПредложениеКлиенту"}
        untyped = {"Ref_Key": guid(4), "ДокументОснование": guid(1)}
        source = Source({"Document_КоммерческоеПредложениеКлиенту": [kp], "Document_ТТК_Тендер": [tender],
                         "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов": [transport, untyped]})
        result = fetch(source, {})
        entities = {e["ref"]: e for e in result["entities"]}
        self.assertEqual(entities[guid(1)]["due"], "2026-10-29T00:00:00")
        self.assertEqual(entities[guid(1)]["price_valid_until"], "2026-12-31T00:00:00")
        self.assertEqual((entities[guid(1)]["amount"], entities[guid(1)]["vat_included"]), (122, True))
        self.assertEqual((entities[guid(2)]["amount"], entities[guid(2)]["vat_included"]), (100, False))
        self.assertEqual(entities[guid(3)]["links"], [identity(source, "kp", guid(1))])
        self.assertNotEqual(entities[guid(3)]["due"], "2026-12-31T00:00:00")
        self.assertEqual(entities[guid(4)]["links"], [])

    def test_supply_rows_quotes_and_selected_supplier_preserve_unknown_conditions(self):
        row = {"LineNumber": "1", "Номенклатура_Key": guid(8), "Количество": 5,
               "ЕдиницаИзмерения_Key": guid(9), "ЦенаПоставки1": 42, "ЦенаПоставки2": 0,
               "ТекущийПоставщик_Key": guid(6), "ЦенаТекущегоПоставщика": 42,
               "ДатаОбновленияЦен": "09.11.2026", "Флаг": False}
        request = {"Ref_Key": guid(1), "Date": "2026-11-09T11:12:13", "СтатусЗаявки": "Отработана",
                   "ДатаЗакрытияЗаявки": "2026-11-09T12:13:14", "Поставщик1_Key": guid(6),
                   "Поставщик2_Key": guid(7), "ТаблицаТовары": [row, dict(row, LineNumber="2")]}
        source = Source({"Document_ТТК_ЗаявкаВОтделСнабжения": [request],
                         "Catalog_Партнеры": [{"Ref_Key": guid(6), "Description": "Поставщик"}],
                         "Catalog_Номенклатура": [{"Ref_Key": guid(8), "Description": "Труба"}],
                         "Catalog_УпаковкиЕдиницыИзмерения": [{"Ref_Key": guid(9), "Description": "шт"}]})
        entity = fetch(source, {})["entities"][0]
        first, second = entity["items"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(first["included"])
        self.assertEqual(first["name"], "Труба")
        self.assertEqual(first["unit"], "шт")
        self.assertEqual(first["selected_supplier_id"], guid(6))
        self.assertEqual(first["offers"][0]["supplier_name"], "Поставщик")
        self.assertEqual(first["offers"][1]["price"], 0)
        self.assertIsNone(first["offers"][0]["offered_at"])
        self.assertIsNone(first["offers"][0]["currency"])
        self.assertIsNone(first["offers"][0]["vat_included"])
        self.assertIsNone(entity["closed_by"])
        self.assertEqual(entity["closed_at"], "2026-11-09T12:13:14")

    def test_register_events_keep_authors_repeated_transitions_and_seconds(self):
        states = [{"Документ": guid(1), "Документ_Type": "StandardODATA.Document_ТТК_Тендер",
                   "Период": at, "Состояние": status, "Автор_Key": guid(5)}
                  for at, status in [("2026-10-01T11:12:13", "Действует"),
                                     ("2026-10-02T12:13:14", "Черновик"),
                                     ("2026-10-03T13:14:15", "Действует")]]
        source = Source({"InformationRegister_ТТКСостоянияДокументов": states,
                         "Document_ТТК_Тендер": [{"Ref_Key": guid(1)}],
                         "Catalog_Пользователи": [{"Ref_Key": guid(5), "Description": "Иванов"}]})
        result = fetch(source, {"employees": [{"login": "ivanov", "onec_manager": "ИВАНОВ"}]})
        submitted = [e for e in result["events"] if e["kind"] == "submitted"]
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0]["author"], "ivanov")
        self.assertEqual(submitted[0]["at"], "2026-10-01T11:12:13")
        self.assertEqual(len({e["id"] for e in result["events"]}), 3)
        self.assertEqual(result["events"], fetch(source, {"employees": [{"login": "ivanov", "onec_manager": "ИВАНОВ"}]})["events"])

    def test_pagination_reads_through_short_pages_and_failure_does_not_leak_secrets(self):
        source = Source({"Document_ТТК_ЗаявкаВОтделСнабжения": [{"Ref_Key": guid(i)} for i in range(1, 4)]})
        result = fetch(source, {"operations": {"page_size": 2}})
        self.assertEqual(len(result["entities"]), 3)
        calls = [p for e, p in source.calls if e == "Document_ТТК_ЗаявкаВОтделСнабжения"]
        self.assertEqual([p["$skip"] for p in calls], [0, 2, 3])
        self.assertTrue(all("DeletionMark eq false" in p["$filter"] for p in calls))
        source.failure = "Document_ТТК_ЗаявкаВОтделСнабжения"
        result = fetch(source, {})
        self.assertIn("supply", result["errors"])
        self.assertNotIn("private", str(result))

    def test_transport_source_history_has_author_and_derived_due_is_unverified(self):
        source = Source({
            "Document_ТТК_Тендер": [{"Ref_Key": guid(1), "СрокДействия": "2026-10-20T09:00:00"}],
            "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов": [{
                "Ref_Key": guid(2), "Статус": "Отработана", "ДокументОснование": guid(1),
                "ДокументОснование_Type": "StandardODATA.Document_ТТК_Тендер",
                "ИсторияСтатусов": [{"LineNumber": "1", "ДатаУстановки": "2026-10-19T19:44:40",
                                       "Статус": "Отработана", "Пользователь_Key": guid(3)}]}],
            "Catalog_Пользователи": [{"Ref_Key": guid(3), "Description": "Логист"}]})
        transport = [e for e in fetch(source, {})["entities"] if e["kind"] == "transport"][0]
        self.assertEqual(transport["closed_at"], "2026-10-19T19:44:40")
        self.assertEqual(transport["closed_by"], "1c:Логист")
        self.assertEqual(transport["due"], "2026-10-20T09:00:00")
        self.assertFalse(transport["due_verified"])
        self.assertEqual(transport["events"][0]["author_name"], "Логист")

    def test_unpublished_register_is_coverage_warning_without_invented_event(self):
        source = Source({"Document_ТТК_Тендер": [{"Ref_Key": guid(1), "Статус": "Действует"}]})
        source.failure = "InformationRegister_ТТКСостоянияДокументов"
        result = fetch(source, {})
        self.assertEqual(result["errors"], {})
        self.assertTrue(result["warnings"])
        self.assertEqual(result["events"], [])

    def test_verified_supply_closure_actor_can_be_a_literal_register_author(self):
        source = Source({
            "Document_ТТК_ЗаявкаВОтделСнабжения": [{"Ref_Key": guid(1), "СтатусЗаявки": "Отработана",
                "ДатаЗакрытияЗаявки": "2026-11-09T12:13:14"}],
            "InformationRegister_ТТКСостоянияДокументов": [{"Документ": guid(1),
                "Документ_Type": "StandardODATA.Document_ТТК_ЗаявкаВОтделСнабжения",
                "Период": "2026-11-09T12:13:14", "Состояние": "Отработана", "Автор": "Иванов"}]})
        result = fetch(source, {"employees": [{"login": "ivanov", "onec_manager": "Иванов"}]})
        self.assertEqual(result["entities"][0]["closed_by"], "ivanov")
        self.assertEqual(result["events"][0]["author_name"], "Иванов")


if __name__ == "__main__":
    unittest.main()
