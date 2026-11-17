import copy
import datetime as dt
import unittest
import urllib.parse

from app import onec_workload


def guid(number):
    return f"00000000-0000-0000-0000-{number:012d}"


COMMON = {"Ref_Key": "Edm.Guid", "Number": "Edm.String", "Date": "Edm.DateTime",
          "Posted": "Edm.Boolean", "DeletionMark": "Edm.Boolean"}


class Source:
    base = "https://example.test/rootbase/"
    cfg = {}

    def __init__(self, schemas, rows=None, page_size=2, next_links=False):
        self.schemas = {"Catalog_Пользователи": {"Ref_Key": "Edm.Guid", "Description": "Edm.String"}, **schemas}
        self.rows = {"Catalog_Пользователи": [
            {"Ref_Key": guid(90), "Description": "Иван Иванов"},
            {"Ref_Key": guid(91), "Description": "Анастасия Шашмурина"},
            {"Ref_Key": guid(92), "Description": "Анастасия Петрова"}], **(rows or {})}
        self.calls = []
        self.page_size = page_size
        self.next_links = next_links
        self.failure = None

    def metadata(self):
        types = "".join('<EntityType Name="' + name + '">' +
                        "".join(f'<Property Name="{field}" Type="{typ}"/>' for field, typ in fields.items()) +
                        "</EntityType>" for name, fields in self.schemas.items())
        sets = "".join(f'<EntitySet Name="{name}" EntityType="StandardODATA.{name}"/>' for name in self.schemas)
        return f'<Schema Namespace="StandardODATA">{types}<EntityContainer>{sets}</EntityContainer></Schema>'

    def _get(self, url):
        parsed = urllib.parse.urlsplit(url)
        entity = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        self.calls.append((entity, params))
        if entity == self.failure:
            raise RuntimeError("private https://user:secret@source.test/rootbase/")
        start = int(params.get("$skip", 0))
        batch = copy.deepcopy(self.rows.get(entity, [])[start:start + self.page_size])
        result = {"value": batch}
        if self.next_links and start + len(batch) < len(self.rows.get(entity, [])):
            params["$skip"] = start + len(batch)
            result["@odata.nextLink"] = entity + "?" + urllib.parse.urlencode(params)
        return result


CONFIG = {"employees": [
    {"login": "ivanov", "onec_manager": "Иван Иванов"},
    {"login": "shashmurina", "onec_manager": "Анастасия Шашмурина"},
    {"login": "petrovaaa", "onec_manager": "Анастасия Петрова"}]}
SINCE = dt.date(2026, 5, 1)


def document(ref=1, **values):
    return {"Ref_Key": guid(ref), "Number": str(ref), "Date": "2026-10-15T09:10:11",
            "Posted": False, "DeletionMark": False, **values}


class WorkloadOneCTests(unittest.TestCase):
    def test_responsible_schema_precedence_and_author_only_exceptions(self):
        entity = "Document_ЗаказКлиента"
        source = Source({entity: {**COMMON, "Ответственный_Key": "Edm.Guid", "Менеджер_Key": "Edm.Guid",
                                  "Автор": "Edm.String", "Автор_Type": "Edm.String"}}, {entity: [
            document(1, Ответственный_Key=guid(90), Менеджер_Key=guid(92), Автор=guid(91), Автор_Type="StandardODATA.Catalog_Пользователи"),
            document(2, Ответственный_Key=guid(99), Менеджер_Key=guid(90), Автор=guid(90), Автор_Type="StandardODATA.Catalog_Пользователи"),
            document(3, Ответственный_Key=guid(91), Автор=guid(99), Автор_Type="StandardODATA.Catalog_Пользователи")]})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertEqual([(row["ref"], row["emp"], row["owner_field"]) for row in result["rows"]],
                         [(guid(1), "ivanov", "Ответственный_Key"), (guid(1), "shashmurina", "Автор")])
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn("Ответственный_Key eq guid'" + guid(90), query["$filter"])
        self.assertIn("Автор eq cast(guid'" + guid(91) + "', 'Catalog_Пользователи')", query["$filter"])
        self.assertNotIn("Менеджер", query["$filter"])
        self.assertIn("Posted eq false or Date ge datetime'2026-05-01T00:00:00'", query["$filter"])

    def test_custom_responsibles_precede_manager_and_keep_multiple_people(self):
        entity = "Document_ТТК_ЗаявкаВОтделСнабжения"
        config = {"employees": [{"login": "a", "onec_manager": guid(90)}, {"login": "b", "onec_manager": guid(93)}]}
        source = Source({entity: {**COMMON, "ОтветственныйСнабжение_Key": "Edm.Guid",
                                  "ТТК_ОтветственныйСнабженец_Key": "Edm.Guid", "Менеджер_Key": "Edm.Guid"}},
                        {entity: [document(1, ОтветственныйСнабжение_Key=guid(90),
                                           ТТК_ОтветственныйСнабженец_Key=guid(93), Менеджер_Key=guid(92))]})
        rows = onec_workload.fetch(source, config, SINCE)["rows"]
        self.assertEqual({row["emp"] for row in rows}, {"a", "b"})
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_deadline_end_of_day_and_real_completion_only(self):
        order = "Document_ЗаказКлиента"
        supply = "Document_ТТК_ЗаявкаВОтделСнабжения"
        source = Source({order: {**COMMON, "Менеджер_Key": "Edm.Guid", "ТТК_КонечнаяДатаПоставки": "Edm.DateTime"},
                         supply: {**COMMON, "Менеджер_Key": "Edm.Guid", "СрокОтработкиДо": "Edm.DateTime",
                                  "ДатаЗакрытияЗаявки": "Edm.DateTime"}}, {
            order: [document(1, Менеджер_Key=guid(90), Posted=True, ТТК_КонечнаяДатаПоставки="2026-11-10T00:00:00")],
            supply: [document(2, Менеджер_Key=guid(90), Posted=True, СрокОтработкиДо="2026-11-11T08:00:00Z",
                              ДатаЗакрытияЗаявки="2026-11-11T10:12:13Z")]})
        rows = {row["ref"]: row for row in onec_workload.fetch(source, CONFIG, SINCE)["rows"]}
        self.assertEqual(rows[guid(1)]["deadline"], "2026-11-10T23:59:59")
        self.assertIsNone(rows[guid(1)]["closed_at"])
        self.assertEqual(rows[guid(1)]["date"], "2026-10-15T09:10:11")
        self.assertEqual(rows[guid(2)]["deadline"], "2026-11-11T13:00:00")
        self.assertEqual(rows[guid(2)]["closed_at"], "2026-11-11T15:12:13")
        self.assertTrue(rows[guid(1)]["url"].startswith(source.base + "#e1cib/data/Документ."))

    def test_pagination_follows_next_links_and_continues_short_pages(self):
        entity = "Document_Прочее"
        for next_links in (False, True):
            with self.subTest(next_links=next_links):
                source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}},
                                {entity: [document(i, Менеджер_Key=guid(90)) for i in range(1, 6)]},
                                next_links=next_links)
                result = onec_workload.fetch(source, CONFIG, SINCE)
                self.assertEqual(len(result["rows"]), 5)
                self.assertEqual(result["entities"], [entity])
                self.assertEqual(result["errors"], {})
                self.assertEqual(result["since"], "2026-05-01")

    def test_failed_entity_preserves_successes_without_private_exception_text(self):
        first, second = "Document_Первый", "Document_Второй"
        source = Source({name: {**COMMON, "Менеджер_Key": "Edm.Guid"} for name in (first, second)},
                        {first: [document(1, Менеджер_Key=guid(90))]})
        source.failure = second
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertEqual(result["entities"], [first])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["errors"][second], "RuntimeError")
        self.assertNotIn("secret", str(result))

    def test_deletion_old_posted_and_wrong_polymorphic_type_are_excluded(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid", "Автор": "Edm.String", "Автор_Type": "Edm.String"}},
                        {entity: [document(1, Менеджер_Key=guid(90), Date="2020-01-01T12:00:00"),
                                  document(2, Менеджер_Key=guid(90), Posted=True, Date="2020-01-01T12:00:00"),
                                  document(3, Менеджер_Key=guid(90), DeletionMark=True),
                                  document(4, Автор=guid(91), Автор_Type="StandardODATA.Catalog_Партнеры")]})
        rows = onec_workload.fetch(source, CONFIG, SINCE)["rows"]
        self.assertEqual([row["ref"] for row in rows], [guid(1)])


    def test_unresolved_employee_mapping_preserves_previous_snapshot(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}})
        config = {"employees": [{"login": "missing", "onec_manager": "Неизвестный пользователь"}]}
        result = onec_workload.fetch(source, config, SINCE)
        self.assertIn("Catalog_Пользователи", result["errors"])
        self.assertEqual(result["entities"], [])
        self.assertFalse(any(name == entity for name, _ in source.calls))

    def test_ambiguous_direct_guid_mapping_is_an_error(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}})
        config = {"employees": [{"login": "first", "onec_manager": guid(90)},
                                {"login": "second", "onec_manager": guid(90)}]}
        result = onec_workload.fetch(source, config, SINCE)
        self.assertIn("Catalog_Пользователи", result["errors"])
        self.assertEqual(result["entities"], [])

    def test_unknown_deadline_and_completion_stay_unknown(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid", "СрокХранения": "Edm.DateTime",
                                 "ДатаЗаписи": "Edm.DateTime"}}, {entity: [
            document(1, Posted=True, Менеджер_Key=guid(90), СрокХранения="2026-11-16T00:00:00",
                     ДатаЗаписи="2026-11-15T11:12:13")]})
        row = onec_workload.fetch(source, CONFIG, SINCE)["rows"][0]
        self.assertIsNone(row["deadline"])
        self.assertIsNone(row["closed_at"])
        params = next(params for name, params in source.calls if name == entity)
        self.assertNotIn("СрокХранения", params["$select"])
        self.assertNotIn("ДатаЗаписи", params["$select"])

    def test_filters_fit_standard_iis_query_limit_with_cyrillic_names(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}})
        source.rows["Catalog_Пользователи"] = [
            {"Ref_Key": guid(90 + index), "Description": "Сотрудник Имя Отчество ДлиннаяФамилия" + str(index)}
            for index in range(18)]
        config = {"employees": [{"login": "person" + str(index), "onec_manager": user["Description"]}
                                for index, user in enumerate(source.rows["Catalog_Пользователи"])]}
        result = onec_workload.fetch(source, config, SINCE)
        self.assertEqual(result["errors"], {})
        self.assertTrue(all(len(urllib.parse.urlencode(params, quote_via=urllib.parse.quote)) < 1800
                            for _, params in source.calls))

    def test_goods_receipt_is_immediately_completed_without_timing(self):
        entity = "Document_ПриобретениеТоваровУслуг"
        fields = {**COMMON, "Ответственный_Key": "Edm.Guid", "СрокВыполнения": "Edm.DateTime",
                  "ДатаФактическогоВыполнения": "Edm.DateTime"}
        source = Source({entity: fields}, {entity: [
            document(1, Ответственный_Key=guid(90), Posted=False, Date="2026-11-16T14:58:11",
                     СрокВыполнения="2026-11-01T12:00:00", ДатаФактическогоВыполнения="2026-11-20T12:00:00"),
            document(2, Ответственный_Key=guid(90), Posted=False, Date="2020-01-01T00:00:00")]})
        rows = onec_workload.fetch(source, CONFIG, SINCE)['rows']
        self.assertEqual(len(rows), 1)
        self.assertTrue(onec_workload.document_closed(rows[0]))
        self.assertEqual(rows[0]['closed_at'], rows[0]['date'])
        self.assertEqual(rows[0]['closed_at'], '2026-11-16T14:58:11')
        self.assertIsNone(rows[0]['deadline'])

    def test_transport_2416_uses_last_processed_time_and_primary_basis_deadline(self):
        entity = "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов"
        tender = "Document_ТТК_Тендер"
        fields = {**COMMON, "ОтветственныйЛогист_Key": "Edm.Guid", "Статус": "Edm.String",
                  "ИсторияСтатусов": "Collection(History)", "ДокументОснование": "Edm.String",
                  "ДокументОснование_Type": "Edm.String"}
        source = Source({entity: fields, tender: {**COMMON, "СрокДействия": "Edm.DateTime"}}, {entity: [
            document(1, Number="000002416", ОтветственныйЛогист_Key=guid(90), Posted=True, Статус="Отработана",
                     ДокументОснование=guid(50), ДокументОснование_Type="StandardODATA." + tender,
                     ИсторияСтатусов=[{"Статус": "Поступила", "ДатаУстановки": "2026-11-16T14:58:11"},
                                     {"Статус": "Отработана", "ДатаУстановки": "2026-11-16T15:00:00"},
                                     {"Статус": "Отработана", "ДатаУстановки": "2026-11-16T15:44:43"}]),
            document(2, ОтветственныйЛогист_Key=guid(90), Posted=True, Статус="ВРаботе"),
            document(3, ОтветственныйЛогист_Key=guid(90), Posted=True, Статус="Поступила"),
            document(4, ОтветственныйЛогист_Key=guid(90), Posted=True, Статус="ЗапросИнформации")],
            tender: [document(50, Number="Т-000910", СрокДействия="2026-11-26T16:00:00")]})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        rows = [row for row in result['rows'] if row['entity'] == entity]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['deadline'], '2026-11-26T16:00:00')
        self.assertIn('Т-000910', rows[0]['deadline_source'])
        self.assertEqual(rows[0]['closed_at'], '2026-11-16T15:44:43')
        self.assertEqual([onec_workload.document_closed(row) for row in rows], [True, False, False, False])
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn('ИсторияСтатусов', query['$select'])
        self.assertNotIn('Posted eq false', query['$filter'])

    def test_transport_native_deadline_has_priority_and_missing_history_stays_unknown(self):
        entity = "Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов"
        fields = {**COMMON, "ОтветственныйЛогист_Key": "Edm.Guid", "Статус": "Edm.String",
                  "ИсторияСтатусов": "Collection(History)", "ОтработатьДо": "Edm.DateTime"}
        source = Source({entity: fields}, {entity: [document(1, ОтветственныйЛогист_Key=guid(90),
            Posted=False, Статус="Отработана", ОтработатьДо="2026-11-26T16:00:00",
            ИсторияСтатусов=[{"Статус": "Поступила", "ДатаУстановки": "2026-11-16T14:58:11"}])]})
        row = onec_workload.fetch(source, CONFIG, SINCE)['rows'][0]
        self.assertTrue(onec_workload.document_closed(row))
        self.assertEqual(row['deadline'], '2026-11-26T16:00:00')
        self.assertEqual(row['deadline_source'], 'ОтработатьДо')
        self.assertIsNone(row['closed_at'])

    def test_forwarder_dfp_controls_closure_and_dpp_is_optional(self):
        entity = "Document_ПоручениеЭкспедитору"
        fields = {**COMMON, "ТТК_ОтветственныйЛогист_Key": "Edm.Guid", "ТТК_Статус": "Edm.String",
                  "ТТК_ДПП": "Edm.DateTime", "ТТК_ДФП": "Edm.DateTime",
                  "СрокВыполнения": "Edm.DateTime", "ДатаФактическогоВыполнения": "Edm.DateTime"}
        source = Source({entity: fields}, {entity: [
            document(1, ТТК_ОтветственныйЛогист_Key=guid(90), Posted=True, ТТК_Статус="ВПути",
                     Date="2020-01-01T00:00:00", ТТК_ДПП="2026-11-20T11:00:00"),
            document(2, ТТК_ОтветственныйЛогист_Key=guid(90), Posted=False, ТТК_Статус="Завершено",
                     ТТК_ДПП="2026-11-17T00:00:00", ТТК_ДФП="2026-11-17T13:15:30"),
            document(3, ТТК_ОтветственныйЛогист_Key=guid(90), Posted=True, ТТК_Статус="Выдан",
                     ТТК_ДПП="0001-01-01T00:00:00", СрокВыполнения="2026-11-20T15:00:00",
                     ТТК_ДФП="2026-11-17T10:00:00"),
            document(4, ТТК_ОтветственныйЛогист_Key=guid(90), Posted=True, ТТК_Статус="Завершено",
                     ТТК_ДФП="0001-01-01T00:00:00", ДатаФактическогоВыполнения="2026-11-17T10:00:00"),
            document(5, ТТК_ОтветственныйЛогист_Key=guid(90), Posted=True, ТТК_Статус="Завершено", Date="2020-01-01T00:00:00")]})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['rows']), 5)
        self.assertEqual([onec_workload.document_closed(row) for row in result['rows']], [False, True, True, False, False])
        self.assertEqual(result['rows'][0]['deadline'], '2026-11-20T11:00:00')
        self.assertEqual(result['rows'][0]['deadline_source'], 'ТТК_ДПП')
        self.assertEqual(result['rows'][1]['deadline'], '2026-11-17T23:59:59')
        self.assertEqual(result['rows'][1]['closed_at'], '2026-11-17T13:15:30')
        self.assertIsNone(result['rows'][2]['deadline'])
        self.assertEqual(result['rows'][2]['closed_at'], '2026-11-17T10:00:00')
        self.assertIsNone(result['rows'][3]['closed_at'])
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn('ТТК_Статус', query['$select'])
        self.assertNotIn('Posted eq false', query['$filter'])

    def test_supply_status_overrides_posting_and_retains_old_open_requests(self):
        entity = "Document_ТТК_ЗаявкаВОтделСнабжения"
        fields = {**COMMON, "ОтветственныйСнабжение_Key": "Edm.Guid", "СтатусЗаявки": "Edm.String",
                  "ДатаЗакрытияЗаявки": "Edm.DateTime", "СрокОтработкиДо": "Edm.DateTime"}
        source = Source({entity: fields}, {entity: [
            document(1, ОтветственныйСнабжение_Key=guid(90), Number="Т-000912", Posted=True,
                     СтатусЗаявки="ВРаботе", ДатаЗакрытияЗаявки="0001-01-01T00:00:00", СрокОтработкиДо="2026-11-20T11:00:00"),
            document(2, ОтветственныйСнабжение_Key=guid(90), Posted=False,
                     СтатусЗаявки="Отработана", ДатаЗакрытияЗаявки="2026-11-17T10:00:00"),
            document(3, ОтветственныйСнабжение_Key=guid(90), Posted=True, СтатусЗаявки="Отменена"),
            document(4, ОтветственныйСнабжение_Key=guid(90), Posted=True, СтатусЗаявки="ВРаботе", Date="2020-01-01T00:00:00"),
            document(5, ОтветственныйСнабжение_Key=guid(90), Posted=True, СтатусЗаявки="Поступила"),
            document(6, ОтветственныйСнабжение_Key=guid(90), Posted=True, СтатусЗаявки="ЗапросПроектнойДокументации")]})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['rows']), 6)
        self.assertEqual([onec_workload.document_closed(row) for row in result['rows']], [False, True, True, False, False, False])
        self.assertEqual(result['rows'][0]['status'], 'ВРаботе')
        self.assertIsNone(result['rows'][0]['closed_at'])
        self.assertEqual(result['rows'][0]['deadline'], '2026-11-20T11:00:00')
        self.assertEqual(result['rows'][1]['closed_at'], '2026-11-17T10:00:00')
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn('СтатусЗаявки', query['$select'])
        self.assertNotIn('Posted eq false', query['$filter'])

    def test_tender_status_overrides_posting_and_keeps_old_open_tenders(self):
        entity = "Document_ТТК_Тендер"
        states = [("Черновик", True), ("Подготовлен", True), ("Действует", True),
                  ("Выиграно", False), ("Проиграно", False), ("Отменено", False), ("Отклонён", False)]
        source = Source({entity: {**COMMON, "Статус": "Edm.String", "ВедущийМенеджер_Key": "Edm.Guid"}},
                        {entity: [document(i, ВедущийМенеджер_Key=guid(90), Posted=posted, Статус=status)
                                  for i, (status, posted) in enumerate(states, 1)] + [
                            document(8, ВедущийМенеджер_Key=guid(90), Posted=True, Статус="Действует", Date="2020-01-01T00:00:00"),
                            document(9, ВедущийМенеджер_Key=guid(90), Posted=False, Статус="Выиграно", Date="2020-01-01T00:00:00")]})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['rows']), 8)
        self.assertEqual([row['status'] for row in result['rows'][:7]], [state for state, _ in states])
        self.assertEqual([onec_workload.document_closed(row) for row in result['rows'][:7]], [False]*3 + [True]*4)
        self.assertEqual([row['posted'] for row in result['rows'][:7]], [posted for _, posted in states])
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn('Статус', query['$select'])
        self.assertNotIn('Posted eq false', query['$filter'])

    def test_tender_leading_manager_is_used_when_responsible_and_manager_are_absent(self):
        entity = "Document_ТТК_Тендер"
        source = Source({entity: {**COMMON, "ВедущийМенеджер_Key": "Edm.Guid"}},
                        {entity: [document(1, ВедущийМенеджер_Key=guid(90))]})
        row = onec_workload.fetch(source, CONFIG, SINCE)["rows"][0]
        self.assertEqual(row["owner_field"], "ВедущийМенеджер_Key")

    def test_document_queries_keep_all_open_dates_and_request_only_scalar_columns(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid", "Товары": "Collection(Items)"}})
        onec_workload.fetch(source, CONFIG, SINCE)
        query = next(params for name, params in source.calls if name == entity)
        self.assertIn("DeletionMark eq false", query["$filter"])
        self.assertIn("(Posted eq false or Date ge datetime'2026-05-01T00:00:00')", query["$filter"])
        self.assertIn("Менеджер_Key eq guid'" + guid(90) + "'", query["$filter"])
        self.assertNotIn("Товары", query["$select"])

    def test_repeated_page_discards_entity_rows_and_reports_safe_failure(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}},
                        {entity: [document(1, Менеджер_Key=guid(90))]})
        original = source._get

        def repeated_page(url):
            params = urllib.parse.urlsplit(url)
            if urllib.parse.unquote(params.path).endswith(entity):
                return {"value": copy.deepcopy(source.rows[entity])}
            return original(url)

        source._get = repeated_page
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["entities"], [])
        self.assertIn(entity, result["errors"])

    def test_legacy_d_results_and_next_link_are_supported(self):
        entity = "Document_Прочее"
        source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}},
                        {entity: [document(i, Менеджер_Key=guid(90)) for i in range(1, 4)]}, next_links=True)
        original = source._get

        def legacy_page(url):
            payload = original(url)
            result = {"results": payload["value"]}
            if payload.get("@odata.nextLink"):
                result["__next"] = payload["@odata.nextLink"]
            return {"d": result}

        source._get = legacy_page
        self.assertEqual(len(onec_workload.fetch(source, CONFIG, SINCE)["rows"]), 3)

    def test_http_success_without_explicit_collection_cannot_clear_previous_rows(self):
        entity = "Document_Прочее"
        for payload in ({"error": {"message": "private source data"}}, {}, {"d": {}},
                        {"value": [], "error": {"message": "failed source"}}):
            with self.subTest(payload=payload):
                source = Source({entity: {**COMMON, "Менеджер_Key": "Edm.Guid"}})
                original = source._get

                def malformed_page(url):
                    if urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) == entity:
                        return payload
                    return original(url)

                source._get = malformed_page
                result = onec_workload.fetch(source, CONFIG, SINCE)
                self.assertIn(entity, result["errors"])
                self.assertNotIn(entity, result["entities"])
                self.assertNotIn("private", str(result))
if __name__ == "__main__":
    unittest.main()
