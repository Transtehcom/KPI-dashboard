import unittest

from app.bitrix24 import Bitrix24, B24Error
from app.client_qualification import qualify_client


class ClientQualificationTest(unittest.TestCase):
    def test_need_must_describe_a_need_instead_of_a_flag_or_placeholder(self):
        for need in ("Нет", "Не выявлена", "нет.", "Потребность не выявлена", "пока нет", "-", ".", "+", "!?", "123", "Да", True):
            with self.subTest(need=need):
                self.assertEqual(qualify_client(need,["Выслать подбор оборудования"],["2026-11-10"])["need"],0)
        self.assertEqual(qualify_client("Требуются насосы для нового объекта",["Отправить КП"],["2026-11-10"])["need"],1)

    def test_each_of_four_strategy_items_needs_a_date(self):
        strategy = ["Уточнить объём", "Отправить КП", "Обсудить условия", "Согласовать договор"]
        for dates, expected in ((["2026-11-10"]*4,1), (["2026-11-10"]*3,0),
                                (["2026-11-10"]*5,0), ([],0), (["2026-11-10"]*3+["2026-02-31"],0)):
            with self.subTest(dates=dates):
                result = qualify_client("Требуется оборудование",strategy,dates)
                self.assertEqual(result["strategy"],expected)
                self.assertEqual(result["strategy_count"],4)

    def test_signs_are_not_strategy_items(self):
        for strategy in ("-", ".", "...", ["!"], ["Отправить КП", "."], ["+", "-"]):
            with self.subTest(strategy=strategy):
                self.assertEqual(qualify_client("Нужны насосы",strategy,["2026-11-10"])["strategy"],0)
        self.assertEqual(qualify_client("Нужны насосы","Отправить КП","10.11.2026")["strategy"],1)

    def test_fetch_discovers_new_date_field_and_requests_it(self):
        class Portal(Bitrix24):
            def call(self,method,params=None):
                return {"result":{
                    "UF_NEED":{"formLabel":"Потребность выявлена"},
                    "UF_STRATEGY":{"formLabel":"Стратегия"},
                    "UF_NEW_DATES":{"formLabel":"Дата реализации стратегии"}}}
            def list_all(self,method,params,key=None):
                self.selected = params["select"]
                return [{"ID":"42","ASSIGNED_BY_ID":"7","TITLE":"Клиент", "UF_NEED":"Нужны насосы",
                         "UF_STRATEGY":["Отправить КП","Обсудить поставку"],"UF_NEW_DATES":["2026-11-10"]}]
        portal = Portal({"webhook":"https://portal.example.test/rest/1/test/","fields":{}})
        row = portal.fetch_clients({7:"lead"})[0]
        self.assertIn("UF_NEW_DATES",portal.selected)
        self.assertEqual((row["need"],row["strategy"],row["strategy_count"],row["strategy_date_count"]),(1,0,2,1))
        self.assertEqual(row["emp"],"lead")

    def test_missing_date_field_does_not_silently_activate_companies(self):
        class Portal(Bitrix24):
            def call(self,method,params=None):
                return {"result":{"UF_NEED":{"formLabel":"Потребность выявлена"},
                                  "UF_STRATEGY":{"formLabel":"Стратегия"}}}
        with self.assertRaises(B24Error):
            Portal({"webhook":"https://portal.example.test/rest/1/test/"}).fetch_clients({7:"lead"})
