import copy
import sqlite3
import unittest
from unittest import mock

from app.common import connect, load_rules
from app.gpt_assistant import GPTAssistant


class BotAPI:
    def __init__(self):
        self.calls = []
        self.batches = []
        self.sent = []
        self.fail_send = False
        self.send_error = None

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'imbot.v2.Bot.register':
            return {'bot': {'id': 456, 'code': 'ttkgpt_dashboard', 'type': 'bot', 'eventMode': 'fetch'}}
        if method == 'imbot.v2.Event.get':
            if self.batches:
                return self.batches.pop(0)
            return {'events': [], 'nextOffset': params['offset'], 'hasMore': False}
        if method == 'imbot.v2.Chat.Message.send':
            self.sent.append(copy.deepcopy(params))
            if self.send_error:
                raise self.send_error
            if self.fail_send:
                raise OSError('access_token=PRIVATE_API_SECRET')
            return {'id': 9000 + len(self.sent), 'uuidMap': {}}
        raise AssertionError(method)


def event(identity=1001, user=7, text='Мои задачи', message=None):
    return {
        'eventId': identity, 'type': 'ONIMBOTV2MESSAGEADD', 'date': '2026-11-18T12:00:00+05:00',
        'data': {
            'bot': {'id': 456, 'code': 'ttkgpt_dashboard', 'type': 'bot', 'eventMode': 'fetch'},
            'message': {'id': message or identity + 100, 'chatId': 112, 'authorId': user,
                        'text': text, 'isSystem': False, 'params': []},
            'chat': {'id': 112, 'dialogId': str(user), 'type': 'private'},
            'user': {'id': user, 'bot': False, 'type': 'user', 'active': True},
        },
    }


class BitrixBotTest(unittest.TestCase):
    def setUp(self):
        from app.bitrix_bot import BitrixBot
        self.Bot = BitrixBot
        self.con = connect(':memory:')
        self.addCleanup(self.con.close)
        self.config = {
            'mode': 'demo', 'ttkgpt_runtime': 'appserver',
            'employees': [
                {'login': 'admin', 'name': 'Администратор', 'role': 'manager', 'b24_user_id': 7},
                {'login': 'staff', 'name': 'Сотрудник', 'role': 'employee', 'position': 'pm', 'b24_user_id': 8},
                {'login': 'third', 'name': 'Третий', 'role': 'employee', 'position': 'supply', 'b24_user_id': 9},
            ],
            'bitrix24': {'webhook': 'https://portal.example/rest/1/SECRET/'},
        }
        self.gpt = GPTAssistant(self.con, self.config, load_rules())
        original_submit = self.gpt.submit

        def submit(*args, **kwargs):
            return original_submit(*args, **kwargs, launch=False)

        self.submit = mock.Mock(side_effect=submit)
        self.gpt.submit = self.submit
        self.api = BotAPI()
        self.bot = self.Bot(self.con, self.config, self.api, lambda: self.gpt)

    def activate(self):
        self.bot.activate()

    def feed(self, *events):
        self.api.batches.append({'events': list(events), 'nextOffset': max(e['eventId'] for e in events) + 1, 'hasMore': False})
        self.bot.tick()

    def jobs(self):
        return [dict(row) for row in self.con.execute('SELECT * FROM bitrix_bot_jobs ORDER BY id')]

    def complete(self, index=0, content='Готово', sources=None):
        import json
        job = self.jobs()[index]
        self.con.execute("UPDATE gpt_messages SET status='completed', content=?, sources=? WHERE id=?",
                         (content, json.dumps(sources or []), job['assistant_id']))
        self.con.commit()
        self.gpt.active.pop(job['chat_id'], None)

    def test_registration_happens_only_on_explicit_activation_and_is_persistent(self):
        self.assertIsNone(self.bot.status()['bot_id'])
        self.bot.start()
        self.bot.stop()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bot.activate(), {'bot_id': 456})
        self.assertEqual(self.bot.activate(), {'bot_id': 456})
        other = self.Bot(self.con, self.config, self.api, lambda: self.gpt)
        self.assertEqual(other.status()['bot_id'], 456)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0][1]['fields'], {
            'code': 'ttkgpt_dashboard', 'properties': {'name': 'ttkGPT'}, 'type': 'bot', 'eventMode': 'fetch',
        })

    def test_private_message_reuses_owned_gpt_and_delivers_exact_assistant(self):
        self.activate()
        self.feed(event())
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.submit.call_args.args[0], 'admin')
        self.complete(content='Ваш ответ')
        self.bot.tick()
        self.assertEqual(self.api.sent[0]['dialogId'], '7')
        self.assertEqual(self.api.sent[0]['fields']['message'], 'Ваш ответ')
        self.assertFalse(self.api.sent[0]['fields']['urlPreview'])
        self.assertEqual(self.jobs()[0]['state'], 'sent')

    def test_fetch_offset_and_queue_survive_restart_and_redelivery(self):
        self.activate()
        self.feed(event())
        self.complete()
        self.bot = self.Bot(self.con, self.config, self.api, lambda: self.gpt)
        self.feed(event(1002, message=1101))
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.api.calls[-2][1]['offset'], 1002)
        self.assertEqual(self.jobs()[0]['state'], 'sent')

    def test_queue_failure_does_not_advance_acknowledgment(self):
        self.activate()
        self.con.execute("CREATE TRIGGER queue_failure BEFORE INSERT ON bitrix_bot_jobs BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        self.con.commit()
        self.api.batches.append({'events': [event()], 'nextOffset': 1002, 'hasMore': False})
        with self.assertRaises(sqlite3.DatabaseError):
            self.bot.tick()
        self.assertEqual(self.con.execute('SELECT next_offset FROM bitrix_bot_state').fetchone()[0], 0)
        self.assertEqual(self.submit.call_count, 0)

    def test_ignores_group_bot_mismatched_and_system_messages(self):
        self.activate()
        changes = [
            ('chat', 'type', 'chat'), ('chat', 'dialogId', 'chat112'),
            ('bot', 'id', 999), ('user', 'bot', True), ('user', 'type', 'bot'),
            ('message', 'authorId', 456), ('message', 'isSystem', True),
            ('message', 'chatId', 999),
        ]
        events = []
        for i, (section, field, value) in enumerate(changes):
            item = event(1001 + i)
            item['data'][section][field] = value
            events.append(item)
        self.feed(*events)
        self.assertEqual(self.submit.call_count, 0)
        self.assertEqual(self.api.sent, [])

    def test_unknown_and_ambiguous_users_never_reach_gpt(self):
        self.activate()
        self.config['employees'].append({'login': 'duplicate', 'b24_user_id': 7})
        self.feed(event(), event(1002, user=99))
        self.assertEqual(self.submit.call_count, 0)
        self.assertEqual(self.api.sent, [])

    def test_mapping_is_checked_again_before_submit_and_delivery(self):
        self.activate()
        self.gpt.active = {'busy1': object(), 'busy2': object()}
        self.feed(event())
        self.assertEqual(self.jobs()[0]['state'], 'queued')
        self.config['employees'][0]['b24_user_id'] = 77
        self.gpt.active.clear()
        self.bot.tick()
        self.assertEqual(self.submit.call_count, 0)
        self.config['employees'][0]['b24_user_id'] = 7
        self.feed(event(1002))
        self.complete(index=1)
        self.config['employees'][0]['b24_user_id'] = 77
        self.bot.tick()
        self.assertEqual(self.api.sent, [])

    def test_keeps_each_user_sequential_and_queues_global_busy_service(self):
        self.activate()
        self.feed(event(), event(1002), event(1003, user=8), event(1004, user=9))
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual([job['state'] for job in self.jobs()], ['running', 'queued', 'running', 'queued'])
        self.complete()
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.jobs()[1]['state'], 'queued')
        self.complete(index=2)
        self.bot.tick()
        self.assertEqual(self.jobs()[1]['state'], 'running')

    def test_model_selection_respects_current_staff_policy(self):
        self.activate()
        self.config['bitrix_app'] = {'model': 'gpt-6.1-sol', 'effort': 'high', 'fast': True}
        self.feed(event(), event(1002, user=8))
        self.assertEqual(self.submit.call_args_list[0].kwargs, {'model': 'gpt-6.1-sol', 'effort': 'high', 'fast': True})
        self.assertEqual(self.submit.call_args_list[1].kwargs, {'model': 'gpt-6.1-sol', 'effort': 'medium', 'fast': False})

    def test_changed_access_creates_new_context_and_old_response_is_withheld(self):
        self.activate()
        self.feed(event(1001, user=8))
        old_chat = self.jobs()[0]['chat_id']
        self.complete()
        self.config['employees'][1]['position'] = 'supply'
        self.bot.tick()
        self.assertEqual(self.api.sent, [])
        self.feed(event(1002, user=8))
        self.assertNotEqual(self.jobs()[1]['chat_id'], old_chat)

    def test_restart_reconciles_submit_commit_without_bridge_mid_commit(self):
        self.activate()
        self.feed(event())
        mid = self.jobs()[0]['assistant_id']
        self.con.execute("UPDATE bitrix_bot_jobs SET state='submitting',assistant_id=NULL,baseline_id=0")
        self.con.commit()
        self.bot = self.Bot(self.con, self.config, self.api, lambda: self.gpt)
        self.assertEqual(self.jobs()[0]['assistant_id'], mid)
        self.assertEqual(self.jobs()[0]['state'], 'running')
        self.assertEqual(self.submit.call_count, 1)

    def test_delivery_neutralizes_model_bbcode_and_rejects_unsafe_source_urls(self):
        self.activate()
        self.feed(event())
        self.complete(content='[USER=1]Все[/USER] [send=/delete]Удалить[/send]', sources=[
            {'title': 'Карточка [USER=1]', 'url': 'https://portal.example/crm/deal/details/42/'},
            {'title': 'Ключ', 'url': 'https://portal.example/rest/1/SECRET/'},
            {'title': 'Опасно', 'url': 'javascript:alert(1)'},
        ])
        self.bot.tick()
        text = self.api.sent[0]['fields']['message']
        self.assertNotIn('[USER', text)
        self.assertNotIn('[send', text)
        self.assertIn('[URL=https://portal.example/crm/deal/details/42/]', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('javascript:', text)

    def test_long_unicode_answer_has_bounded_messages_and_persistent_chunk_progress(self):
        self.activate()
        self.feed(event())
        self.complete(content='😀' * 21000)
        self.bot.tick()
        self.bot = self.Bot(self.con, self.config, self.api, lambda: self.gpt)
        for _ in range(3):
            self.bot.tick()
        self.assertEqual(''.join(item['fields']['message'] for item in self.api.sent), '😀' * 21000)
        self.assertTrue(all(len(item['fields']['message'].encode('utf-16-le')) // 2 <= 20000 for item in self.api.sent))
        self.assertEqual(self.jobs()[0]['state'], 'sent')

    def test_ambiguous_delivery_retries_only_on_own_retry_command_without_new_gpt_turn(self):
        self.activate()
        self.feed(event())
        self.complete()
        self.api.fail_send = True
        self.bot.tick()
        self.assertEqual(self.jobs()[0]['state'], 'uncertain')
        self.assertNotIn('PRIVATE_API_SECRET', str(self.bot.status()))
        self.bot.tick()
        self.assertEqual(len(self.api.sent), 1)
        self.api.fail_send = False
        self.feed(event(1002, text='/retry'))
        self.bot.tick()
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(len(self.api.sent), 2)
        self.assertEqual(self.jobs()[0]['state'], 'sent')

    def test_new_command_resets_only_requesters_context(self):
        self.activate()
        self.feed(event(), event(1002, user=8))
        self.complete()
        self.complete(index=1)
        self.bot.tick()
        staff_chat = self.jobs()[1]['chat_id']
        self.feed(event(1003, text='/new'))
        self.feed(event(1004), event(1005, user=8))
        self.assertNotEqual(self.jobs()[3]['chat_id'], self.jobs()[0]['chat_id'])
        self.assertEqual(self.jobs()[4]['chat_id'], staff_chat)

    def test_stale_job_snapshot_cannot_resubmit_or_redeliver_a_finished_turn(self):
        self.activate()
        self.feed(event())
        stale = self.jobs()[0]
        self.complete()
        self.bot.tick()
        self.bot._process(stale)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.submit.call_count, 1)

    def test_employee_event_user_type_keeps_all_private_identity_gates(self):
        self.activate()
        item = event()
        item['data']['user']['type'] = 'employee'
        self.feed(item)
        self.assertEqual(self.submit.call_count, 1)

    def test_known_rate_rejection_is_retried_without_model_reprocessing(self):
        class RateError(Exception):
            retryable = True
            ambiguous = False

        self.activate()
        self.feed(event())
        self.complete()
        self.api.send_error = RateError('PRIVATE_API_SECRET')
        self.bot.tick()
        self.assertEqual(self.jobs()[0]['state'], 'delivering')
        self.assertNotIn('PRIVATE_API_SECRET', str(self.bot.status()))
        self.api.send_error = None
        self.bot.tick()
        self.assertEqual(self.jobs()[0]['state'], 'sent')
        self.assertEqual(self.submit.call_count, 1)

    def test_uncertain_delivery_does_not_block_new_user_questions(self):
        self.activate()
        self.feed(event())
        self.complete()
        self.api.fail_send = True
        self.bot.tick()
        self.feed(event(1002))
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual(self.jobs()[1]['state'], 'running')
        self.assertIn('/retry', self.bot.status()['last_error'])


if __name__ == '__main__':
    unittest.main()
