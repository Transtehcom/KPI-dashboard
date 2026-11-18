"""Durable private-message bridge between Bitrix24 Bot API v2 and ttkGPT."""
import json
import re
import threading
from urllib.parse import parse_qsl, urlsplit

from .call_analysis import model_options
from .common import db_lock


DELIVERY_UNKNOWN = 'Доставка ответа не подтверждена. Команда /retry повторит отправку сохранённого ответа.'
REQUEST_FAILED = 'Не удалось подготовить ответ. Отправьте вопрос ещё раз.'


def _positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,18}', str(value)):
        return None
    number = int(value)
    return number if number <= 9223372036854775807 else None


def _plain(text):
    # Fullwidth brackets render as text and cannot open Bitrix mentions or BB codes.
    text = str(text or '').replace('[', '［').replace(']', '］')
    return ''.join(char for char in text if char in '\n\t' or ord(char) >= 32)


def _source_line(source):
    if not isinstance(source, dict):
        return None
    url = source.get('url')
    if not isinstance(url, str) or len(url) > 2000 or re.search(r'[\s\[\]<>"\x00-\x1f]', url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        if re.search(r'/rest/[0-9]+/[^/]+', parsed.path, re.I):
            return None
        if any(re.search(r'auth|token|secret|password|api.?key', name, re.I) for name, _ in parse_qsl(parsed.query)):
            return None
    except ValueError:
        return None
    title = _plain(source.get('title') or source.get('system') or 'Источник')[:200]
    return '[URL=' + url + ']' + title + '[/URL]'


def _answer_parts(text, sources):
    text = _plain(text).strip() or REQUEST_FAILED
    # 9,000 Unicode code points remain below 20,000 UTF-16 units, including emoji.
    parts = [text[index:index + 9000] for index in range(0, len(text), 9000)]
    for source in (sources or [])[:10]:
        line = _source_line(source)
        if line:
            if len(parts[-1]) + len(line) + 2 <= 9000:
                parts[-1] += '\n\n' + line
            else:
                parts.append(line)
    return parts


class BitrixBot:
    def __init__(self, con, config, api, gpt_factory):
        self.con, self.config, self.api, self.gpt_factory = con, config, api, gpt_factory
        self._activation_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        with db_lock():
            con.executescript('''
                CREATE TABLE IF NOT EXISTS bitrix_bot_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), bot_id INTEGER,
                    next_offset INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO bitrix_bot_state(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS bitrix_bot_events (
                    bot_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
                    PRIMARY KEY(bot_id,event_id));
                CREATE TABLE IF NOT EXISTS bitrix_bot_chats (
                    bot_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    owner TEXT NOT NULL, chat_id TEXT NOT NULL,
                    PRIMARY KEY(bot_id,user_id));
                CREATE TABLE IF NOT EXISTS bitrix_bot_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    dialog_id TEXT NOT NULL, owner TEXT NOT NULL, question TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', chat_id TEXT, assistant_id INTEGER,
                    baseline_id INTEGER NOT NULL DEFAULT 0, parts TEXT,
                    part_index INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    UNIQUE(bot_id,message_id));
                CREATE INDEX IF NOT EXISTS ix_bitrix_bot_pending ON bitrix_bot_jobs(bot_id,state,user_id,id);
            ''')
            con.execute("UPDATE bitrix_bot_jobs SET state='uncertain',error=? WHERE state='sending'", (DELIVERY_UNKNOWN,))
            pending = [dict(row) for row in con.execute("SELECT * FROM bitrix_bot_jobs WHERE state='submitting'")]
            for job in pending:
                self._reconcile_submit(job)
            con.commit()

    def status(self):
        with db_lock():
            state = dict(self.con.execute('SELECT * FROM bitrix_bot_state WHERE id=1').fetchone())
            counts = dict(self.con.execute('SELECT state,count(*) FROM bitrix_bot_jobs WHERE bot_id=? GROUP BY state',
                                           (state['bot_id'],)))
            latest_error = self.con.execute("""SELECT error FROM bitrix_bot_jobs WHERE bot_id=?
                AND state IN ('uncertain','failed','delivering') AND error<>'' ORDER BY id DESC LIMIT 1""",
                                            (state['bot_id'],)).fetchone()
        return {'bot_id': state['bot_id'], 'queued': counts.get('queued', 0),
                'running': sum(counts.get(key, 0) for key in ('submitting', 'running', 'delivering', 'sending')),
                'uncertain': counts.get('uncertain', 0), 'failed': counts.get('failed', 0),
                'last_error': state['last_error'] or (latest_error[0] if latest_error else '')}

    def activate(self):
        with self._activation_lock:
            current = self.status()['bot_id']
            if current:
                return {'bot_id': current}
            try:
                result = self.api.call('imbot.v2.Bot.register', {'fields': {
                    'code': 'ttkgpt_dashboard', 'properties': {'name': 'ttkGPT'},
                    'type': 'bot', 'eventMode': 'fetch',
                }})
                bot = result['bot']
                identity = _positive_id(bot.get('id'))
                if (not identity or bot.get('code') != 'ttkgpt_dashboard'
                        or bot.get('eventMode') != 'fetch' or bot.get('type') != 'bot'):
                    raise ValueError
            except Exception:
                raise ValueError('Не удалось зарегистрировать чат-бота Битрикс24. Повторите активацию.') from None
            with db_lock():
                self.con.execute("UPDATE bitrix_bot_state SET bot_id=?,last_error='' WHERE id=1", (identity,))
                self.con.commit()
            self._wake.set()
            return {'bot_id': identity}

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopped.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name='bitrix-ttkgpt')
            self._thread.start()

    def stop(self):
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(5)

    def _run(self):
        while not self._stopped.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception:
                with db_lock():
                    self.con.execute('UPDATE bitrix_bot_state SET last_error=? WHERE id=1',
                                     ('Обмен с Битрикс24 временно недоступен. Повторная проверка выполняется автоматически.',))
                    self.con.commit()
            self._wake.wait(2)

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            bot_id = self.status()['bot_id']
            if not bot_id:
                return
            self._fetch(bot_id)
            with db_lock():
                jobs = [dict(row) for row in self.con.execute('''
                    SELECT j.* FROM bitrix_bot_jobs j
                    WHERE j.bot_id=? AND j.state IN ('queued','submitting','running','delivering')
                      AND NOT EXISTS (SELECT 1 FROM bitrix_bot_jobs earlier
                          WHERE earlier.bot_id=j.bot_id AND earlier.user_id=j.user_id AND earlier.id<j.id
                            AND earlier.state IN ('queued','submitting','running','delivering','sending'))
                    ORDER BY j.id LIMIT 100
                ''', (bot_id,))]
            for job in jobs:
                if self._stopped.is_set() and self._thread is threading.current_thread():
                    break
                self._process(job)
        finally:
            self._tick_lock.release()

    def _owner(self, user_id):
        matches = [employee for employee in self.config.get('employees', [])
                   if _positive_id(employee.get('b24_user_id')) == user_id]
        if len(matches) == 1 and isinstance(matches[0].get('login'), str) and matches[0]['login']:
            return matches[0]['login']
        return None

    def _incoming(self, bot_id, event):
        if event.get('type') != 'ONIMBOTV2MESSAGEADD' or not isinstance(event.get('data'), dict):
            return None
        data = event['data']
        bot, message, chat, user = (data.get(key) for key in ('bot', 'message', 'chat', 'user'))
        if not all(isinstance(item, dict) for item in (bot, message, chat, user)):
            return None
        user_id = _positive_id(user.get('id'))
        message_id = _positive_id(message.get('id'))
        if (not user_id or not message_id or user_id == bot_id or _positive_id(bot.get('id')) != bot_id
                or user.get('bot') is not False or user.get('type') not in (None, 'user', 'employee')
                or message.get('isSystem') is not False or _positive_id(message.get('authorId')) != user_id
                or chat.get('type') != 'private' or str(chat.get('dialogId')) != str(user_id)
                or not _positive_id(chat.get('id')) or chat['id'] != message.get('chatId')):
            return None
        owner = self._owner(user_id)
        text = message.get('text')
        if not owner or not isinstance(text, str) or not text.strip():
            return None
        return (bot_id, event['eventId'], message_id, user_id, str(user_id), owner, text.strip())

    def _fetch(self, bot_id):
        with db_lock():
            offset = self.con.execute('SELECT next_offset FROM bitrix_bot_state WHERE id=1').fetchone()[0]
        result = self.api.call('imbot.v2.Event.get', {'botId': bot_id, 'offset': offset, 'limit': 100, 'withUserEvents': False})
        if not isinstance(result, dict) or not isinstance(result.get('events'), list):
            raise ValueError('Некорректный ответ очереди Битрикс24')
        events, next_offset = result['events'], result.get('nextOffset')
        if (type(next_offset) is not int or next_offset < offset or len(events) > 100
                or any(not isinstance(event, dict) or not _positive_id(event.get('eventId')) for event in events)
                or events and next_offset <= max(event['eventId'] for event in events)):
            raise ValueError('Некорректный ответ очереди Битрикс24')
        with db_lock():
            try:
                for event in events:
                    inserted = self.con.execute('INSERT OR IGNORE INTO bitrix_bot_events VALUES (?,?)',
                                                (bot_id, event['eventId'])).rowcount
                    if inserted:
                        incoming = self._incoming(bot_id, event)
                        if incoming:
                            self.con.execute('''INSERT OR IGNORE INTO bitrix_bot_jobs
                                (bot_id,event_id,message_id,user_id,dialog_id,owner,question) VALUES (?,?,?,?,?,?,?)''', incoming)
                self.con.execute("UPDATE bitrix_bot_state SET next_offset=?,last_error='' WHERE id=1", (next_offset,))
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

    def _update(self, job_id, **values):
        # All field names come from this module, never from incoming events.
        with db_lock():
            self.con.execute('UPDATE bitrix_bot_jobs SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                             (*values.values(), job_id))
            self.con.commit()

    def _reconcile_submit(self, job):
        matches = self.con.execute('''SELECT id FROM gpt_messages WHERE chat_id=? AND id>? AND role='assistant'
                                      ORDER BY id LIMIT 2''', (job['chat_id'], job['baseline_id'])).fetchall()
        if len(matches) == 1:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='running',assistant_id=? WHERE id=?", (matches[0][0], job['id']))
        elif not matches:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='queued' WHERE id=?", (job['id'],))
        else:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='ignored',error=? WHERE id=?", (REQUEST_FAILED, job['id']))

    def _chat(self, service, job, reset=False):
        with db_lock():
            saved = self.con.execute('SELECT owner,chat_id FROM bitrix_bot_chats WHERE bot_id=? AND user_id=?',
                                     (job['bot_id'], job['user_id'])).fetchone()
            if saved and saved['owner'] == job['owner'] and not reset:
                try:
                    service.check_chat(job['owner'], saved['chat_id'])
                    return saved['chat_id']
                except ValueError:
                    pass
            chat_id = service.create_chat(job['owner'])['id']
            self.con.execute('INSERT OR REPLACE INTO bitrix_bot_chats VALUES (?,?,?,?)',
                             (job['bot_id'], job['user_id'], job['owner'], chat_id))
            self.con.commit()
            return chat_id

    def _selection(self, owner):
        employee = next(employee for employee in self.config['employees'] if employee['login'] == owner)
        admin = employee.get('role') == 'manager'
        options = model_options(admin)
        settings = self.config.get('bitrix_app', {})
        model = settings.get('model', 'gpt-6.1-sol')
        if model not in options['models']:
            model = 'gpt-6.1-sol' if 'gpt-6.1-sol' in options['models'] else next(iter(options['models']))
        effort = settings.get('effort', 'high' if admin else 'medium')
        if effort not in options['model_efforts'][model]:
            effort = 'medium' if 'medium' in options['model_efforts'][model] else next(iter(options['model_efforts'][model]))
        return {'model': model, 'effort': effort, 'fast': admin and settings.get('fast') is True}

    def _ready(self, job, text, sources=None):
        parts = _answer_parts(text, sources)
        with db_lock():
            current = self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()
            if current[0] not in ('queued', 'running'):
                return
            self._update(job['id'], state='delivering', parts=json.dumps(parts, ensure_ascii=False), part_index=0, error='')
            job.update(state='delivering', parts=json.dumps(parts, ensure_ascii=False), part_index=0)

    def _command(self, service, job):
        command = job['question'].casefold()
        if command in ('/new', 'новый чат'):
            self._chat(service, job, reset=True)
            self._ready(job, 'Создан новый чат. Отправьте вопрос.')
            return True
        if command == '/retry':
            with db_lock():
                previous = self.con.execute('''SELECT id FROM bitrix_bot_jobs
                    WHERE bot_id=? AND user_id=? AND owner=? AND id<? AND state='uncertain'
                    ORDER BY id DESC LIMIT 1''', (job['bot_id'], job['user_id'], job['owner'], job['id'])).fetchone()
                if previous:
                    self.con.execute("UPDATE bitrix_bot_jobs SET state='delivering',error='' WHERE id=?", (previous['id'],))
                    self.con.execute("UPDATE bitrix_bot_jobs SET state='sent' WHERE id=?", (job['id'],))
                    self.con.commit()
                    return True
            self._ready(job, 'Сохранённых ответов для повторной отправки нет.')
            return True
        return False

    def _process(self, job):
        with db_lock():
            job = dict(self.con.execute('SELECT * FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone())
            if job['state'] not in ('queued', 'submitting', 'running', 'delivering'):
                return
            if self._owner(job['user_id']) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
        service = self.gpt_factory()
        if job['state'] == 'queued':
            if self._command(service, job):
                if job['state'] == 'delivering':
                    self._deliver(service, job)
                return
            if len(job['question']) > 6000:
                self._ready(job, 'Введите вопрос длиной до 6 000 символов.')
            else:
                self._submit(service, job)
                return
        if job['state'] == 'running':
            try:
                chat = service.chat(job['owner'], job['chat_id'])
                message = next(item for item in chat['messages'] if item['id'] == job['assistant_id'] and item['role'] == 'assistant')
            except (ValueError, StopIteration):
                self._update(job['id'], state='ignored', error='Сохранённый ответ недоступен для текущих прав.')
                return
            if message['status'] in ('running', 'cancelling'):
                return
            if message['status'] == 'completed':
                self._ready(job, message['content'], message['sources'])
            else:
                self._ready(job, REQUEST_FAILED)
        if job['state'] == 'delivering':
            self._deliver(service, job)

    def _submit(self, service, job):
        with db_lock():
            if self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()[0] != 'queued':
                return
            if self._owner(job['user_id']) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
            chat_id = self._chat(service, job)
            if chat_id in service.active or len(service.active) >= 2:
                return
            baseline = self.con.execute('SELECT coalesce(max(id),0) FROM gpt_messages WHERE chat_id=?', (chat_id,)).fetchone()[0]
            self._update(job['id'], state='submitting', chat_id=chat_id, baseline_id=baseline)
            try:
                result = service.submit(job['owner'], chat_id, job['question'], **self._selection(job['owner']))
                self._update(job['id'], state='running', assistant_id=result['id'])
            except Exception:
                # submit() commits its messages internally. Recover a committed turn before any retry.
                recovery = {**job, 'chat_id': chat_id, 'baseline_id': baseline}
                self._reconcile_submit(recovery)
                self.con.commit()
                current = self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()[0]
                if current == 'queued':
                    self._ready(job, REQUEST_FAILED)

    def _deliver(self, service, job):
        with db_lock():
            job = dict(self.con.execute('SELECT * FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone())
            if job['state'] != 'delivering':
                return
            if self._owner(job['user_id']) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
            if job['chat_id']:
                try:
                    service.check_chat(job['owner'], job['chat_id'])
                except ValueError:
                    self._update(job['id'], state='ignored', error='Права доступа изменились до доставки ответа.')
                    return
            parts = json.loads(job['parts'])
            index = job['part_index']
            self._update(job['id'], state='sending')
        try:
            result = self.api.call('imbot.v2.Chat.Message.send', {
                'botId': job['bot_id'], 'dialogId': job['dialog_id'],
                'fields': {'message': parts[index], 'urlPreview': False, 'replyId': job['message_id']},
            })
            if not isinstance(result, dict) or not _positive_id(result.get('id')):
                raise ValueError
        except Exception as error:
            if getattr(error, 'ambiguous', True):
                state, message = 'uncertain', DELIVERY_UNKNOWN
            elif getattr(error, 'retryable', False):
                state, message = 'delivering', 'Битрикс24 ограничил запросы. Повторная доставка выполняется автоматически.'
            else:
                state, message = 'failed', 'Битрикс24 отклонил отправку ответа. Проверьте подключение приложения.'
            self._update(job['id'], state=state, error=message)
            with db_lock():
                self.con.execute('UPDATE bitrix_bot_state SET last_error=? WHERE id=1', (message,))
                self.con.commit()
            return
        index += 1
        self._update(job['id'], part_index=index, state='sent' if index == len(parts) else 'delivering', error='')
