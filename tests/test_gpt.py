import copy
import json
import unittest
from unittest import mock
from app.common import connect,load_rules
from app import gpt_privacy,gpt_assistant
from tests.test_engine import CONFIG

class PrivacyTest(unittest.TestCase):
    def test_masks_names_company_document_url_and_requisites_but_not_financial_values(self):
        p=gpt_privacy.PrivacyContext()
        p.register("СОТРУДНИК","Анна Мерзлякова")
        p.register("КОМПАНИЯ","ООО ТрансТехКом")
        text="Анна Мерзлякова ООО ТрансТехКом 00ЦБ-000078 05.11.2026 257020,16 руб. +7 999 123-45-67 https://crm.example.ru/company/42"
        out=p.mask(text)
        for raw in ("Мерзлякова","ТрансТехКом","00ЦБ-000078","999 123","crm.example.ru"):
            self.assertNotIn(raw,out)
        self.assertIn("05.11.2026",out);self.assertIn("257020,16",out)
        self.assertIn("Анна Мерзлякова",p.restore(out))
        restored=gpt_privacy.PrivacyContext(p.dump())
        self.assertEqual(restored.mask("Анна Мерзлякова"),p.mask("Анна Мерзлякова"))

    def test_secrets_are_removed_not_recoverable(self):
        p=gpt_privacy.PrivacyContext()
        p.add_secret("MY_PRIVATE_PASSWORD")
        out=p.mask("Пароль MY_PRIVATE_PASSWORD https://crm.example.ru/rest/1/abcdefSECRET123456/")
        self.assertNotIn("MY_PRIVATE_PASSWORD",out)
        self.assertNotIn("abcdefSECRET",p.restore(out))
        self.assertNotIn("MY_PRIVATE_PASSWORD",p.restore(out))

    def test_context_requisites_are_masked_without_hiding_unrelated_amounts(self):
        p=gpt_privacy.PrivacyContext()
        out=p.mask("БИК 044525225, КПП 770101001, ИНН 7701234567; р/с 4070 2810 0000 0000 1234. Сумма 257020,16 на 05.11.2026")
        for value in ("044525225","770101001","7701234567","4070 2810"):
            self.assertNotIn(value,out)
        self.assertIn("257020,16",out);self.assertIn("05.11.2026",out)

class GPTTest(unittest.TestCase):
    def setUp(self):
        self.con=connect(":memory:");self.addCleanup(self.con.close)
        self.config=copy.deepcopy(CONFIG)
        self.config["employees"][2]["name"]="Анна Мерзлякова"
        self.config["bitrix24"]={"webhook":"https://crm.example.test/rest/1/SECRET_HOOK/"}
        self.service=gpt_assistant.GPTAssistant(self.con,self.config,load_rules())

    def test_chat_ownership(self):
        chat=self.service.create_chat("boss")
        with self.assertRaises(ValueError):self.service.chat("other",chat["id"])

    def test_tool_loop_is_masked_and_restores_sources_locally(self):
        chat=self.service.create_chat("boss")
        step={"kind":"tool","tool":"employees","query":"","employee":"","month":"2026-11","answer":"","sources":[]}
        calls=[]
        def model(prompt,config,**kwargs):
            calls.append(prompt)
            if len(calls)==1:return {"result":step}
            return {"result":{**step,"kind":"answer","answer":"Сотрудник [СОТРУДНИК_3] найден.","sources":["source_1"]}}
        with mock.patch.object(gpt_assistant.call_analysis,"run_codex",side_effect=model):
            job=self.service.submit("boss",chat["id"],"Найди Анна Мерзлякова",launch=False)
            self.service.work(chat["id"],job["id"])
        result=self.service.chat("boss",chat["id"])
        self.assertEqual(result["messages"][-1]["status"],"completed")
        for prompt in calls:
            self.assertNotIn("Мерзлякова",prompt)
            self.assertNotIn("SECRET_HOOK",prompt)
        self.assertTrue(result["messages"][-1]["sources"])

    def test_unknown_tool_cannot_execute_sql(self):
        chat=self.service.create_chat("boss")
        with self.assertRaises(ValueError):
            self.service.tools("sql","DROP TABLE employees","","2026-11",gpt_privacy.PrivacyContext())
        self.assertIsNotNone(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='employees'").fetchone())

    def test_kpi_and_finance_tools_keep_amounts_and_strip_structured_requisites(self):
        p=gpt_privacy.PrivacyContext();self.service.data.seed(p)
        safe=self.service.data.protect({"account":40702810000000001234,"amount":257020.16,"date":"2026-11-05","id":42},p)
        self.assertNotIn("40702810000000001234",json.dumps(safe))
        self.assertEqual(safe["amount"],257020.16)
        self.assertEqual(safe["date"],"2026-11-05")
        kpi=self.service.tools("kpi","","Анна Мерзлякова","2026-11",p)
        self.assertIn("items",kpi)
        self.assertNotIn("Мерзлякова",json.dumps(kpi,ensure_ascii=False))
        self.assertIn("tiles",self.service.tools("money","","","2026-11",p))

    def test_cyrillic_search_does_not_leak_company_or_document_ids(self):
        self.con.execute("INSERT INTO clients(id,emp,name) VALUES ('co123','tender','СИБАЦ, АО')");self.con.commit()
        p=gpt_privacy.PrivacyContext();self.service.data.seed(p)
        rows=self.service.tools("search","сибац","","2026-11",p)
        self.assertTrue(rows["rows"])
        text=json.dumps(rows,ensure_ascii=False)
        self.assertNotIn("СИБАЦ",text);self.assertNotIn("co123",text)
        self.assertNotIn("СИБАЦ",p.mask("Есть вопрос к СИБАЦ"))

    def test_worker_does_not_hold_database_lock_during_model_call_and_can_cancel(self):
        import threading,time
        from app.common import db_lock
        ready=threading.Event();release=threading.Event()
        answer={"result":{"kind":"answer","tool":"","query":"","employee":"","month":"","answer":"Готово","sources":[]}}
        def model(*args,**kwargs):ready.set();release.wait(2);return answer
        chat=self.service.create_chat("boss")
        try:
            with mock.patch.object(gpt_assistant.call_analysis,"run_codex",side_effect=model):
                self.service.submit("boss",chat["id"],"Привет")
                self.assertTrue(ready.wait(2))
                acquired=db_lock().acquire(blocking=False)
                self.assertTrue(acquired)
                if acquired:db_lock().release()
                self.service.cancel("boss",chat["id"])
                release.set()
                for _ in range(200):
                    if not self.service.active:break
                    time.sleep(.01)
        finally:
            release.set()
        self.assertEqual(self.service.chat("boss",chat["id"])["messages"][-1]["status"],"cancelled")

    def test_restart_marks_pending_turn_failed_and_chat_remains_private(self):
        chat=self.service.create_chat("boss")
        self.service.submit("boss",chat["id"],"Вопрос",launch=False)
        restarted=gpt_assistant.GPTAssistant(self.con,self.config,load_rules())
        self.assertEqual(restarted.chat("boss",chat["id"])["messages"][-1]["status"],"error")
        with self.assertRaises(ValueError):restarted.preview("other",chat["id"],1)

    def test_numeric_document_fields_are_masked_independently_of_format(self):
        p=gpt_privacy.PrivacyContext()
        safe=self.service.data.protect({"doc":"000123","customer_order":"456789","basis":"987654","amount":257020.16,"dt":"2026-11-05"},p)
        self.assertNotIn("000123",json.dumps(safe))
        self.assertNotIn("456789",json.dumps(safe))
        self.assertNotIn("987654",json.dumps(safe))
        self.assertEqual(safe["amount"],257020.16)

    def test_raw_question_requisites_are_absent_from_actual_prompt(self):
        chat=self.service.create_chat("boss");captured=[]
        response={"kind":"answer","tool":"","query":"","employee":"","month":"","answer":"Нужны данные","sources":[]}
        def model(prompt,*args,**kwargs):captured.append(prompt);return {"result":response}
        with mock.patch.object(gpt_assistant.call_analysis,"run_codex",side_effect=model):
            mid=self.service.submit("boss",chat["id"],"Заказ №000123; БИК 044525225, КПП 770101001. Сумма 257020,16 на 05.11.2026",launch=False)
            self.service.work(chat["id"],mid["id"])
        for value in ("000123","044525225","770101001"):
            self.assertNotIn(value,captured[0])
        self.assertIn("257020,16",captured[0]);self.assertIn("05.11.2026",captured[0])

    def test_employee_surname_alias_can_be_matched_to_full_name(self):
        p=gpt_privacy.PrivacyContext();self.service.data.seed(p)
        rows=self.service.tools("employees","","","2026-11",p)["employees"]
        full=p.mask("Анна Мерзлякова");surname=p.mask("Мерзлякова")
        row=next(r for r in rows if r["employee"]==full)
        self.assertIn(surname,row["aliases"])
