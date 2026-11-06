import json
import threading
import unittest
from unittest import mock
from app.common import connect,load_rules,db_lock
from app.gpt_tools import DataTools
from app.gpt_privacy import PrivacyContext

META=b'''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA"><EntityType Name="Document_Order"><Key><PropertyRef Name="Ref_Key"/></Key><Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Number" Type="Edm.String"/><Property Name="Date" Type="Edm.DateTime"/><Property Name="Amount" Type="Edm.Decimal"/></EntityType><EntityContainer Name="Container"><EntitySet Name="Document_Order" EntityType="StandardODATA.Document_Order"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'''

class LiveTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(":memory:");self.addCleanup(self.con.close)
        self.tools=DataTools(self.con,{"employees":[],"bitrix24":{"webhook":"https://crm.example.test/rest/1/secret/"},"onec":{"base_url":"https://onec.example.test/"}},load_rules())
        self.p=PrivacyContext()

    def test_b24_direct_company_search_masks_new_company_and_returns_fresh_source(self):
        self.tools.live.b24=mock.Mock()
        self.tools.live.b24.call.return_value={"result":[{"ID":"123","TITLE":"Совсем Новый Клиент","PHONE":[{"VALUE":"89991112233"}]}],"total":1}
        out=self.tools.execute("b24_search",json.dumps({"entity":"company","text":"Новый"}),"","2026-11",self.p)
        self.assertNotIn("Совсем Новый Клиент",json.dumps(out,ensure_ascii=False))
        self.assertNotIn("89991112233",json.dumps(out))
        self.assertTrue(out["fetched_at"])
        self.assertEqual(self.tools.live.b24.call.call_args.args[0],"crm.company.list")
        self.assertTrue(self.p.sources)

    def test_live_network_call_is_outside_database_lock(self):
        checked=[]
        def fetch(*args,**kwargs):
            def probe():
                acquired=db_lock().acquire(blocking=False);checked.append(acquired)
                if acquired:db_lock().release()
            thread=threading.Thread(target=probe);thread.start();thread.join(1)
            return {"result":[],"total":0}
        self.tools.live.b24=mock.Mock();self.tools.live.b24.call.side_effect=fetch
        self.tools.execute("b24_search",'{"entity":"company","text":"test"}',"","2026-11",self.p)
        self.assertEqual(checked,[True])

    def test_1c_metadata_and_parameterized_search_escape_odata_literals(self):
        self.tools.live.onec=mock.Mock();self.tools.live.onec.metadata.return_value=META
        self.tools.live.onec._odata.return_value=[]
        types=self.tools.execute("onec_entities","Order","","2026-11",self.p)
        handle=types["entities"][0]["entity"]
        self.tools.execute("onec_search",json.dumps({"entity":handle,"field":"Number","text":"x' or true or 'y"}),"","2026-11",self.p)
        entity,params=self.tools.live.onec._odata.call_args.args
        self.assertEqual(entity,"Document_Order")
        self.assertIn("x'' or true or ''y",params["$filter"])
        with self.assertRaises(ValueError):
            self.tools.execute("onec_search",'{"entity":"../admin","text":"x"}',"","2026-11",self.p)
        with self.assertRaises(ValueError):
            self.tools.execute("b24_search",'{"entity":"crm.company.delete","id":123}',"","2026-11",self.p)

    def test_live_errors_do_not_fallback_to_local_rows_or_expose_credentials(self):
        self.tools.live.b24=mock.Mock();self.tools.live.b24.call.side_effect=RuntimeError("HTTP 401 secret password https://crm.example.test/rest/1/secret/")
        with self.assertRaisesRegex(ValueError,"401") as caught:
            self.tools.execute("b24_search",'{"entity":"company","text":"test"}',"","2026-11",self.p)
        self.assertNotIn("secret",str(caught.exception))

    def test_1c_guid_reverse_lookup_and_numeric_threshold(self):
        self.tools.live.onec=mock.Mock();self.tools.live.onec.metadata.return_value=META;self.tools.live.onec._odata.return_value=[]
        self.tools.execute("onec_search",json.dumps({"entity":"Document_Order","field":"Amount","text":"500000","match":"ge"}),"","2026-11",self.p)
        self.assertEqual(self.tools.live.onec._odata.call_args.args[1]["$filter"],"Amount ge 500000")
        self.tools.execute("onec_search",json.dumps({"entity":"Document_Order","field":"Ref_Key","text":"12345678-1234-1234-1234-123456789abc","match":"equals"}),"","2026-11",self.p)
        self.assertIn("guid'12345678-1234-1234-1234-123456789abc'",self.tools.live.onec._odata.call_args.args[1]["$filter"])
        with self.assertRaises(ValueError):
            self.tools.execute("onec_search",json.dumps({"entity":"Document_Order","field":"Amount","text":"NaN","match":"ge"}),"","2026-11",self.p)

    def test_b24_custom_requisites_and_url_card_are_masked(self):
        card={"ID":"123","TITLE":"Новый Секретный Клиент","UF_CUSTOM":"770101001","UF_MONEY":"1000000000.00","COMPANY_ID":"55"}
        def call(method,params=None):
            if method.endswith(".fields"):return {"result":{"UF_CUSTOM":{"type":"string","title":"КПП"},"UF_MONEY":{"type":"money","title":"Бюджет"}}}
            self.assertEqual(method,"crm.company.get")
            self.assertEqual(params["id"],"123")
            return {"result":card}
        self.tools.live.b24=mock.Mock();self.tools.live.b24.call.side_effect=call
        out=self.tools.execute("b24_card",json.dumps({"id":"https://crm.example.test/crm/company/details/123/"}),"","2026-11",self.p)
        text=json.dumps(out,ensure_ascii=False)
        self.assertNotIn("770101001",text);self.assertNotIn("Новый Секретный Клиент",text)
        self.assertIn("1000000000.00",text)
        self.assertEqual(out["rows"][0]["references"][0]["entity"],"company")

    def test_numeric_exponent_is_rejected_before_decimal_formatting(self):
        self.tools.live.onec=mock.Mock();self.tools.live.onec.metadata.return_value=META
        with mock.patch("app.gpt_live.Decimal",side_effect=AssertionError("must reject exponent first")) as decimal:
            with self.assertRaises(ValueError):
                self.tools.execute("onec_search",json.dumps({"entity":"Document_Order","field":"Amount","text":"1e-100000000","match":"ge"}),"","2026-11",self.p)
        decimal.assert_not_called()
        self.tools.live.onec._odata.assert_not_called()

    def test_fresh_structured_party_names_are_always_masked(self):
        for entity,field in (("lead","COMPANY_TITLE"),("requisite","RQ_COMPANY_NAME"),("requisite","RQ_COMPANY_FULL_NAME"),("requisite","RQ_FIRST_NAME"),("requisite","RQ_LAST_NAME"),("Catalog_Контрагенты","ПолноеНаименование")):
            with self.subTest(field=field):
                out=self.tools.live.value(field,"acme-private-482",self.p,entity)
                self.assertNotIn("acme-private-482",json.dumps(out))
        self.assertEqual(self.tools.live.value("OPPORTUNITY","1000000000.00",self.p,"deal"),"1000000000.00")
        self.assertEqual(self.tools.live.value("ДатаСчета","2026-11-06T00:00:00",self.p,"Document_Order","Edm.DateTime"),"2026-11-06T00:00:00")

    def test_live_json_transport_uses_byte_limit_and_preserves_auth(self):
        from app.onec import OneC
        client=OneC({"base_url":"https://onec.example/","user":"test","password":"secret","response_limit_bytes":4})
        response=mock.MagicMock();response.__enter__.return_value=response;response.read.return_value=b'12345'
        with mock.patch("app.onec.urllib.request.urlopen",return_value=response) as request:
            with self.assertRaises(ValueError):client._get('https://onec.example/odata/standard.odata/X')
        response.read.assert_called_once_with(5)
        self.assertTrue(request.call_args.args[0].has_header('Authorization'))

    def test_chat_normal_entrypoint_uses_live_source_and_masks_before_next_model_call(self):
        from app.gpt_assistant import GPTAssistant
        service=GPTAssistant(self.con,self.tools.config,load_rules())
        service.data.live.b24=mock.Mock()
        service.data.live.b24.call.return_value={"result":[{"ID":"888","TITLE":"acme-private-482","PHONE":[{"VALUE":"89991112233"}]}],"total":1}
        chat=service.create_chat("boss");prompts=[]
        base={"kind":"tool","tool":"b24_search","query":json.dumps({"entity":"company","text":"test"}),"employee":"","month":"2026-11","answer":"","sources":[]}
        def model(prompt,*args,**kwargs):
            prompts.append(prompt)
            if len(prompts)==1:return {"result":base}
            return {"result":{**base,"kind":"answer","answer":"Карточка найдена","sources":["source_2"]}}
        with mock.patch("app.gpt_assistant.call_analysis.run_codex",side_effect=model):
            message=service.submit("boss",chat["id"],"Проверь компанию прямо в Б24",launch=False)
            service.work(chat["id"],message["id"])
        result=service.chat("boss",chat["id"])["messages"][-1]
        self.assertEqual(result["status"],"completed")
        self.assertIn("прямой запрос",result["sources"][0]["system"])
        self.assertNotIn("acme-private-482",prompts[1]);self.assertNotIn("89991112233",prompts[1])
