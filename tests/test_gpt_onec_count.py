import copy
import json
import unittest
from unittest.mock import Mock

from app.gpt_access import Access
from app.gpt_live import LiveSources
from app.gpt_privacy import PrivacyContext
from tests.test_engine import CONFIG


class OneCCountTest(unittest.TestCase):
    def setUp(self):
        self.live = LiveSources({})
        self.fields = {"Ref_Key":"Edm.Guid", "Number":"Edm.String", "Date":"Edm.DateTime", "Автор":"Edm.String", "Автор_Type":"Edm.String", "DeletionMark":"Edm.Boolean"}
        self.metadata = {"Document_ЗаказКлиента":{"fields":self.fields,"references":{}}}
        self.live.metadata = Mock(return_value=self.metadata)
        self.live.onec = Mock()
        self.privacy = PrivacyContext(documents_only=True)
        self.author = "752c1b06-2bc2-11ef-8998-9c6b003f96c6"
        self.rows = [{"Ref_Key":"one","Автор":self.author,"DeletionMark":False}, {"Ref_Key":"two","Автор":self.author,"DeletionMark":True}, {"Ref_Key":"three","Автор":"other","DeletionMark":False}]

    def count(self, **params):
        return self.live.execute("onec_count", json.dumps({"entity":"Document_ЗаказКлиента",**params}), self.privacy)

    def test_exact_count_reads_all_pages_and_reports_deletion_basis(self):
        self.live.onec._odata.side_effect = [self.rows[:2],self.rows[2:],[],self.rows[:2],self.rows[2:],[]]
        result = self.count(field="Автор", text=self.author, match="equals")
        self.assertEqual((result["total"],result["matched"]), (3,2))
        self.assertAlmostEqual(result["percent"], 200 / 3)
        self.assertEqual(result["excluding_marked_for_deletion"], {"total":2,"matched":1,"percent":50})
        self.assertEqual([c.args[1]["$skip"] for c in self.live.onec._odata.call_args_list], [0,2,3,0,2,3])
        self.assertTrue(all("$filter" not in c.args[1] for c in self.live.onec._odata.call_args_list))

    def test_empty_denominator_is_unknown_and_count_all_works(self):
        self.live.onec._odata.return_value = []
        self.assertIsNone(self.count()["percent"])
        self.live.onec._odata.side_effect = [self.rows,[],self.rows,[]]
        self.assertEqual(self.count()["matched"],3)

    def test_source_changes_during_pagination_are_not_reported_as_exact(self):
        self.live.onec._odata.side_effect = [self.rows[:2],[],self.rows[1:],[]]
        with self.assertRaisesRegex(ValueError,"изменились"):
            self.count(field="Автор",text=self.author)

    def test_composite_search_uses_local_reference_comparison(self):
        self.live.onec._odata.side_effect = [self.rows,[]]
        result = self.live.execute("onec_search", json.dumps({"entity":"Document_ЗаказКлиента","field":"Автор","text":self.author,"match":"equals","fields":["Автор"]}), self.privacy)
        self.assertEqual(len(result["rows"]),2)
        self.assertTrue(all("$filter" not in c.args[1] for c in self.live.onec._odata.call_args_list))

    def test_invalid_composite_text_never_reaches_1c(self):
        for tool in ("onec_search","onec_count"):
            with self.assertRaises(ValueError):
                self.live.execute(tool,json.dumps({"entity":"Document_ЗаказКлиента","field":"Автор","text":"Шарлаимова","match":"contains"}),self.privacy)
        self.live.onec._odata.assert_not_called()

    def test_repeated_pages_and_oversized_selection_are_not_reported_as_complete(self):
        self.live.onec._odata.return_value = self.rows
        with self.assertRaisesRegex(ValueError,"повторяющиеся"):
            self.count()
        self.live.onec._odata.side_effect = [[{"Ref_Key":str(i)} for i in range(10001)]]
        with self.assertRaisesRegex(ValueError,"10 000"):
            self.count()

    def test_staff_count_preserves_owner_filter_and_forbids_author_field(self):
        access = Access(copy.deepcopy(CONFIG), "pm")
        self.live.access = access
        self.live.onec._odata.return_value = []
        self.count(month="2026-11")
        expression = self.live.onec._odata.call_args.args[1]["$filter"]
        self.assertIn("Менеджер", expression)
        self.assertIn("Date ge datetime'2026-11-01", expression)
        self.live.onec._odata.reset_mock()
        with self.assertRaises(ValueError):
            self.count(field="Автор",text=self.author)
        self.live.onec._odata.assert_not_called()
