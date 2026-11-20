import unittest
from unittest.mock import patch

from app import gpt_web
from app.gpt_privacy import PrivacyContext
from app.bitrix_bot import _answer_parts


class WebSearchTest(unittest.TestCase):
    def test_directory_requires_inn_and_email_and_keeps_internal_queries_internal(self):
        q = 'Найди в интернете 10 производителей упаковки с ИНН и почтой'
        self.assertEqual(gpt_web.directory_size(q), 10)
        self.assertTrue(gpt_web.public_only(q))
        for other in (q + ' и сравни с 1С', q + ' из CRM'):
            self.assertEqual(gpt_web.directory_size(other), 0)
            self.assertFalse(gpt_web.public_only(other))
        self.assertEqual(gpt_web.directory_size('Найди 10 компаний с ИНН'), 0)
        self.assertEqual(gpt_web.directory_size('Найди в интернете 10 потенциальных клиентов для поставки упаковки. Интересуют их ИНН, контакты для связи, почта, краткое описание'), 10)
        self.assertEqual(gpt_web.directory_size('Найди мне десять покупателей картона с ИНН и электронным адресом'), 10)
        self.assertEqual(gpt_web.directory_size('Найди мне десять наших клиентов с ИНН и почтой из 1С'), 0)

    def test_contact_rejects_unvisited_email_source_and_invalid_inn(self):
        url = 'https://example.org/details'
        row = dict(company='Тест', inn='7707049388', email='office@example.org',
                   inn_url=url, email_url=url, inn_quote='ИНН 7707049388', email_quote='office@example.org')
        for fields, ok in (({}, True), ({'inn':'7707049389'}, False),
                           ({'email_url':url+'/other'}, False), ({'email_quote':''}, False)):
            with self.subTest(fields=fields), patch.object(gpt_web,'public_run',return_value=({**row,**fields},{url},True)), \
                 patch.object(gpt_web,'_public_page_text',return_value='ИНН 7707049388 · office@example.org'):
                self.assertEqual(bool(gpt_web._company_contact('Тест', {}, 'test')),ok)
        with patch.object(gpt_web,'public_run',return_value=(row,{url},True)), \
             patch.object(gpt_web,'_public_page_text',return_value='Контакты 7707049388 other-office@example.org'):
            self.assertIsNone(gpt_web._company_contact('Тест', {}, 'test'))

    def test_directory_replaces_duplicates_and_missing_contacts(self):
        def lookup(name, *_):
            if name == 'Компания 2':
                return None
            return {'company':name,'inn':'0' if name in ('Компания 0','Компания 1') else name}
        names = ['Компания '+str(i) for i in range(8)]
        with patch.object(gpt_web, 'public_run', return_value=({'companies':names,'status':'ok'},set(),True)), \
             patch.object(gpt_web, '_company_contact', side_effect=lookup):
            summary, rows = gpt_web.directory('Подборка компаний',5,{},'test',PrivacyContext(),lambda _:None,lambda:None)
        self.assertEqual(len(set(r['inn'] for r in rows)),5)
        self.assertIn('5 из 5', summary)

    def test_telegram_directory_keeps_cards_and_source_urls_together(self):
        cards = [f'{i}. Компания\nИНН 123\ninfo@example.org\nhttps://example.org/{i}' for i in range(60)]
        parts = _answer_parts('\n\n'.join(cards), [{'url':'https://example.org/1'}], telegram=True)
        self.assertGreater(len(parts),1)
        self.assertEqual('\n\n'.join(parts), '\n\n'.join(cards))
        self.assertTrue(all(len(part) <= 2000 for part in parts))

    def test_markdown_table_becomes_readable_company_cards(self):
        text = "| Компания | ИНН | Email |\n|---|---|---|\n| **Тест** | 123 | info@example.org |"
        self.assertEqual(_answer_parts(text, [], telegram=True), ["Тест\nИНН: 123\nEmail: info@example.org"])

    def test_next_directory_excludes_prior_inns_even_under_another_brand(self):
        self.assertEqual(gpt_web.next_directory_size("Найди ещё 3 других компании"),3)
        self.assertEqual(gpt_web.next_directory_size("ещё 3"),3)
        self.assertEqual(gpt_web.next_directory_size("Дай 2 другие компании"),2)
        self.assertEqual(gpt_web.next_directory_size("найди десять других компаний"),10)
        self.assertEqual(gpt_web.next_directory_size("10"),0)
        with patch.object(gpt_web, 'public_run', return_value=({'companies':['Другой бренд'],'status':'ok'},set(),True)), \
             patch.object(gpt_web, '_company_contact', return_value={'inn':'123','company':'Другой бренд'}):
            _, rows = gpt_web.directory('Подборка компаний',3,{},'test',PrivacyContext(),lambda _:None,lambda:None,
                                        [{'company':'Бренд','inn':'123'}])
        self.assertEqual(rows, [])

    def test_personal_contacts_are_restorable_locally_and_block_public_search(self):
        privacy = PrivacyContext(documents_only=True)
        for value in ("contact@example.org", "+7 (999) 123-45-67", "123-456-789 00", "45 12 123456"):
            with self.subTest(value=value), patch.object(gpt_web.call_analysis, "run_codex") as run:
                masked = privacy.mask(value)
                self.assertNotIn(value, masked)
                self.assertEqual(privacy.restore(masked), value)
                for query in (value, masked):
                    with self.assertRaises(ValueError):
                        gpt_web.search(query, query, {}, "test", privacy)
                run.assert_not_called()
        self.assertEqual(privacy.mask_contacts("ООО Пример 5900000000"), "ООО Пример 5900000000")

    def test_only_evidenced_urls_are_links_in_both_bots(self):
        url = 'https://example.org/spec'
        def research(prompt, settings, **kwargs):
            settings.update(codex_web_used=True, codex_web_sources=[{'url':url}])
            return {'result':{'pages':[{'url':u,'title':'Стандарт','finding':'Факт'} for u in (url,url+'/extra')]}}
        privacy = PrivacyContext()
        with patch.object(gpt_web.call_analysis,'run_codex',side_effect=research):
            result = gpt_web.search('стандарт CSV','Найди стандарт CSV',{},'test',privacy)
        self.assertEqual(len(result['pages']),1)
        self.assertEqual(result['pages'][0]['url'], url)
        sources = list(privacy.sources.values())
        self.assertIn('[URL='+url+']',_answer_parts('Ответ',sources)[0])
        self.assertIn('\n'+url,_answer_parts('Ответ',sources,telegram=True)[0])
        for telegram in (False, True):
            rendered = _answer_parts('Подробнее на [сайте]('+url+').',sources,telegram=telegram)[0]
            self.assertEqual(rendered.count(url), 1)
            self.assertIn('Подробнее на сайте.', rendered)

    def test_standalone_question_returns_answer_and_actual_source_without_internal_context(self):
        q = 'Найди в интернете максимальный вес посылки и дай ссылку'
        url = 'https://example.org/weight'
        self.assertEqual(gpt_web.standalone_web_query(q), q)
        self.assertIsNone(gpt_web.standalone_web_query('Найди в интернете информацию об этих компаниях'))
        self.assertIsNone(gpt_web.standalone_web_query(q + ' и сравни с данными 1С'))
        result = {'pages':[{'url':url,'title':'Правила','finding':'20 кг'}], 'answer':'Обычная посылка — до 20 кг.'}
        with patch.object(gpt_web,'public_run',return_value=(result,{url},True)):
            data = gpt_web.search(q, q, {}, 'test', PrivacyContext(), compose=True)
            self.assertEqual(data['answer'], result['answer'])
            self.assertEqual(data['pages'][0]['url'], url)
            result['answer'] += ' https://example.org/fake'
            self.assertNotIn('answer', gpt_web.search(q, q, {}, 'test', PrivacyContext(), compose=True))

    def test_query_from_other_context_is_not_sent(self):
        with patch.object(gpt_web.call_analysis,'run_codex') as run:
            with self.assertRaises(ValueError):
                gpt_web.search('данные из CRM','найди информацию в интернете',{},'test',PrivacyContext())
            run.assert_not_called()
