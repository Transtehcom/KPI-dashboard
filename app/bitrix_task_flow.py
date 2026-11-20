"""Private Bitrix task drafts scoped to the verified sender."""
import json
import threading

from .common import db_lock
from .bitrix_app import BitrixAppError
from .crm_links import portal_origin
from . import bitrix_task_parse


class TaskFlow:
    def __init__(self, bot):
        self.bot = bot
        self.slots = threading.BoundedSemaphore(2)
        with db_lock():
            bot.con.execute("""CREATE TABLE IF NOT EXISTS bitrix_task_drafts(
                bot INTEGER, sender INTEGER, owner TEXT, messages TEXT NOT NULL,
                PRIMARY KEY(bot,sender,owner))""")
            bot.con.execute("UPDATE bitrix_bot_jobs SET state='queued' WHERE state='task_parsing'")
            bot.con.commit()

    def handle(self, job):
        text = job['question'].strip()
        key = (job['bot_id'], job['user_id'], job['owner'])
        with db_lock():
            draft = self.bot.con.execute("SELECT messages FROM bitrix_task_drafts WHERE bot=? AND sender=? AND owner=?", key).fetchone()
        if draft and text.casefold() in ('отмена', 'отмени', '/cancel', '/new', 'новый чат'):
            with db_lock():
                self.clear(key)
                self.bot.con.commit()
            if text.casefold() in ('/new', 'новый чат'):
                return False
            self.bot._ready(job, 'Постановка задачи отменена.')
            return True
        starts = bitrix_task_parse.starts(text)
        if not starts and not draft:
            return False
        messages = [] if starts else json.loads(draft[0])
        if len(text) > 6000 or sum(map(len, messages)) + len(text) > 18000:
            self.bot._ready(job, 'Запрос слишком длинный. Начните заново словами «Поставь задачу».')
            return True
        if not self.slots.acquire(blocking=False):
            return True
        self.bot._update(job['id'], state='task_parsing')
        threading.Thread(target=self.process, args=(job, key, messages + [text]), daemon=True).start()
        return True

    def clear(self, key):
        self.bot.con.execute('DELETE FROM bitrix_task_drafts WHERE bot=? AND sender=? AND owner=?', key)

    def resolve(self, names, sender):
        from .bitrix_bot import _recipient_names, _positive_id
        resolved = []
        for name in names:
            alias = ' '.join(name.casefold().replace('ё', 'е').split())
            if alias in ('мне', 'меня', 'я', 'себе'):
                matches = [sender]
            else:
                matches = []
                for employee in self.bot.config.get('employees', []):
                    uid = _positive_id(employee.get('b24_user_id'))
                    if uid and alias in (_recipient_names(employee.get('name') or '') | {str(uid), employee['login'].casefold()}):
                        matches.append(uid)
            if len(matches) != 1:
                raise ValueError(f'Уточни имя и фамилию сотрудника «{name}» или укажи его ID Б24.')
            if matches[0] not in resolved:
                resolved.append(matches[0])
        return resolved

    def process(self, job, key, messages):
        writing = False
        try:
            parsed = bitrix_task_parse.parse(messages, self.bot.config, job['owner'])
            question = parsed.get('question') or ''
            try:
                people = self.resolve(parsed['responsible'], job['user_id'])
                helpers = self.resolve(parsed['accomplices'], job['user_id'])
                observers = self.resolve(parsed['auditors'], job['user_id'])
            except ValueError as error:
                question = str(error)
            if not question and (not people or not parsed['title']):
                question = ('Кому поставить задачу и что нужно сделать?' if not people and not parsed['title']
                            else 'Кому поставить задачу?' if not people else 'Что нужно сделать?')
            if question:
                with db_lock():
                    self.bot.con.execute('INSERT OR REPLACE INTO bitrix_task_drafts VALUES (?,?,?,?)',
                                         (*key, json.dumps(messages, ensure_ascii=False)))
                    self.reply(job, question)
                return
            fields = {'TITLE': parsed['title'], 'RESPONSIBLE_ID': people[0]}
            accomplices = [i for i in dict.fromkeys(people[1:] + helpers) if i != people[0]]
            if accomplices:
                fields['ACCOMPLICES'] = accomplices
            if observers:
                fields['AUDITORS'] = observers
            if parsed.get('deadline'):
                fields['DEADLINE'] = parsed['deadline']
            if parsed.get('description'):
                fields['DESCRIPTION'] = parsed['description']
            with db_lock():
                if self.bot.job_owner(job) != job['owner']:
                    self.bot._update(job['id'], state='ignored')
                    return
                self.clear(key)
                self.bot._update(job['id'], state='writing')
            writing = True
            identity = self.bot.api.create_user_task(job['user_id'], fields)
            origin = portal_origin(self.bot.config)
            self.reply(job, f'Задача №{identity} создана. {fields["TITLE"]}\n{origin}/company/personal/user/{people[0]}/tasks/task/view/{identity}/')
        except BitrixAppError as error:
            self.reply(job, str(error))
        except ValueError as error:
            self.reply(job, str(error) if writing else 'Не удалось разобрать запрос. Повтори, кому поставить задачу и что сделать.')
        except Exception:
            self.reply(job, 'Битрикс24 не подтвердил создание. Проверь список задач перед повторной командой.' if writing else 'Не удалось разобрать запрос. Повтори команду.')
        finally:
            self.slots.release()

    def reply(self, job, text):
        from .bitrix_bot import _answer_parts
        self.bot._update(job['id'], state='delivering', parts=json.dumps(_answer_parts(text, [])), part_index=0)
