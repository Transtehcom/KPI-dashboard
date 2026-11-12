import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch

from app.common import connect, load_rules, replace_rows
from app.engine import Engine
from app.supply_documents import CONTRACT_NAME, fetch, report
from test_engine import CONFIG


class SupplyDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)

    def test_contract_name_variants(self):
        for name in ("ДОГОВОР поставки.pdf", "скан_договора.pdf", "дог. 123.pdf", "Дог.pdf",
                     "дог 123.pdf", "скан_дог_123.PDF", "Допсоглашение к договору.pdf"):
            self.assertIsNotNone(CONTRACT_NAME.search(name), name)
        for name in ("счет.pdf", "спецификация.pdf", "подогрев.pdf", "dog.pdf", "догма.pdf", "бульдог.pdf"):
            self.assertIsNone(CONTRACT_NAME.search(name), name)

    def test_order_and_contract_files_and_request_owner_determine_qualification(self):
        calls = []
        class Source:
            def _odata(self, entity, params):
                calls.append((entity,params))
                rows = {
                    "Document_ЗаказКлиента": [{"Ref_Key":"customer","ДокументОснование":"base"}],
                    "Document_ТТК_ЗаявкаВОтделСнабжения": [
                        {"Ref_Key":"request","Number":"REQ-1","ДокументОснования":"base",
                         "СтатусЗаявки":"Отработана","ОтветственныйСнабжение":{"Description":"Supply owner"}}],
                    "Document_ЗаказПоставщику": [
                        {"Ref_Key":"one","Number":"001","Date":"2026-11-02","Договор_Key":"contract",
                         "ТТК_ЗаказКлиента_Key":"customer","Автор_Key":"author"},
                        {"Ref_Key":"two","Number":"002","Date":"2026-11-03","Договор_Key":"other",
                         "ДокументОснование":"request"},
                        {"Ref_Key":"three","Number":"003","Date":"2026-11-03","Договор_Key":"third",
                         "ДокументОснование":"request"},
                        {"Ref_Key":"four","Number":"004","Date":"2026-11-03","Договор_Key":"fourth",
                         "ДокументОснование":"request"}],
                    "Catalog_ДоговорыКонтрагентов": [{"Ref_Key":"contract","Description":"Договор поставки"}],
                    "Catalog_ДоговорыКонтрагентовПрисоединенныеФайлы": [
                        {"Ref_Key":"f1","ВладелецФайла_Key":"contract","Description":"договор","Расширение":"pdf"},
                        {"Ref_Key":"f2","ВладелецФайла_Key":"contract","Description":"копия договора","Расширение":"pdf"},
                        {"Ref_Key":"f3","ВладелецФайла_Key":"other","Description":"договор","DeletionMark":True},
                        {"Ref_Key":"folder","ВладелецФайла_Key":"other","Description":"договора","IsFolder":True},
                        {"Ref_Key":"f4","ВладелецФайла_Key":"other","Description":"счет"}],
                    "Catalog_Файлы": [
                        {"Ref_Key":f"unrelated-{i}","ВладелецФайла":"two",
                         "ВладелецФайла_Type":"StandardODATA.BusinessProcess_Задание","Description":"договор"}
                        for i in range(200)] + [
                        {"Ref_Key":"order-file","ВладелецФайла":"two",
                         "ВладелецФайла_Type":"StandardODATA.Document_ЗаказПоставщику","Description":"ДОГ 123","Расширение":"pdf"},
                        {"Ref_Key":"order-file-2","ВладелецФайла":"two",
                         "ВладелецФайла_Type":"StandardODATA.Document_ЗаказПоставщику","Description":"договор.pdf","Расширение":"pdf"},
                        {"Ref_Key":"deleted","ВладелецФайла":"three","DeletionMark":True,
                         "ВладелецФайла_Type":"StandardODATA.Document_ЗаказПоставщику","Description":"договор"},
                        {"Ref_Key":"unmatched","ВладелецФайла":"three",
                         "ВладелецФайла_Type":"StandardODATA.Document_ЗаказПоставщику","Description":"счет"},
                        {"Ref_Key":"contract-file","ВладелецФайла":"fourth",
                         "ВладелецФайла_Type":"StandardODATA.Catalog_ДоговорыКонтрагентов","Description":"договора.pdf"}],
                }[entity]
                start = params.get("$skip", 0)
                return rows[start:start + params.get("$top", len(rows))]
        rows = fetch(Source(),{"author_owners":{"author":"Wrong owner"}},{"supply owner":"supply"},
                     dt.date(2026,11,1),dt.date(2026,11,30))
        self.assertEqual([r["emp"] for r in rows],["supply"] * 4)
        self.assertEqual([r["has_contract"] for r in rows],[1,1,0,1])
        self.assertEqual(len(json.loads(rows[0]["files"])),2)
        self.assertEqual(rows[1]["request_docs"],"REQ-1")
        self.assertEqual(json.loads(rows[1]["files"]),["ДОГ 123.pdf", "договор.pdf"])
        self.assertEqual(json.loads(rows[3]["files"]),["договора.pdf"])
        file_query = next(params for entity,params in calls if entity.endswith("ПрисоединенныеФайлы"))
        self.assertNotIn("Base64Data",file_query["$select"])

    def test_unknown_coverage_is_not_a_zero_score(self):
        result = report(self.con,"supply",dt.date(2026,11,1),dt.date(2026,11,30),dt.date(2026,11,5))
        self.assertFalse(result["known"])
        self.assertIsNone(result["percent"])

    def test_monthly_kpi_uses_order_fraction_and_readonly_details(self):
        config = copy.deepcopy(CONFIG)
        next(e for e in config["employees"] if e["login"]=="supply")["b24_user_id"] = 15
        replace_rows(self.con,"supply_contracts",[
            {"id":str(i),"emp":"supply","doc":f"ORD-{i}","dt":"2026-11-02","has_contract":i % 2,
             "files":json.dumps(["Договор.pdf"] if i % 2 else [])} for i in range(4)])
        for dataset in ("supply_contracts","task_efficiency:15"):
            self.con.execute("INSERT INTO dataset_state VALUES(?,'2026-11-01','2026-11-30','now')",(dataset,))
        # A stored manual grade does not replace the automatic order fraction.
        self.con.execute("INSERT INTO monthly_scores VALUES('supply','2026-11','docs',100,'','boss','now')")
        engine = Engine(self.con,load_rules(),config)
        with patch("app.engine.today",return_value=dt.date(2026,11,5)):
            item = next(i for i in engine.compute("supply","2026-11")["items"] if i["code"]=="exec_discipline")
        self.assertEqual(item["components"][0]["fact"],.5)
        self.assertEqual(item["pct"],.75)
        self.assertEqual(item["payout"],1125)
        self.assertNotIn("manual",item)
        self.assertTrue(item["entry"]["readonly"])
        self.assertEqual(len(item["entry"]["rows"]),4)
        self.assertEqual([x["value"] for x in item["extra"][:2]],[4,2])
        self.assertIn("Задача",item["details"]["columns"])
