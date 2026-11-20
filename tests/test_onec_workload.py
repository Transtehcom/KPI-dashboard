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
        if entity == 'Balance()':
            entity = urllib.parse.unquote(parsed.path.rsplit('/', 2)[-2]) + '/Balance()'
        params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        self.calls.append((entity, params))
        if entity == self.failure:
            raise RuntimeError("private https://user:secret@source.test/rootbase/")
        start = int(params.get("$skip", 0))
        batch = copy.deepcopy(self.rows.get(entity, [])[start:start + self.page_size])
        result = {"value": batch}
        if self.next_links and start + len(batch) < len(self.rows.get(entity, [])):
            params["$skip"] = start + len(batch)
            result["@odata.nextLink"] = parsed.path + "?" + urllib.parse.urlencode(params)
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
    def test_supplier_orders_require_full_payment_and_receipt_and_prioritize_completion_sources(self):
        order = 'Document_ЗаказПоставщику'
        receipt = 'Document_ПриобретениеТоваровУслуг'
        forwarder = 'Document_ПоручениеЭкспедитору'
        payment = 'Document_СписаниеБезналичныхДенежныхСредств'
        register = 'AccumulationRegister_РасчетыСПоставщиками'
        plan = 'AccumulationRegister_РасчетыСПоставщикамиПланОплат'
        quantity = 'AccumulationRegister_ЗаказыПоставщикам'
        schemas = {
            order: {**COMMON, 'Менеджер_Key': 'Edm.Guid', 'Статус': 'Edm.String', 'ОбъектРасчетов_Key': 'Edm.Guid',
                    'СуммаДокумента': 'Edm.Double', 'Валюта_Key': 'Edm.Guid', 'ПорядокРасчетов': 'Edm.String',
                    'ТТК_ДатаГотовностиСкорректированная': 'Edm.DateTime', 'ТТК_ДатаГотовностиПлановая': 'Edm.DateTime',
                    'СрокВыполнения': 'Edm.DateTime'},
            receipt: {**COMMON, 'ЗаказПоставщику_Key': 'Edm.Guid', 'Товары': 'Collection(ReceiptRow)'},
            forwarder: {**COMMON, 'ТТК_ЗаказПоставщику_Key': 'Edm.Guid', 'ТТК_ДФП': 'Edm.DateTime', 'Основания': 'Collection(BasisRow)'},
            payment: {**COMMON, 'ЗаявкаНаРасходованиеДенежныхСредств': 'Edm.String',
                      'ЗаявкаНаРасходованиеДенежныхСредств_Type': 'Edm.String', 'РасшифровкаПлатежа': 'Collection(PaymentRow)'},
            register + '_RecordType': {'ОбъектРасчетов_Key': 'Edm.Guid', 'Валюта_Key': 'Edm.Guid',
                                      'АналитикаУчетаПоПартнерам_Key': 'Edm.Guid', 'КОплате': 'Edm.Double', 'КПоступлению': 'Edm.Double'},
            plan + '_RecordType': {'ДокументПлан':'Edm.String','КОплате':'Edm.Double'},
            quantity + '_RecordType': {'ЗаказПоставщику_Key':'Edm.Guid','Заказано':'Edm.Double',
                                      'КОформлению':'Edm.Double','КПоступлению':'Edm.Double'},
        }
        orders = [document(i, Менеджер_Key=guid(90), Posted=True, Статус='Подтвержден', СуммаДокумента=1000,
                           ОбъектРасчетов_Key=guid(200+i), Валюта_Key=guid(800), ПорядокРасчетов='ПоЗаказам',
                           ТТК_ДатаГотовностиСкорректированная='0001-01-01T00:00:00',
                           ТТК_ДатаГотовностиПлановая='2026-11-20T00:00:00', СрокВыполнения='2026-12-31T12:00:00')
                  for i in range(1, 13)]
        orders[0]['Number'] = '00ЦБ-000710'
        orders[1]['Number'] = '00ЦБ-000707'
        orders[2]['ТТК_ДатаГотовностиСкорректированная'] = '2026-11-19T13:14:15'
        orders[6]['Date'] = '2020-01-01T12:00:00'
        orders[7]['ТТК_ДатаГотовностиПлановая'] = '0001-01-01T00:00:00'
        orders[7]['СуммаДокумента'] = 1e20
        orders[8].update(Posted=False)
        for raw in orders[9:]:
            raw.update(ПорядокРасчетов='ПоДоговорамКонтрагентов',ОбъектРасчетов_Key=guid(202),Товары=[{
                'КодСтроки':'1','Номенклатура_Key':guid(700),'Характеристика_Key':guid(0),
                'Количество':10,'СуммаСНДС':1000,'Отменено':False}])
        balances = [dict(ОбъектРасчетов_Key=guid(200+ref), Валюта_Key=guid(800),
                         АналитикаУчетаПоПартнерам_Key=guid(900+ref), КОплатеBalance=pay, КПоступлениюBalance=received)
                    for ref, pay, received in [(2, 0, 1000), (6, -500, 0), (7, -1000, 1000), (8, -0.01, 0)]]
        balances[1]['КОплатеBalance']=-250
        balances.append({**balances[1], 'АналитикаУчетаПоПартнерам_Key':guid(999)})
        balances.append({**balances[0], 'Валюта_Key':guid(801), 'ОбъектРасчетов_Key':guid(201)})
        receipts = [document(101, Number='receipt-first', Date='2026-11-17T11:12:13', ЗаказПоставщику_Key=guid(1), Товары=[]),
                    document(102, Number='receipt-last', Date='2026-11-18T14:15:16', Товары=[{'ЗаказПоставщику_Key': guid(1)}]),
                    document(103, Date='2026-11-30T12:00:00', ЗаказПоставщику_Key=guid(1), DeletionMark=True, Товары=[])]
        receipts.append(document(104,Number='contract-receipt',Date='2026-11-19T12:00:00',ЗаказПоставщику_Key=guid(10),Товары=[]))
        forwarders = [document(110, Number='forwarder-with-receipt', ТТК_ЗаказПоставщику_Key=guid(1), ТТК_ДФП='2026-11-25T15:00:00', Основания=[]),
                      document(111, Number='forwarder-first', Date='2026-11-10T12:00:00', ТТК_ЗаказПоставщику_Key=guid(3), ТТК_ДФП='2026-11-21T11:00:00', Основания=[]),
                      document(112, Number='forwarder-last', Date='2026-11-11T12:00:00', ТТК_ДФП='2026-11-20T16:17:18',
                               Основания=[{'Основание': guid(3), 'Основание_Type': 'StandardODATA.'+order}]),
                      document(113, ТТК_ЗаказПоставщику_Key=guid(5), ТТК_ДФП='0001-01-01T00:00:00', Основания=[])]
        payments = [document(120+i, Number='payment-'+str(i), Date=at,
                             РасшифровкаПлатежа=[{'ОбъектРасчетов_Key': guid(200+i)}])
                    for i, at in [(1, '2026-11-30T12:00:00'), (3, '2026-11-30T12:00:00'),
                                  (4, '2026-11-15T17:18:19'), (5, '2026-11-16T18:19:20')]]
        payments.extend([document(140,Number='contract-payment',Date='2026-11-25T11:12:13',РасшифровкаПлатежа=[{'ОбъектРасчетов_Key':guid(202)}]),
                         document(141,Number='other-contract-payment',Date='2026-11-30T11:12:13',РасшифровкаПлатежа=[{'ОбъектРасчетов_Key':guid(202)}])])
        plan_balance=[dict(ОбъектРасчетов_Key=guid(202),АналитикаУчетаПоПартнерам_Key=guid(900),Валюта_Key=guid(800),
                           ДокументПлан=guid(11),ДокументПлан_Type='StandardODATA.'+order,
                           ДатаПлановогоПогашения='2026-11-10T00:00:00',ДатаВозникновения='2026-11-10T00:00:00',
                           ВариантОплаты='КредитПослеПоступления',НераспределенныйАванс=False,КОплатеBalance=500)]
        quantity_balance=[dict(ЗаказПоставщику_Key=guid(11),Номенклатура_Key=guid(700),Характеристика_Key=guid(0),
                               КодСтроки='1',Склад_Key=guid(801),ЗаказаноBalance=5,КОформлениюBalance=0,КПоступлениюBalance=0)]
        plan_moves=[dict(Recorder=guid(140),Recorder_Type='StandardODATA.'+payment,Period='2026-11-25T11:12:13',LineNumber='1',
                         ДокументПлан=guid(12),ДокументПлан_Type='StandardODATA.'+order,ДокументОплаты=guid(140),
                         ДокументОплаты_Type='StandardODATA.'+payment,Active=True)]
        source = Source(schemas, {order: orders, receipt: receipts, forwarder: forwarders, payment: payments,
                                  register+'/Balance()': balances,plan+'/Balance()':plan_balance,
                                  quantity+'/Balance()':quantity_balance,plan+'_RecordType':plan_moves}, next_links=True)
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        rows = {row['ref']: row for row in result['rows'] if row['entity']==order}
        self.assertEqual(len(rows), 12)
        self.assertEqual({ref for ref,row in rows.items() if onec_workload.document_closed(row)}, {guid(i) for i in (1,3,4,5,10,12)})
        self.assertEqual((rows[guid(1)]['payment_pct'], rows[guid(1)]['receipt_pct']), (100,100))
        self.assertEqual((rows[guid(2)]['payment_pct'], rows[guid(2)]['receipt_pct']), (100,0))
        self.assertEqual(rows[guid(6)]['payment_pct'], 50)
        self.assertLess(rows[guid(8)]['payment_pct'],100)
        self.assertIsNone(rows[guid(9)]['payment_pct'])
        self.assertEqual(rows[guid(10)]['receipt_pct'],100)
        self.assertEqual(rows[guid(11)]['receipt_pct'],50)
        self.assertEqual(rows[guid(11)]['payment_pct'],50)
        self.assertEqual(rows[guid(12)]['closed_at'],'2026-11-25T11:12:13')
        self.assertEqual(rows[guid(1)]['closed_at'], '2026-11-18T14:15:16')
        self.assertIn('receipt-last', rows[guid(1)]['closed_at_source'])
        self.assertEqual(rows[guid(3)]['closed_at'], '2026-11-20T16:17:18')
        self.assertIn('forwarder-last', rows[guid(3)]['closed_at_source'])
        self.assertEqual(rows[guid(4)]['closed_at'], '2026-11-15T17:18:19')
        self.assertEqual(rows[guid(5)]['closed_at'], '2026-11-16T18:19:20')
        self.assertTrue(all(rows[guid(i)]['closed_at'] is None for i in (2,6,7,8,9,11)))
        self.assertEqual(rows[guid(1)]['deadline'],'2026-11-20T23:59:59')
        self.assertEqual(rows[guid(3)]['deadline'],'2026-11-19T13:14:15')
        self.assertIsNone(rows[guid(8)]['deadline'])
        query=next(params for name,params in source.calls if name==order)
        self.assertNotIn('Posted eq',query['$filter'])
        for entity in (register+'/Balance()',plan+'/Balance()',receipt,forwarder,payment):
            with self.subTest(failed_source=entity):
                source.failure=entity
                failed=onec_workload.fetch(source,CONFIG,SINCE)
                self.assertIn(order,failed['errors'])
                self.assertNotIn(order,failed['entities'])

    def test_quotes_use_submission_deadline_and_status_without_invented_completion(self):
        entity = 'Document_КоммерческоеПредложениеКлиенту'
        fields = {**COMMON, 'Менеджер_Key': 'Edm.Guid', 'Статус': 'Edm.String',
                  'ТТК_СрокПодачи': 'Edm.DateTime', 'СрокПодачи': 'Edm.DateTime',
                  'СрокДействия': 'Edm.DateTime', 'СрокВыполнения': 'Edm.DateTime',
                  'ДатаФактическогоВыполнения': 'Edm.DateTime'}
        statuses = ['Действует', 'Отменено', 'Черновик', 'НеСогласовано', 'Черновик', 'Действует']
        documents = [document(i, Менеджер_Key=guid(90), Статус=status, Posted=status in ('Черновик', 'НеСогласовано'),
                              ТТК_СрокПодачи='2026-11-17T13:14:15', СрокДействия='2026-12-31T00:00:00',
                              СрокВыполнения='2026-11-30T12:00:00', ДатаФактическогоВыполнения='2026-11-16T09:00:00')
                     for i, status in enumerate(statuses, 1)]
        documents[1].update(ТТК_СрокПодачи='0001-01-01T00:00:00', СрокПодачи='2026-11-18T15:00:00')
        documents[2]['ТТК_СрокПодачи'] = '2026-11-17T00:00:00'
        documents[4]['Date'] = documents[5]['Date'] = '2020-01-01T12:00:00'
        source = Source({entity: fields}, {entity: documents})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        rows = {row['ref']: row for row in result['rows']}
        self.assertEqual(len(rows), 5)
        self.assertEqual({row['status'] for row in rows.values() if onec_workload.document_closed(row)}, {'Действует', 'Отменено'})
        self.assertEqual(rows[guid(1)]['deadline'], '2026-11-17T13:14:15')
        self.assertEqual(rows[guid(1)]['deadline_source'], 'ТТК_СрокПодачи')
        self.assertIsNone(rows[guid(2)]['deadline'])
        self.assertIsNone(rows[guid(2)]['deadline_source'])
        self.assertEqual(rows[guid(3)]['deadline'], '2026-11-17T23:59:59')
        self.assertTrue(all(row['closed_at'] is None for row in rows.values()))
        self.assertIn(guid(5), rows)
        self.assertNotIn(guid(6), rows)
        query = next(params for name, params in source.calls if name == entity)
        self.assertNotIn('Posted eq', query['$filter'])
        self.assertIn('Статус', query['$select'])
        self.assertNotIn('ДатаФактическогоВыполнения', query['$select'])
        self.assertNotIn('СрокВыполнения', query['$select'])

    def test_payment_requests_close_on_rejection_or_last_linked_payment(self):
        request = 'Document_ЗаявкаНаРасходованиеДенежныхСредств'
        payment = 'Document_СписаниеБезналичныхДенежныхСредств'
        link = 'ЗаявкаНаРасходованиеДенежныхСредств'
        schemas = {
            request: {**COMMON, 'Ответственный_Key': 'Edm.Guid', 'Статус': 'Edm.String',
                      'ДатаПлатежа': 'Edm.DateTime', 'СрокВыполнения': 'Edm.DateTime',
                      'ДатаФактическогоВыполнения': 'Edm.DateTime'},
            payment: {**COMMON, link: 'Edm.String', link + '_Type': 'Edm.String',
                      'РасшифровкаПлатежа': 'Collection(PaymentRow)'},
        }
        requests = [document(i, Ответственный_Key=guid(90), Posted=True, Статус='КОплате',
                             ДатаПлатежа='2026-11-16T00:00:00', СрокВыполнения='2026-11-30T12:00:00',
                             ДатаФактическогоВыполнения='2026-11-01T10:00:00') for i in range(1, 11)]
        requests[1]['Posted'] = False
        requests[2]['Статус'] = 'Отклонена'
        requests[3].update(Статус='НеСогласована', Закрыта=True)
        requests[7]['Date'] = '2020-01-01T12:00:00'
        requests[8]['Date'] = '2020-01-01T12:00:00'
        requests[9]['Статус'] = 'Отклонена'

        def reference(ref, typ=request):
            return {link: guid(ref), link + '_Type': 'StandardODATA.' + typ}

        payments = [
            document(101, **reference(2), Date='2026-11-17T09:15:12', Posted=False),
            document(102, РасшифровкаПлатежа=[reference(5), reference(5)], Date='2026-11-18T11:12:13'),
            document(103, РасшифровкаПлатежа=[reference(5)], Date='2026-11-16T15:14:13'),
            document(104, **reference(6, 'Document_ЗаказПоставщику')),
            document(105, **reference(7), DeletionMark=True),
            document(106, **reference(9), Date='2020-01-02T12:00:00'),
            document(107, РасшифровкаПлатежа=[reference(10)], Date='2026-11-16T16:01:02'),
        ]
        for payment_row in payments:
            payment_row.setdefault('РасшифровкаПлатежа', [])
        source = Source(schemas, {request: requests, payment: payments}, next_links=True)
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        rows = {row['ref']: row for row in result['rows'] if row['entity'] == request}
        self.assertEqual(len(rows), 9)
        for ref in (1, 4, 6, 7, 8):
            with self.subTest(open_request=ref):
                self.assertFalse(onec_workload.document_closed(rows[guid(ref)]))
                self.assertIsNone(rows[guid(ref)]['closed_at'])
                self.assertEqual(rows[guid(ref)]['deadline'], '2026-11-16T23:59:59')
                self.assertEqual(rows[guid(ref)]['deadline_source'], 'ДатаПлатежа')
        self.assertEqual(rows[guid(2)]['closed_at'], '2026-11-17T09:15:12')
        self.assertEqual(rows[guid(5)]['closed_at'], '2026-11-18T11:12:13')
        self.assertTrue(onec_workload.document_closed(rows[guid(3)]))
        self.assertIsNone(rows[guid(3)]['closed_at'])
        self.assertEqual(rows[guid(10)]['closed_at'], '2026-11-16T16:01:02')
        self.assertNotIn(guid(9), rows)
        query = next(params for entity, params in source.calls if entity == request)
        self.assertNotIn('Posted eq', query['$filter'])
        self.assertNotIn('СрокВыполнения', query['$select'])
        self.assertNotIn('ДатаФактическогоВыполнения', query['$select'])

    def test_payment_lookup_failure_preserves_previous_request_snapshot(self):
        request = 'Document_ЗаявкаНаРасходованиеДенежныхСредств'
        payment = 'Document_СписаниеБезналичныхДенежныхСредств'
        fields = {**COMMON, 'ЗаявкаНаРасходованиеДенежныхСредств': 'Edm.String',
                  'ЗаявкаНаРасходованиеДенежныхСредств_Type': 'Edm.String',
                  'РасшифровкаПлатежа': 'Collection(PaymentRow)'}
        source = Source({request: {**COMMON, 'Ответственный_Key': 'Edm.Guid', 'Статус': 'Edm.String',
                                  'ДатаПлатежа': 'Edm.DateTime'}, payment: fields},
                        {request: [document(1, Ответственный_Key=guid(90), Posted=True, Статус='КОплате')]})
        for failure in ('unavailable', 'missing_schema'):
            with self.subTest(failure=failure):
                if failure == 'unavailable':
                    source.failure = payment
                else:
                    source.failure = None
                    del source.schemas[payment]
                result = onec_workload.fetch(source, CONFIG, SINCE)
                self.assertIn(request, result['errors'])
                self.assertNotIn(request, result['entities'])
                self.assertFalse(any(row['entity'] == request for row in result['rows']))

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

    def test_other_instant_document_types_complete_at_document_date(self):
        entities = [
            'Document_ПоступлениеБезналичныхДенежныхСредств',
            'Document_РеализацияТоваровУслуг',
            'Document_ОжидаемоеПоступлениеДенежныхСредств',
            'Document_СписаниеБезналичныхДенежныхСредств',
            'Document_СчетНаОплатуКлиенту',
            'Document_ДоверенностьВыданная',
        ]
        fields = {**COMMON, "Ответственный_Key": "Edm.Guid", "СрокВыполнения": "Edm.DateTime"}
        source = Source({entity: fields for entity in entities}, {entity: [
            document(i, Ответственный_Key=guid(90), Posted=False, Date="2026-11-16T14:58:11",
                     СрокВыполнения="2026-11-01T12:00:00")] for i, entity in enumerate(entities, 1)})
        result = onec_workload.fetch(source, CONFIG, SINCE)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['rows']), len(entities))
        for row in result['rows']:
            with self.subTest(entity=row['entity']):
                self.assertTrue(onec_workload.document_closed(row))
                self.assertEqual(row['closed_at'], row['date'])
                self.assertIsNone(row['deadline'])

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
        states = [("Черновик", True), ("Подготавливается", True), ("Действует", True),
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
