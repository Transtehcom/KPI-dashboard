import copy
import base64
import datetime as dt
import io
import json
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
        self.uploaded = []
        self.fail_upload = False

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
        if method == 'imbot.v2.File.upload':
            self.uploaded.append(copy.deepcopy(params))
            if self.fail_upload:
                raise OSError('access_token=PRIVATE_API_SECRET')
            return {'file': {'id': 555 + len(self.uploaded)}, 'messageId': 9500 + len(self.uploaded),
                    'chatId': 112, 'dialogId': params['dialogId']}
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
        job = self.jobs()[index]
        self.con.execute("UPDATE gpt_messages SET status='completed', content=?, sources=?,finished=? WHERE id=?",
                         (content, json.dumps(sources or []), dt.datetime.now().isoformat(timespec='microseconds'), job['assistant_id']))
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

    def pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 100 Td (Attached document) Tj ET')
        page[NameObject('/Contents')] = stream
        raw = io.BytesIO()
        writer.write(raw)
        return raw.getvalue()

    def attach(self, item, *identities):
        item['data']['message']['params'] = {'FILE_ID': [str(identity) for identity in identities]}
        return item

    def files(self, values):
        helper = mock.Mock()
        helper.download.side_effect = lambda _bot, _dialog, ref: values[ref['id']]
        self.bot.files = helper
        return helper

    def generated(self, job_index=0, name='result.docx'):
        from docx import Document
        job = self.jobs()[job_index]
        source = self.con.execute('SELECT id FROM gpt_documents WHERE chat_id=? ORDER BY rowid LIMIT 1', (job['chat_id'],)).fetchone()[0]
        doc = Document()
        doc.add_paragraph('Converted document')
        raw = io.BytesIO()
        doc.save(raw)
        return self.gpt.documents.save(job['chat_id'], name, raw.getvalue(), parent=source)

    def test_pdf_is_imported_with_privacy_before_the_model_receives_conversion_request(self):
        self.activate()
        raw = self.pdf()
        helper = self.files({66: ('statement.pdf', raw)})
        with mock.patch.object(self.gpt, 'document_upload', wraps=self.gpt.document_upload) as upload:
            self.feed(self.attach(event(text='сделай из pdf в word'), 66))
        self.assertEqual(helper.download.call_count, 1)
        self.assertEqual(upload.call_args.kwargs, {'protected': True})
        self.assertEqual(self.submit.call_args.args[2], 'сделай из pdf в word')
        self.assertEqual(len(self.gpt.chat('admin', self.jobs()[0]['chat_id'])['documents']), 1)
        self.assertEqual(json.loads(self.jobs()[0]['attachments'])[0]['id'], 66)

    def test_file_only_message_is_retained_and_has_a_useful_request(self):
        self.activate()
        self.files({66: ('notes.txt', b'Attached text')})
        self.feed(self.attach(event(text=''), 66))
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.submit.call_count, 1)
        self.assertIn('Прикреплён файл', self.submit.call_args.args[2])

    def test_import_failure_reports_attachment_failure_without_model_call_or_secret(self):
        self.activate()
        self.bot.files = mock.Mock()
        self.bot.files.download.side_effect = OSError('PRIVATE_API_SECRET')
        self.feed(self.attach(event(text='Преобразуй PDF'), 66))
        self.bot.tick()
        self.assertEqual(self.submit.call_count, 0)
        self.assertIn('Не удалось загрузить', self.api.sent[0]['fields']['message'])
        self.assertNotIn('PRIVATE_API_SECRET', str(self.api.sent))
        self.assertTrue(json.loads(self.jobs()[0]['attachments']))

    def test_restart_after_local_document_commit_recovers_import_without_duplicate(self):
        self.activate()
        values = {66: ('notes.txt', b'Attached text')}
        self.files(values)
        original_upload = self.gpt.document_upload

        def crash_after_upload(*args, **kwargs):
            original_upload(*args, **kwargs)
            raise SystemExit('simulated process loss')

        with mock.patch.object(self.gpt, 'document_upload', side_effect=crash_after_upload):
            with self.assertRaises(SystemExit):
                self.feed(self.attach(event(text='Прочитай файл'), 66))
        self.bot = self.Bot(self.con, self.config, self.api, lambda: self.gpt)
        self.files(values)
        self.bot.tick()
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.con.execute('SELECT count(*) FROM gpt_documents').fetchone()[0], 1)

    def test_attachment_network_download_does_not_hold_database_lock_and_rechecks_owner(self):
        from app.common import db_lock
        self.activate()
        self.bot.files = mock.Mock()

        def download(*_args):
            self.assertFalse(db_lock()._is_owned())
            self.config['employees'][0]['b24_user_id'] = 77
            return 'notes.txt', b'Attached text'

        self.bot.files.download.side_effect = download
        self.feed(self.attach(event(text='Прочитай файл'), 66))
        self.assertEqual(self.submit.call_count, 0)
        self.assertEqual(self.con.execute('SELECT count(*) FROM gpt_documents').fetchone()[0], 0)

    def test_only_this_turns_new_derived_documents_are_returned_to_the_same_user(self):
        self.activate()
        self.files({66: ('statement.pdf', self.pdf())})
        self.feed(self.attach(event(text='сделай из pdf в word'), 66))
        output = self.generated()
        self.complete()
        self.generated(name='later.docx')
        self.gpt.documents.save(self.jobs()[0]['chat_id'], 'unrelated.txt', b'Other upload')
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(len(self.api.uploaded), 1)
        request = self.api.uploaded[0]
        self.assertEqual(request['dialogId'], '7')
        self.assertEqual(request['fields']['name'], output['name'])
        self.assertEqual(base64.b64decode(request['fields']['content']), self.gpt.documents.download(self.jobs()[0]['chat_id'], output['id'])[1])

    def test_uncertain_file_upload_retries_saved_file_without_repeating_text_or_gpt(self):
        self.activate()
        self.files({66: ('notes.txt', b'Attached text')})
        self.feed(self.attach(event(text='Сделай Word'), 66))
        self.generated()
        self.complete()
        self.api.fail_upload = True
        self.bot.tick()
        self.assertEqual(self.jobs()[0]['state'], 'uncertain')
        self.assertEqual(self.api.sent, [])
        self.api.fail_upload = False
        self.feed(event(1002, text='/retry'))
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(len(self.api.uploaded), 2)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.jobs()[0]['state'], 'sent')

    def test_explicit_current_turn_output_returns_cached_conversion_without_other_history(self):
        self.activate()
        self.files({66: ('notes.txt', b'Attached text')})
        self.feed(self.attach(event(text='Сделай Word'), 66))
        cached = self.generated(name='cached.docx')
        self.complete()
        self.bot.tick()
        self.bot.tick()
        self.feed(event(1002, text='Пришли тот же Word ещё раз'))
        self.generated(job_index=1, name='unbound.docx')
        job = self.jobs()[1]
        self.con.execute('''CREATE TABLE IF NOT EXISTS gpt_document_outputs (
            chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,document_id TEXT NOT NULL,
            PRIMARY KEY(message_id,document_id))''')
        self.con.execute('INSERT INTO gpt_document_outputs VALUES (?,?,?)',
                         (job['chat_id'], job['assistant_id'], cached['id']))
        self.con.commit()
        self.complete(index=1)
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(len(self.api.uploaded), 2)
        self.assertEqual(self.api.uploaded[-1]['fields']['name'], 'cached.docx')

    def test_legacy_string_text_outbox_remains_deliverable(self):
        self.activate()
        self.feed(event())
        self.complete()
        self.con.execute("UPDATE bitrix_bot_jobs SET state='delivering',parts=?,part_index=0 WHERE id=1", (json.dumps(['Старый ответ']),))
        self.con.commit()
        self.bot.tick()
        self.assertEqual(self.api.sent[0]['fields']['message'], 'Старый ответ')


if __name__ == '__main__':
    unittest.main()
