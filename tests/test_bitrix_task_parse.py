import unittest
from unittest.mock import patch

from app import bitrix_task_parse as parser


class TaskParserTest(unittest.TestCase):
    def test_explicit_requests(self):
        for text in ('Мне надо поставить задачу', 'Поставь задачу Валентине',
                     'Пожалуйста, поставьте Кириллу задачу проверить КП',
                     'Можешь создать задачу?', 'Привет! Заведи, пожалуйста, новую задачу'):
            with self.subTest(text=text):
                self.assertTrue(parser.starts(text))

    def test_questions_and_mentions_stay_in_chat(self):
        for text in ('Покажи задачи Валентины', 'Кто поставил задачу?', 'Можно ли поставить задачу?',
                     'Как поставить задачу?', 'Не надо создавать задачу', 'Закрой задачу',
                     'Создай отчёт по задачам', 'Мне сказали: «Поставь задачу»'):
            with self.subTest(text=text):
                self.assertFalse(parser.starts(text))

    def setUp(self):
        self.result = {'title': 'Согласование оформления сайта',
                       'responsible': ['Валентине'], 'auditors': [], 'accomplices': [],
                       'description': 'Валентина, согласуйте, пожалуйста, оформление сайта.', 'deadline': '', 'question': ''}
        runner = patch.object(parser.call_analysis, 'run_codex', return_value={'result': self.result})
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.messages = ['Поставь задачу Валентине. Мне надо, чтобы она согласовала оформление сайта.']

    def test_restricts_model_inputs_and_preserves_roles(self):
        config = {'webhook': 'xyzSecret', 'employees': [{'name': 'Скрытый сотрудник'}]}
        self.messages.append('Для наблюдателя Ивана, соисполнитель Анна. Пароль: xyzSecret')
        self.result.update(auditors=['Ивана'], accomplices=['Анна'], deadline='2026-11-20T15:00:00+05:00')
        self.messages.append('До завтра в 15:00.')
        self.assertEqual(parser.parse(self.messages, config, 'private_login'), self.result)
        prompt, settings = self.runner.call_args.args
        for secret in ('xyzSecret', 'Скрытый сотрудник', 'private_login'):
            self.assertNotIn(secret, prompt)
            self.assertNotIn(secret, str(settings))
        self.assertFalse(settings['codex_web_search'])
        self.assertFalse(self.runner.call_args.kwargs['schema']['additionalProperties'])

    def test_unknown_or_partial_person_requires_clarification(self):
        self.result['auditors'] = ['Валентин', 'Пётр']
        draft = parser.parse(self.messages, {}, 'owner')
        self.assertEqual(draft['auditors'], [])
        self.assertTrue(draft['question'])

    def test_rejects_unusable_deadline(self):
        for invalid in ('завтра', '2026-11-20', '2026-11-20T12:00'):
            with self.subTest(invalid=invalid):
                self.result['deadline'] = invalid
                draft = parser.parse(self.messages, {}, 'owner')
                self.assertEqual(draft['deadline'], '')
                self.assertTrue(draft['question'])

    def test_incomplete_or_unrelated_requires_clarification(self):
        self.result.update(responsible=[], title='')
        self.assertTrue(parser.parse(['Мне надо поставить задачу'], {}, 'owner')['question'])
        self.result['question'] = 'Продолжить постановку задачи или отменить её?'
        self.assertEqual(parser.parse(self.messages + ['Какая погода?'], {}, 'owner')['question'],
                         self.result['question'])

    def test_complete_task_requires_description(self):
        self.result['description'] = ' '
        with self.assertRaisesRegex(ValueError, 'описание'):
            parser.parse(self.messages, {}, 'owner')

    def test_rejects_unexpected_model_fields(self):
        self.result['responsibleId'] = 55
        with self.assertRaisesRegex(ValueError, 'разобрать поля'):
            parser.parse(self.messages, {}, 'owner')
