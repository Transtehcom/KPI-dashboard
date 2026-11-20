import time
import unittest
from unittest.mock import Mock, patch

from tests import test_bitrix_bot as fixtures


class TaskFlowTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BitrixBotTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.activate()
        self.fixture.api.create_user_task = Mock(return_value=123)

    def send(self, n, text, user=7):
        self.fixture.feed(fixtures.event(n, user=user, text=text))
        for _ in range(60):
            job = self.fixture.jobs()[-1]
            if job['state'] != 'task_parsing':
                return job
            time.sleep(0.01)
        self.fail('Task parser failed to finish')

    def test_clarify_then_create_once(self):
        fields = dict(responsible=[], accomplices=[], auditors=[], deadline='', description='', title='', question='')
        complete = dict(fields, responsible=['staff', 'third'], title='Согласуйте оформление сайта.')
        with patch('app.bitrix_task_parse.parse', side_effect=[fields, complete]) as parse:
            job = self.send(1001, 'Поставь задачу')
            self.assertIn('Кому', __import__('json').loads(job['parts'])[0])
            self.fixture.bot.tick()
            self.send(1002, 'staff и third, согласовать оформление сайта')
            self.assertEqual(len(parse.call_args.args[0]), 2)
            self.assertEqual(parse.call_args.kwargs, self.fixture.bot._selection(job['owner']))
            self.fixture.bot.tick()
            self.send(1002, 'staff и third, согласовать оформление сайта')
        self.fixture.api.create_user_task.assert_called_once_with(7, {'TITLE':complete['title'], 'RESPONSIBLE_ID':8,'ACCOMPLICES':[9]})

    def test_unknown_recipient_and_cancel(self):
        fields = dict(responsible=['Неизвестный'], accomplices=[], auditors=[], deadline='', description='', title='Согласуй сайт', question='')
        with patch('app.bitrix_task_parse.parse', return_value=fields):
            job = self.send(1001, 'Поставь задачу Неизвестному')
            self.assertIn('Уточни', __import__('json').loads(job['parts'])[0])
            self.fixture.bot.tick()
            self.send(1002, 'отмена')
        self.fixture.api.create_user_task.assert_not_called()
        self.assertEqual(self.fixture.con.execute('select count(*) from bitrix_task_drafts').fetchone()[0], 0)
