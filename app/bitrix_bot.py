"""Durable private-message bridge between Bitrix24 Bot API v2 and ttkGPT."""
import base64
import hashlib
import json
import re
import threading
import time
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit

from .call_analysis import model_options
from .common import db_lock
from .section_access import visible
from .bitrix_files import BitrixFiles, message_files
from . import gpt_voice


DELIVERY_UNKNOWN = 'Доставка ответа не подтверждена. Команда /retry повторит отправку сохранённого ответа.'
ATTACHMENT_QUESTION = ('Прикреплён файл. Если в предыдущем запросе пользователь просил обработать или преобразовать файл, '
                       'выполни этот запрос с новым вложением. Иначе кратко уточни задачу.')
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


@lru_cache(maxsize=1)
def _name_morphology():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except ImportError:
        return None


@lru_cache(maxsize=512)
def _recipient_names(name):
    parts = name.casefold().replace('ё', 'е').split()
    if not parts:
        return set()
    aliases = {' '.join(parts), parts[0]}
    morph = _name_morphology()
    if morph:
        inflected = []
        for i, part in enumerate(parts):
            tag = 'Name' if i == 0 else 'Surn'
            form = next((p.inflect({'datv'}) for p in morph.parse(part)
                         if tag in p.tag and 'nomn' in p.tag), None)
            if not form:
                break
            inflected.append(form.word.replace('ё', 'е'))
            if i == 0 or len(inflected) == len(parts):
                aliases.add(' '.join(inflected))
    return aliases


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


def _split_reply(text, limit):
    chunks = []
    while len(text) > limit:
        cut = next((i for sep in ("\n\n", "\n", " ") if (i := text.rfind(sep, 0, limit + 1)) > 0), limit)
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def _chat_text(text):
    lines = str(text or '').splitlines()
    out = []
    i = 0
    while i < len(lines):
        if (i + 1 < len(lines) and '|' in lines[i]
                and re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[i+1])):
            headers = [s.strip() for s in lines[i].strip().strip('|').split('|')]
            i += 2
            while i < len(lines) and '|' in lines[i]:
                cells = [s.strip() for s in lines[i].strip().strip('|').split('|')]
                if len(headers) == len(cells):
                    out.extend([cells[0], *(f"{key}: {value}" for key,value in zip(headers[1:], cells[1:])), ""])
                else:
                    out.append(lines[i])
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return re.sub(r'\*\*([^*\n]+)\*\*', r'\1', '\n'.join(out))


def _answer_parts(text, sources, telegram=False):
    urls = {source['url'] for source in (sources or [])[:10] if _source_line(source)}
    # The channel renders these verified links below the answer.
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s<>()[\]]+)\)',
                  lambda match: match[1] if match[2] in urls else match[0], str(text or ''))
    text = _plain(_chat_text(text)).strip() or REQUEST_FAILED
    # 9,000 Unicode code points remain below 20,000 UTF-16 units, including emoji.
    limit = 2000 if telegram else 9000
    parts = _split_reply(text, limit)
    for source in (sources or [])[:10]:
        line = _source_line(source)
        if line and source['url'] not in text.split():
            if telegram:
                line = _plain(source.get('title') or 'Источник')[:200] + '\n' + source['url']
            if len(parts[-1]) + len(line) + 2 <= limit:
                parts[-1] += '\n\n' + line
            else:
                parts.append(line)
    return parts


class BitrixBot:
    def __init__(self, con, config, api, gpt_factory):
        self.con, self.config, self.api, self.gpt_factory = con, config, api, gpt_factory
        self.files = BitrixFiles(api, config)
        self._activation_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._submitting_owners = set()
        self._lifecycle_lock = threading.Lock()
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._indicators = {}
        self._telegram_drafts = {}
        self._thread = None
        with db_lock():
            con.executescript('''
                CREATE TABLE IF NOT EXISTS bitrix_bot_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), bot_id INTEGER,
                    next_offset INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO bitrix_bot_state(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS bot_task_delete (
                    bot INTEGER, sender INTEGER, dialog TEXT, owner TEXT, task INTEGER, expires REAL,
                    PRIMARY KEY(bot,sender,dialog,owner));
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
                CREATE TABLE IF NOT EXISTS bitrix_bot_imports (
                    job_id INTEGER NOT NULL, file_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
                    document_id TEXT, name TEXT NOT NULL, content_hash TEXT NOT NULL,
                    baseline TEXT NOT NULL, source_hash TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id,file_id));
            ''')
            columns = {row[1] for row in con.execute('PRAGMA table_info(bitrix_bot_jobs)')}
            for name, declaration in (('attachments', "TEXT NOT NULL DEFAULT '[]'"), ('document_baseline', 'TEXT')):
                if name not in columns:
                    con.execute('ALTER TABLE bitrix_bot_jobs ADD COLUMN ' + name + ' ' + declaration)
            import_columns = {row[1] for row in con.execute('PRAGMA table_info(bitrix_bot_imports)')}
            if 'source_hash' not in import_columns:
                con.execute("ALTER TABLE bitrix_bot_imports ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''")
            con.execute("UPDATE bitrix_bot_jobs SET state='uncertain',error=? WHERE state='sending'", (DELIVERY_UNKNOWN,))
            con.execute("UPDATE bitrix_bot_jobs SET state='delivering',parts=? WHERE state='writing'",
                        (json.dumps([{'kind': 'text', 'text': 'Битрикс24 не подтвердил результат команды. Проверьте диалог перед повторной отправкой.'}]),))
            pending = [dict(row) for row in con.execute("SELECT * FROM bitrix_bot_jobs WHERE state='submitting'")]
            for job in pending:
                self._reconcile_submit(job)
            con.commit()

        from .bitrix_task_flow import TaskFlow
        self.tasks = TaskFlow(self)

    def status(self):
        with db_lock():
            state = dict(self.con.execute('SELECT * FROM bitrix_bot_state WHERE id=1').fetchone())
            counts = dict(self.con.execute('SELECT state,count(*) FROM bitrix_bot_jobs WHERE bot_id=? GROUP BY state',
                                           (state['bot_id'],)))
            latest_error = self.con.execute("""SELECT error FROM bitrix_bot_jobs WHERE bot_id=?
                AND state IN ('uncertain','failed','delivering') AND error<>'' ORDER BY id DESC LIMIT 1""",
                                            (state['bot_id'],)).fetchone()
        return {'bot_id': state['bot_id'], 'queued': counts.get('queued', 0),
                'running': sum(counts.get(key, 0) for key in ('task_parsing', 'writing', 'submitting', 'running', 'delivering', 'sending')),
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
            self._activity_thread = threading.Thread(target=self._activity_loop, daemon=True, name='ttkgpt-activity')
            self._activity_thread.start()

    def stop(self):
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(5)
        activity = getattr(self, '_activity_thread', None)
        if activity and activity is not threading.current_thread():
            activity.join(5)

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
            self.process_jobs(bot_id)
        finally:
            self._tick_lock.release()

    def process_jobs(self, bot_id):
        with db_lock():
            jobs = [dict(row) for row in self.con.execute('''
                SELECT j.* FROM bitrix_bot_jobs j
                WHERE j.bot_id=? AND j.state IN ('queued','submitting','running','delivering')
                  AND NOT EXISTS (SELECT 1 FROM bitrix_bot_jobs earlier
                      WHERE earlier.bot_id=j.bot_id AND earlier.user_id=j.user_id AND earlier.id<j.id
                        AND earlier.state IN ('queued','task_parsing','writing','submitting','running','delivering','sending'))
                ORDER BY j.id LIMIT 100
            ''', (bot_id,))]
        for job in jobs:
            if self._stopped.is_set() and self._thread is threading.current_thread():
                break
            self._process(job)

    def job_owner(self, job):
        if job['bot_id'] < 0 and not self.telegram.bound(job):
            return None
        return self._owner(job['user_id'])

    def _owner(self, user_id):
        matches = [employee for employee in self.config.get('employees', [])
                   if _positive_id(employee.get('b24_user_id')) == user_id]
        if len(matches) == 1 and isinstance(matches[0].get('login'), str) and matches[0]['login']:
            employee = matches[0]
            with db_lock():
                if employee.get('role') != 'manager' and 'gpt' not in visible(self.con, employee.get('department')):
                    return None
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
        attachments = message_files(data)
        text = message.get('text')
        if text is None and attachments:
            text = ''
        if not owner or not isinstance(text, str) or not text.strip() and not attachments:
            return None
        question = text.strip() or ATTACHMENT_QUESTION
        message_params = message.get('params') or {}
        if (message.get('forward') or message.get('forwardId') or
                isinstance(message_params, dict) and any(k.upper().startswith('FORWARD') for k in message_params)):
            question = 'Пересланное сообщение:\n' + question
        return (bot_id, event['eventId'], message_id, user_id, str(user_id), owner, question,
                json.dumps(attachments, ensure_ascii=False))

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
                                (bot_id,event_id,message_id,user_id,dialog_id,owner,question,attachments)
                                VALUES (?,?,?,?,?,?,?,?)''', incoming)
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
        if job['baseline_id'] < 0:
            # New submissions commit the exact reply ID with the messages themselves.
            self.con.execute("UPDATE bitrix_bot_jobs SET state='queued' WHERE id=?", (job['id'],))
            return
        matches = self.con.execute('''SELECT id FROM gpt_messages WHERE chat_id=? AND id>? AND role='assistant'
                                      ORDER BY id LIMIT 2''', (job['chat_id'], job['baseline_id'])).fetchall()
        if len(matches) == 1:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='running',assistant_id=? WHERE id=?", (matches[0][0], job['id']))
        elif not matches:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='queued' WHERE id=?", (job['id'],))
        else:
            self.con.execute("UPDATE bitrix_bot_jobs SET state='ignored',error=? WHERE id=?", (REQUEST_FAILED, job['id']))

    def _chat(self, service, job, reset=False):
        if reset:
            return service.create_chat(job['owner'], shared=True)['id']
        return service.shared_chat(job['owner'])['id']

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

    def _ready(self, job, text, sources=None, documents=None):
        parts = [{'kind': 'file', 'document_id': document['id']} for document in documents or []]
        parts.extend({'kind': 'text', 'text': text} for text in _answer_parts(text, sources, telegram=job['bot_id'] < 0))
        with db_lock():
            current = self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()
            if current[0] not in ('queued', 'running'):
                return
            self._update(job['id'], state='delivering', parts=json.dumps(parts, ensure_ascii=False), part_index=0, error='')
            job.update(state='delivering', parts=json.dumps(parts, ensure_ascii=False), part_index=0)

    def _command(self, service, job):
        if self._file_to_notes(service, job):
            return True
        if self._delete_command(job):
            return True
        if self.tasks.handle(job):
            return True
        if self._write_command(job):
            return True
        command = job['question'].casefold()
        if command in ('/new', 'новый чат'):
            self._chat(service, job, reset=True)
            self._ready(job, 'Новый общий чат открыт в дашборде, Битрикс24 и Telegram.')
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

    def _file_to_notes(self, service, job):
        match = re.fullmatch(
            r'(?:отправь|перешли|скинь|сохрани|добавь)\s+(?:(?:его|её|ее|это|этот файл|этот документ|файл|документ)\s+)?'
            r'(?:[«"]([^«»"\n]+)[»"]\s+)?(?:мне\s+)?в\s+(?:мои\s+)?заметки(?:\s+в\s+(?:б24|битрикс(?:е|24)?))?[.!]?',
            job['question'].strip(), re.I)
        if not match:
            return False
        try:
            chat = self._chat(service, job)
            with db_lock():
                last = self.con.execute("""SELECT parts,assistant_id FROM bitrix_bot_jobs WHERE
                    owner=? AND chat_id=? AND state='sent' AND id<?
                    AND EXISTS(SELECT 1 FROM json_each(bitrix_bot_jobs.parts)
                        WHERE json_each.type='object' AND json_each.value ->> '$.kind'='file')
                    ORDER BY id DESC LIMIT 1""", (job['owner'],chat,job['id'])).fetchone()
                outputs = self.con.execute("""SELECT o.message_id,o.document_id FROM gpt_document_outputs o
                    JOIN gpt_messages m ON m.id=o.message_id AND m.chat_id=o.chat_id
                    WHERE o.chat_id=? AND m.status='completed' AND o.message_id>=?
                    ORDER BY o.message_id DESC""", (chat,(last['assistant_id'] or 0) if last else 0)).fetchall()
            if outputs:
                ids = [r['document_id'] for r in outputs if r['message_id']==outputs[0]['message_id']]
            else:
                ids = [p['document_id'] for p in json.loads(last[0]) if isinstance(p,dict) and p.get('kind')=='file'] if last else []
            files = [d for d in service.documents.listing(chat) if d['id'] in ids]
            if match[1]:
                files = [d for d in files if d['name'].casefold()==match[1].casefold()]
            if len(files)!=1:
                self._ready(job, 'В последнем ответе с файлами несколько документов. Напишите: Отправь файл «имя файла» в мои заметки.' if files else 'В этом чате не нашёл подходящего готового файла.')
                return True
            doc = service.document_download(job['owner'], chat, files[0]['id'])
            with db_lock():
                if self.job_owner(job) != job['owner']:
                    self._update(job['id'],state='ignored')
                    return True
                claimed = self.con.execute("UPDATE bitrix_bot_jobs SET state='writing' WHERE id=? AND state='queued'",(job['id'],)).rowcount
                self.con.commit()
            if not claimed:
                return True
            self.api.write_personal_note(job['user_id'], '', document=doc)
            reply = f'Файл «{doc["name"]}» отправлен в ваши заметки Битрикс24.'
        except ValueError as error:
            reply = str(error)
        except Exception:
            reply = 'Не удалось подтвердить отправку файла. Проверьте «Мои заметки» в Битрикс24.'
        self._update(job['id'],state='delivering',parts=json.dumps([reply],ensure_ascii=False),part_index=0)
        job['state']='delivering'
        return True

    def _delete_command(self, job):
        text = job['question'].strip().casefold()
        scope = (job['bot_id'], job['user_id'], job['dialog_id'], job['owner'])
        where = 'bot=? AND sender=? AND dialog=? AND owner=?'
        match = re.fullmatch(r'(удали(?:те)?\s+задачу|подтверждаю\s+удаление(?:\s+задачи)?)\s*№?\s*([1-9]\d{0,12})[.!]?', text)
        with db_lock():
            pending = self.con.execute('SELECT task,expires FROM bot_task_delete WHERE '+where, scope).fetchone()
            self.con.execute('DELETE FROM bot_task_delete WHERE '+where, scope)
            self.con.commit()
        if not match:
            if pending and text in ('отмена','/cancel'):
                self._ready(job, 'Удаление отменено.')
                return True
            return False
        task = int(match[2])
        if match[1].startswith('удали'):
            try:
                card = self.api.read_for_user(job['user_id'], 'tasks.task.get', {'taskId':task,'select':['ID','TITLE']})
                item = card['result']['task']
                if str(item.get('id') or item.get('ID')) != str(task):
                    raise ValueError('Не удалось проверить номер задачи.')
                title = _plain(item.get('title') or item.get('TITLE') or '')[:160]
            except (ValueError, RuntimeError, KeyError, TypeError) as error:
                self._ready(job, str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Не удалось прочитать карточку задачи.')
                return True
            with db_lock():
                self.con.execute('INSERT OR REPLACE INTO bot_task_delete VALUES(?,?,?,?,?,?)', (*scope,task,time.time()+300))
                self._ready(job, f'Удалить задачу №{task} «{title}»? В течение 5 минут напишите «Подтверждаю удаление {task}» или «Отмена».')
            return True
        if not pending or pending[0] != task or pending[1] <= time.time():
            self._ready(job, f'Нет действующего запроса. Начните с «Удали задачу {task}».')
            return True
        with db_lock():
            if self.job_owner(job) != job['owner']:
                self._update(job['id'], state='ignored')
                return True
            self._update(job['id'], state='writing')
        try:
            self.api.delete_user_task(job['user_id'],task)
            reply = f'Задача №{task} удалена.'
        except RuntimeError as error:
            reply = str(error)
        except Exception:
            reply = 'Результат удаления неизвестен. Проверьте карточку задачи.'
        self._update(job['id'], state='delivering', parts=json.dumps([reply],ensure_ascii=False),part_index=0)
        return True

    def _write_command(self, job):
        question = job['question'].strip()
        flags = re.IGNORECASE | re.DOTALL
        bot_link = re.fullmatch(
            r'(?:кинь|скинь|отправь|пришли|перешли)\s+ссылку\s+'
            r'([^:\n!?]{1,100}?)\s+в\s+(?:битрикс(?:е|24)?|б24)\s+на\s+'
            r'(?:нашего\s+)?(?:тг|телеграм|telegram)[ -]?бота[.!]?', question, flags)
        note = re.fullmatch(r'(?:/note\s+|(?:запиши|сохрани|добавь|напиши)\s+в\s+(?:мои\s+)?заметки\s*:\s*)(.+)', question, flags)
        quoted = re.fullmatch(r'(?:напиши|запиши|сохрани|добавь)(?:\s+сообщение)?\s+(?:"([^"]+)"|«([^»]+)»)\s+в\s+мои\s+заметки[.!]?', question, flags)
        channel = r'в\s+(?:битрикс(?:е|24)?|б24)'
        verb = r'(?:/send|(?:напиши|отправь|передай|скинь|кинь)(?:\s+сообщение)?)\s+(?:' + channel + r'\s+)?'
        direct = re.fullmatch(verb + r'([^:\n"«»“”]{1,120}?)\s+(?:"(.+)"|«(.+)»|“(.+)”)[.!]?', question, flags)
        if not direct:
            direct = re.fullmatch(verb + r'([^:\n"«»“”]{1,120}):\s*(.+)', question, flags)
        if not direct:
            direct = re.fullmatch(verb + r'([\w-]+(?:\s+[\w-]+){0,2}?)\s*(?:,\s*что\s+|\s+что\s+|\s+' + channel + r'\s+)(.+)', question, flags)
        if job['bot_id'] < 0 and (note or quoted):
            return False
        if not (note or quoted or direct or bot_link):
            return False
        recipient = None
        if note or quoted:
            text = note[1] if note else quoted[1] or quoted[2]
        else:
            target = ' '.join((bot_link or direct)[1].casefold().split())
            target = re.sub(r'\s+' + channel + r'$', '', target).replace('ё','е')
            if bot_link:
                telegram = getattr(self, 'telegram', None)
                if not telegram or not telegram.username:
                    self._ready(job, 'Telegram-бот пока не подключён.')
                    return True
                text = (f'ttkGPT в Telegram: https://t.me/{telegram.username}\n'
                        'Для привязки откройте ttkGPT в дашборде и нажмите «Подключить Telegram».')
            else:
                text = next(s for s in direct.groups()[1:] if s is not None)
            matches = [e for e in self.config.get('employees', []) if _positive_id(e.get('b24_user_id'))
                       and (target in (str(e['b24_user_id']), str(e.get('login') or '').casefold())
                            or target.replace('ё','е') in _recipient_names(e.get('name') or ''))]
            if job['bot_id'] < 0 and not bot_link:
                try:
                    local_ids = {int(e['b24_user_id']) for e in matches}
                    matches = self._portal_recipients(target, local_ids)
                except Exception:
                    self._ready(job, 'Не удалось проверить адресата в справочнике Битрикс24. Сообщение не отправлено.')
                    return True
            if len(matches) != 1:
                self._ready(job, 'Укажите точное имя и фамилию из справочника или ID Б24. Формат: /send ID: текст')
                return True
            recipient = matches[0]
        if not text.strip() or len(text) > 6000:
            self._ready(job, 'Укажите текст от 1 до 6 000 символов.')
            return True
        with db_lock():
            if self.job_owner(job) != job['owner']:
                self._update(job['id'], state='ignored')
                return True
            claimed = self.con.execute("UPDATE bitrix_bot_jobs SET state='writing' WHERE id=? AND state='queued'", (job['id'],)).rowcount
            self.con.commit()
        if not claimed:
            return True
        try:
            if recipient:
                source_bot = job['bot_id'] if job['bot_id'] > 0 else self.status()['bot_id']
                if not source_bot:
                    raise ValueError('Чат-бот Битрикс24 не подключён.')
                result = self.api.call('imbot.v2.Chat.Message.send', {
                    'botId': source_bot, 'dialogId': str(recipient['b24_user_id']),
                    'fields': {'message': _plain(text), 'urlPreview': False}})
                if not isinstance(result, dict) or not _positive_id(result.get('id')):
                    raise RuntimeError()
                reply = 'Сообщение отправлено: ' + _plain(recipient.get('name') or str(recipient['b24_user_id'])) + '.'
            else:
                self.api.write_personal_note(job['user_id'], _plain(text))
                reply = 'Записано в ваши «Мои заметки».'
        except ValueError as error:
            reply = str(error) if not recipient and type(error) is ValueError else REQUEST_FAILED
        except Exception:
            reply = 'Битрикс24 не подтвердил отправку. Проверьте диалог перед повторной командой.'
        self._update(job['id'], state='delivering', parts=json.dumps([{'kind':'text','text':reply}]), part_index=0)
        job['state'] = 'delivering'
        return True

    def _portal_recipients(self, target, local_ids):
        criteria = {'ACTIVE': True, 'USER_TYPE':'employee'}
        if target.isdecimal():
            criteria['ID'] = target
        start, seen, matches = 0, set(), []
        while True:
            rows = self.api.call('user.get', {'filter':criteria, 'select':['ID','NAME','LAST_NAME'],
                                              'sort':'ID','order':'ASC','start':start})
            if not isinstance(rows,list):
                raise ValueError('Справочник недоступен')
            for row in rows:
                uid = _positive_id(row.get('ID'))
                if not uid or uid in seen:
                    raise ValueError('Неполный справочник')
                seen.add(uid)
                name = ' '.join(str(row.get(field) or '') for field in ('NAME','LAST_NAME')).strip()
                if uid in local_ids or str(uid) == target or target in _recipient_names(name):
                    matches.append({'b24_user_id':uid,'name':name})
            if len(rows) < 50 or len(matches)>1:
                return matches
            start += len(rows)

    def _activity_loop(self):
        while not self._stopped.is_set():
            try:
                self._refresh_activity()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Не удалось проверить состояние индикаторов ttkGPT')
            self._stopped.wait(1)

    def _refresh_activity(self):
        with db_lock():
            jobs = [dict(row) for row in self.con.execute("""
                SELECT j.id,j.bot_id,j.dialog_id,j.user_id,j.owner,coalesce(m.content,'') AS content
                FROM bitrix_bot_jobs j LEFT JOIN gpt_messages m ON j.assistant_id=m.id
                WHERE j.state IN ('queued','task_parsing','submitting','writing','running')
                  AND (j.state!='running' OR m.status='running')
                ORDER BY j.id""")]
        active = set()
        for job in jobs:
            key = (job['bot_id'], job['dialog_id'])
            if key not in active and self.job_owner(job) == job['owner']:
                active.add(key)
                self._notify_activity(key, job)
        for key in self._indicators.keys() - active:
            self._indicators.pop(key)
            self._telegram_drafts.pop(key, None)

    def _notify_activity(self, key, job):
        stage = job['content'].removeprefix('Агент · ')
        action = 'THINKING'
        for prefix, code in [('Готовлю ответ', 'GENERATING'), ('Проверяю', 'CHECKING'),
                             ('Ищу', 'SEARCHING'), ('Читаю', 'READING_DOCS'), ('Выполняю расчёт', 'CALCULATING')]:
            if stage.startswith(prefix):
                action = code
                break
        now = time.monotonic()
        telegram = job['bot_id'] < 0
        if telegram:
            labels = {'THINKING':'Думаю…', 'GENERATING':'Готовлю ответ…',
                      'CHECKING':'Проверяю данные…', 'SEARCHING':'Ищу информацию…',
                      'READING_DOCS':'Читаю документ…', 'CALCULATING':'Выполняю расчёт…'}
            label = stage if stage in ('Проверяю данные 1С…', 'Проверяю данные Битрикс24…',
                                       'Ищу звонки…', 'Собираю отчёт…', 'Изучаю изображение…') else labels[action]
            if re.fullmatch(r'(?:Ищу компании по заданным условиям|Проверяю ИНН и общую почту)\. Подтверждено \d{1,2} из \d{1,2}…', stage):
                label = stage
            identity = (job['id'], label)
            previous_draft, renewed = self._telegram_drafts.get(key, (None, 0))
            if identity != previous_draft or now - renewed >= 20:
                self._telegram_drafts[key] = (identity, now)
                try:
                    self.telegram.call('sendMessageDraft', {'chat_id':int(job['dialog_id']),
                                       'draft_id':job['id'], 'text':label})
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning('Telegram не подтвердил показ этапа ttkGPT')
        action = 'typing' if telegram else action
        previous, sent = self._indicators.get(key, (None, 0))
        if previous == action and now - sent < (3 if telegram else 7):
            return
        self._indicators[key] = (action, now)
        try:
            if telegram:
                self.telegram.call('sendChatAction', {'chat_id':job['dialog_id'], 'action':'typing'})
                return
            params = {
                'botId': job['bot_id'], 'dialogId': job['dialog_id'],
                'duration': 12}
            try:
                result = self.api.call('imbot.v2.Chat.InputAction.notify', {
                    **params, 'statusMessageCode': 'IMBOT_AGENT_ACTION_' + action})
                if result is True or isinstance(result, dict) and result.get('result') is True:
                    return
            except Exception:
                pass
            self.api.call('imbot.v2.Chat.InputAction.notify', params)
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Индикатор ttkGPT недоступен в %s', 'Telegram' if telegram else 'Б24')

    def _process(self, job):
        with db_lock():
            job = dict(self.con.execute('SELECT * FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone())
            if job['state'] not in ('queued', 'submitting', 'running', 'delivering'):
                return
            if self.job_owner(job) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
        service = self.gpt_factory()
        if job['state'] == 'queued':
            if not json.loads(job['attachments']) and self._command(service, job):
                if job['state'] == 'delivering':
                    self._deliver(service, job)
                return
            if len(job['question']) > 6000:
                self._ready(job, 'Введите вопрос длиной до 6 000 символов.')
            else:
                with db_lock():
                    if job['owner'] in self._submitting_owners:
                        return
                    self._submitting_owners.add(job['owner'])
                try:
                    self._submit(service, job)
                finally:
                    with db_lock():
                        self._submitting_owners.discard(job['owner'])
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
                self._ready(job, message['content'], message['sources'], self._output_documents(job))
            elif message['status'] == 'error' and message.get('content') in (
                    'Codex попытался использовать недоступный инструмент',
                    'Общий поиск в интернете недоступен в этом чате.'):
                if re.search(r'интернет|\bсайт\w*|\bв сети\b', job['question'], re.I) or message['content'].startswith('Общий поиск'):
                    self._ready(job, 'Встроенный инструмент не выполнил поиск. Для проверки открытых источников в ttkGPT предусмотрен отдельный веб-поиск.')
                else:
                    self._ready(job, 'Для ответа потребовался инструмент, недоступный этому боту. Могу работать с данными Битрикс24, 1С и файлами этого чата.')
            else:
                self._ready(job, REQUEST_FAILED)
        if job['state'] == 'delivering':
            self._deliver(service, job)

    def _submit(self, service, job):
        with db_lock():
            if self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()[0] != 'queued':
                return
            if self.job_owner(job) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
            chat_id = job['chat_id'] or self._chat(service, job)
            if job['chat_id']:
                try:
                    service.check_chat(job['owner'], chat_id)
                except ValueError:
                    self._update(job['id'],state='ignored',error='Чат запроса недоступен с текущими правами.')
                    return
            if chat_id in service.active or len(service.active) >= 2:
                return
            self._update(job['id'], chat_id=chat_id)
            job['chat_id'] = chat_id
        if not self._import_files(service, job):
            return
        with db_lock():
            if self.job_owner(job) != job['owner']:
                self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                return
            try:
                service.check_chat(job['owner'], chat_id)
            except ValueError:
                self._update(job['id'], state='ignored', error='Права доступа изменились до обработки вложений.')
                return
            if chat_id in service.active or len(service.active) >= 2:
                return
            baseline = -1
            documents = [row[0] for row in self.con.execute('SELECT id FROM gpt_documents WHERE chat_id=?', (chat_id,))]
            self._update(job['id'], state='submitting', chat_id=chat_id, baseline_id=baseline,
                         document_baseline=json.dumps(documents))
            try:
                service.submit(job['owner'], chat_id, job['question'], bot_job_id=job['id'], **self._selection(job['owner']))
            except Exception:
                # submit() commits its messages internally. Recover a committed turn before any retry.
                recovery = {**job, 'chat_id': chat_id, 'baseline_id': baseline}
                if self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?',(job['id'],)).fetchone()[0] == 'submitting':
                    self._reconcile_submit(recovery)
                self.con.commit()
                current = self.con.execute('SELECT state FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone()[0]
                if current == 'queued':
                    self._ready(job, REQUEST_FAILED)

    def _import_files(self, service, job):
        for reference in json.loads(job['attachments']):
            with db_lock():
                if self.job_owner(job) != job['owner']:
                    self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                    return False
                saved = self.con.execute('SELECT * FROM bitrix_bot_imports WHERE job_id=? AND file_id=?',
                                         (job['id'], reference['id'])).fetchone()
                if saved and saved['chat_id'] == job['chat_id'] and saved['document_id']:
                    existing = self.con.execute('SELECT 1 FROM gpt_documents WHERE chat_id=? AND id=?',
                                                (job['chat_id'], saved['document_id'])).fetchone()
                    if existing:
                        continue
            try:
                # The OAuth download can wait on the network. It runs outside db_lock().
                if job['bot_id'] < 0:
                    name, raw = self.telegram.download(reference)
                else:
                    name, raw = self.files.download(job['bot_id'], job['dialog_id'], reference)
                if name.lower().endswith(tuple(gpt_voice.FORMATS)):
                    speech = gpt_voice.recognize(name, raw)
                    caption = job['question'] if job['question'] != ATTACHMENT_QUESTION else ''
                    job['question'] = (caption + '\n' + speech).strip()
                    if len(job['question']) > 6000:
                        raise ValueError('Расшифровка слишком длинная. Разделите сообщение.')
                    continue
                source_hash = hashlib.sha256(raw).hexdigest()
                if saved and saved['chat_id'] == job['chat_id'] and saved['source_hash'] == source_hash:
                    with db_lock():
                        if self.job_owner(job) != job['owner']:
                            self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                            return False
                        service.check_chat(job['owner'], job['chat_id'])
                        previous = set(json.loads(saved['baseline']))
                        candidates = self.con.execute('''SELECT id,content FROM gpt_documents
                            WHERE chat_id=? AND name=? AND parent_id IS NULL ORDER BY rowid''',
                            (job['chat_id'], saved['name'])).fetchall()
                        recovered = next((row['id'] for row in candidates if row['id'] not in previous
                                          and hashlib.sha256(row['content']).hexdigest() == saved['content_hash']), None)
                        if recovered:
                            self.con.execute('UPDATE bitrix_bot_imports SET document_id=? WHERE job_id=? AND file_id=?',
                                             (recovered, job['id'], reference['id']))
                            self.con.commit()
                            continue
                if name.lower().endswith(('.doc', '.xls', '.ppt', '.rtf', '.odt', '.ods', '.odp', '.html', '.htm')):
                    from .gpt_office import normalize
                    name, raw = normalize(name, raw)
                from .gpt_images import FORMATS, normalize
                expected = normalize(raw)[0] if name.lower().endswith(tuple(FORMATS)) else raw
                digest = hashlib.sha256(expected).hexdigest()
                with db_lock():
                    if self.job_owner(job) != job['owner']:
                        self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                        return False
                    service.check_chat(job['owner'], job['chat_id'])
                    baseline = [row[0] for row in self.con.execute('SELECT id FROM gpt_documents WHERE chat_id=?', (job['chat_id'],))]
                    if saved and saved['chat_id'] == job['chat_id'] and saved['content_hash'] == digest:
                        previous = set(json.loads(saved['baseline']))
                        candidates = self.con.execute('''SELECT id,content FROM gpt_documents
                            WHERE chat_id=? AND name=? AND parent_id IS NULL ORDER BY rowid''', (job['chat_id'], name)).fetchall()
                        recovered = next((row['id'] for row in candidates if row['id'] not in previous
                                          and hashlib.sha256(row['content']).hexdigest() == digest), None)
                        if recovered:
                            self.con.execute('UPDATE bitrix_bot_imports SET document_id=? WHERE job_id=? AND file_id=?',
                                             (recovered, job['id'], reference['id']))
                            self.con.commit()
                            continue
                    self.con.execute('''INSERT OR REPLACE INTO bitrix_bot_imports
                        (job_id,file_id,chat_id,document_id,name,content_hash,baseline,source_hash)
                        VALUES (?,?,?,NULL,?,?,?,?)''',
                        (job['id'], reference['id'], job['chat_id'], name, digest, json.dumps(baseline), source_hash))
                    self.con.commit()
                document = service.document_upload(job['owner'], job['chat_id'], name,
                                                   base64.b64encode(raw).decode('ascii'), protected=True)
                with db_lock():
                    service.check_chat(job['owner'], job['chat_id'])
                    if self.job_owner(job) != job['owner']:
                        self._update(job['id'], state='ignored', error='Связь пользователя с сотрудником изменилась.')
                        return False
                    self.con.execute('UPDATE bitrix_bot_imports SET document_id=? WHERE job_id=? AND file_id=?',
                                     (document['id'], job['id'], reference['id']))
                    self.con.commit()
            except Exception as error:
                label = _plain(reference.get('name') or 'вложение')[:120]
                detail = _plain(str(error))[:250] + '. ' if isinstance(error, ValueError) else ''
                self._ready(job, 'Не удалось загрузить файл «' + label + '». ' + detail + 'Прикрепите его повторно. Запрос ещё не обработан.')
                return False
        return True

    def _output_documents(self, job):
        with db_lock():
            bindings = self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_document_outputs'").fetchone()
            if bindings:
                bound = self.con.execute('SELECT 1 FROM gpt_document_outputs WHERE chat_id=? AND message_id=? LIMIT 1',
                                         (job['chat_id'], job['assistant_id'])).fetchone()
                if bound:
                    return [dict(row) for row in self.con.execute('''SELECT d.id,d.parent_id
                        FROM gpt_document_outputs output JOIN gpt_documents d
                          ON d.id=output.document_id AND d.chat_id=output.chat_id
                        WHERE output.chat_id=? AND output.message_id=? AND d.parent_id IS NOT NULL
                        ORDER BY d.created,d.rowid''', (job['chat_id'], job['assistant_id']))]
            if job['document_baseline'] is None:
                return []
            turn = self.con.execute('''SELECT created,finished FROM gpt_messages
                WHERE chat_id=? AND id=? AND role='assistant' AND status='completed' ''',
                                    (job['chat_id'], job['assistant_id'])).fetchone()
            if not turn or not turn['finished']:
                return []
            candidates = [dict(row) for row in self.con.execute('''SELECT id,parent_id FROM gpt_documents
                WHERE chat_id=? AND parent_id IS NOT NULL AND created>=? AND created<=?
                ORDER BY created,rowid''', (job['chat_id'], turn['created'], turn['finished']))]
        # Only descendants of the documents present when this exact turn began belong to its output.
        parents = set(json.loads(job['document_baseline']))
        output = []
        for document in candidates:
            if document['id'] not in parents and document['parent_id'] in parents:
                output.append(document)
                parents.add(document['id'])
        return output

    def _deliver(self, service, job):
        with db_lock():
            job = dict(self.con.execute('SELECT * FROM bitrix_bot_jobs WHERE id=?', (job['id'],)).fetchone())
            if job['state'] != 'delivering':
                return
            if self.job_owner(job) != job['owner']:
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
            part = parts[index]
            # Already queued textual replies from the first bridge version remain valid.
            if isinstance(part, str):
                part = {'kind': 'text', 'text': part}
            if part['kind'] == 'file':
                try:
                    document = service.document_download(job['owner'], job['chat_id'], part['document_id'])
                except ValueError:
                    self._update(job['id'], state='failed', error='Сохранённый файл недоступен для отправки.')
                    return
                method = 'imbot.v2.File.upload'
                fields = {'name': document['name'], 'content': document['content'], 'message': ''}
            else:
                method = 'imbot.v2.Chat.Message.send'
                fields = {'message': part['text'], 'urlPreview': False, 'replyId': job['message_id']}
            if job['bot_id'] < 0 and part['kind'] == 'text' and len(fields['message']) > 2000:
                text = fields['message']
                parts[index:index+1] = [{'kind':'text','text':chunk} for chunk in _split_reply(text, 2000)]
                self._update(job['id'], parts=json.dumps(parts, ensure_ascii=False))
                fields['message'] = parts[index]['text']
            self._update(job['id'], state='sending')
        try:
            if job['bot_id'] < 0:
                self.telegram.deliver(job, part, fields)
                self._update(job['id'], part_index=index+1, state='sent' if index+1 == len(parts) else 'delivering', error='')
                return
            result = self.api.call(method, {
                'botId': job['bot_id'], 'dialogId': job['dialog_id'],
                'fields': fields,
            })
            identity = result.get('file', {}).get('id') if method == 'imbot.v2.File.upload' else result.get('id')
            if not isinstance(result, dict) or not _positive_id(identity):
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
