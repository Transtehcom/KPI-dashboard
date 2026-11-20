import unittest
from unittest.mock import Mock

from app import bot_chat_controls
from app.bitrix_bot import BitrixBot
from app.common import connect
from app.telegram_bot import Telegram


class TelegramMenus(unittest.TestCase):
    def setUp(self):
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        config = {'employees': [
            {'login':'boss', 'role':'manager', 'b24_user_id':7},
            {'login':'worker', 'role':'employee', 'b24_user_id':8}]}
        self.bot = BitrixBot(self.con,config,Mock(),Mock())
        self.tg = self.bot.telegram = Telegram(self.bot)
        self.tg.bot_id, self.tg.username = 51, 'bot'
        self.tg.call = Mock()
        self.con.executemany('INSERT INTO tg_binding VALUES(51,?,?,?)',[(77,'boss',7),(88,'worker',8)])
        self.con.commit()
        self.counter = 100

    def message(self, peer, text):
        self.counter += 1
        self.tg.call.reset_mock()
        self.tg.handle_update({'update_id':self.counter, 'message':{'chat':{'id':peer,'type':'private'},
                               'from':{'id':peer}, 'text':text}})
        return self.tg.call.call_args.args[1] if self.tg.call.called else None

    def click(self, peer, value, sender=None):
        self.counter += 1
        self.tg.handle_update({'update_id':self.counter, 'callback_query':{
            'id':str(self.counter), 'data':value, 'from':{'id':sender or peer},
            'message':{'message_id':111,'from':{'id':self.tg.bot_id}, 'chat':{'id':peer,'type':'private'}}}})
        return self.tg.call.call_args.args

    def options(self, fields):
        return {button['text']:button['callback_data'] for row in fields['reply_markup']['inline_keyboard']
                for button in row}

    def test_employee_chooses_from_two_models_and_finishes_after_effort(self):
        entry = self.options(self.message(88,'/start'))
        _, models = self.click(88,entry[bot_chat_controls.MODELS_LABEL])
        self.assertEqual(set(self.options(models)), {'GPT-6.2 Sol','GPT-6.1 Terra'})
        method, levels = self.click(88,self.options(models)['GPT-6.2 Sol'])
        self.assertEqual(method,'editMessageText')
        self.assertEqual(set(self.options(levels)), {'Низкий','Средний','Высокий'})
        _, result = self.click(88,self.options(levels)['Высокий'])
        self.assertFalse(self.options(result))
        self.assertEqual(self.bot._selection('worker'), {'model':'gpt-6.2-sol','effort':'high','fast':False})

    def test_admin_chooses_speed_and_stale_buttons_cannot_change_settings(self):
        initial = self.bot._selection('boss')
        models = self.options(self.message(77,'/settings'))
        _, levels = self.click(77,models['GPT-6.2 Astra'])
        _, speeds = self.click(77,self.options(levels)['Ультра'])
        self.assertEqual(self.bot._selection('boss'),initial)
        method, ack = self.click(77,models['GPT-6.2 Astra'])
        self.assertEqual(method,'answerCallbackQuery')
        self.assertTrue(ack['show_alert'])
        self.click(77,self.options(speeds)['🚀 Fast'])
        self.assertEqual(self.bot._selection('boss'), {'model':'gpt-6.2-astra','effort':'ultra','fast':True})

    def test_callbacks_require_private_sender_and_current_binding(self):
        options = self.options(self.message(77,'/model'))
        button = options['GPT-6.2 Astra']
        self.assertEqual(self.click(77,button,sender=88)[0],'answerCallbackQuery')
        self.tg.unlink('boss')
        self.assertEqual(self.click(77,button)[0],'answerCallbackQuery')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM bot_model_preferences').fetchone()[0],0)

    def test_help_and_menu_work_for_employee(self):
        entry = self.options(self.message(88,'/start'))
        self.assertIn(bot_chat_controls.RESTART_LABEL,entry)
        _, help_info = self.click(88,entry[bot_chat_controls.HELP_LABEL])
        self.assertEqual(help_info['text'],self.message(88,'/help')['text'])
        back = next(iter(self.options(help_info).values()))
        _, main = self.click(88,back)
        self.assertEqual(self.options(main),entry)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM bitrix_bot_jobs').fetchone()[0],0)

    def test_only_confirmed_restart_is_enqueued_in_message_order(self):
        self.message(88,'первое сообщение')
        entry = self.options(self.message(88,'/menu'))
        _, prompt = self.click(88,entry[bot_chat_controls.RESTART_LABEL])
        options = self.options(prompt).values()
        self.click(88,next(v for v in options if v.endswith(':no')))
        yes = next(v for v in options if v.endswith(':yes'))
        self.assertEqual(self.click(88,yes)[0],'answerCallbackQuery')
        confirmation = self.options(self.message(88,'/restart@bot')).values()
        yes = next(v for v in confirmation if v.endswith(':yes'))
        self.assertEqual(self.click(77,yes)[0],'answerCallbackQuery')
        self.click(88,yes)
        self.assertEqual(self.click(88,yes)[0],'answerCallbackQuery')
        self.message(88,'второе сообщение')
        rows = self.con.execute('SELECT owner,question FROM bitrix_bot_jobs ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in rows],[('worker','первое сообщение'),('worker','/new'),('worker','второе сообщение')])
