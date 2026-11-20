import base64
import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app import common, help_requests


class HelpRequestsTest(unittest.TestCase):
    def setUp(self):
        self.con = common.connect(':memory:')
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO employees(login,b24_user_id) VALUES ('debugger',42)")
        self.bot = Mock()
        self.bot.status.return_value = {'bot_id': 7}
        self.sender = {'login': 'employee', 'name': 'Кирилл Бартов'}
        self.bot.config = {}
        runner = patch('app.help_requests.run_codex', return_value={'result': {'summary': 'Сбой загрузки'}})
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.expected = ('Проблема в работе дашборда\nПользователь: Кирилл Бартов\n'
                         'Суть проблемы: Сбой загрузки\nТекст пользователя: Ошибка')
        self.data = {'request_id': '1234567890abcdef', 'text': 'Ошибка', 'images': []}

    def send(self):
        help_requests.send(self.con, self.bot, self.sender, self.data)

    def test_requires_recipient(self):
        with self.assertRaisesRegex(ValueError, 'не назначен'):
            self.send()
        self.bot.api.call.assert_not_called()

    def test_delivers_text_and_image_only_once(self):
        help_requests.configure(self.con, 'debugger')
        image = io.BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')
        self.data['images'] = [base64.b64encode(image.getvalue()).decode()]
        self.bot.api.call.side_effect = [{'id': 1}, {'file': {'id': 2}}]
        self.send()
        self.send()
        text, file = self.bot.api.call.call_args_list
        self.assertEqual(text.args[1]['dialogId'], '42')
        self.assertEqual(text.args[1]['fields']['message'], self.expected)
        self.assertEqual(file.args[1]['dialogId'], '42')
        self.assertEqual(file.args[0], 'imbot.v2.File.upload')

    def test_text_only(self):
        help_requests.configure(self.con, 'debugger')
        self.bot.api.call.return_value = {'id': 1}
        self.send()
        del self.data['images']
        self.send()
        self.bot.api.call.assert_called_once()
        self.assertEqual(self.bot.api.call.call_args.args[1]['fields']['message'], self.expected)
        self.runner.assert_called_once()

    def test_summary_failure_preserves_original(self):
        help_requests.configure(self.con, 'debugger')
        self.runner.side_effect = RuntimeError()
        self.bot.api.call.return_value = {'id': 1}
        self.send()
        self.assertIn('Автосводка недоступна\nТекст пользователя: Ошибка',
                      self.bot.api.call.call_args.args[1]['fields']['message'])
