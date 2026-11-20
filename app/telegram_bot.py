"""Telegram private messages backed by the dashboard's existing chat and task queue."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.request
import uuid

from . import bot_model_menu
from .common import db_lock
from .bitrix_bot import ATTACHMENT_QUESTION
from .gpt_access import Access


OPEN_SETTINGS = 'ttk:open'


def settings_reply(text):
    return {'text':text, 'buttons':[('Настройки модели',OPEN_SETTINGS)]}


def telegram_message(peer, reply):
    if isinstance(reply,str):
        return {'chat_id':peer, 'text':reply}
    buttons = [{'text':label,'callback_data':value} for label,value in reply['buttons']]
    return {'chat_id':peer, 'text':reply['text'], 'reply_markup':{
        'inline_keyboard':[buttons[i:i+2] for i in range(0,len(buttons),2)]}}


def bot_token():
    token = os.environ.get('TTKGPT_TELEGRAM_TOKEN', '')
    if not token and os.name == 'nt':
        class Credential(ctypes.Structure):
            _fields_ = [('flags', wintypes.DWORD), ('type', wintypes.DWORD), ('name', wintypes.LPWSTR),
                        ('comment', wintypes.LPWSTR), ('written', wintypes.FILETIME),
                        ('size', wintypes.DWORD), ('blob', ctypes.c_void_p)]
        ptr = ctypes.POINTER(Credential)()
        api = ctypes.windll.advapi32
        if api.CredReadW('ttkGPT/Telegram', 1, 0, ctypes.byref(ptr)):
            try:
                token = ctypes.string_at(ptr.contents.blob, ptr.contents.size).decode('utf-16-le')
            finally:
                api.CredFree(ptr)
    return token if re.fullmatch(r'[1-9]\d*:[A-Za-z0-9_-]{20,}', token) else ''


class Telegram:
    def __init__(self, bridge):
        self.bridge, self.con = bridge, bridge.con
        self.token, self.username, self.bot_id = '', '', 0
        self.stop = threading.Event()
        with db_lock():
            self.con.executescript("""
                CREATE TABLE IF NOT EXISTS tg_ticket(hash TEXT PRIMARY KEY, owner TEXT UNIQUE, expires INTEGER, bot INTEGER);
                CREATE TABLE IF NOT EXISTS tg_binding(bot INTEGER, peer INTEGER, owner TEXT, uid INTEGER,
                    PRIMARY KEY(bot,peer), UNIQUE(bot,owner));
                CREATE TABLE IF NOT EXISTS tg_cursor(bot INTEGER PRIMARY KEY, offset INTEGER);
            """)

    def ticket(self, owner):
        if not self.username:
            raise ValueError('Telegram-бот ещё не подключён администратором.')
        code = secrets.token_urlsafe(32)
        digest = hashlib.sha256(code.encode()).hexdigest()
        with db_lock():
            self.con.execute('DELETE FROM tg_ticket WHERE expires<=?', (time.time(),))
            self.con.execute('INSERT OR REPLACE INTO tg_ticket VALUES(?,?,?,?)',
                             (digest, owner, time.time()+300, self.bot_id))
            self.con.commit()
        return {'url': f'https://t.me/{self.username}?start={code}'}

    def unlink(self, owner):
        with db_lock():
            self.con.execute("DELETE FROM bot_model_menus WHERE owner=? AND channel LIKE 'tg:%'", (owner,))
            self.con.execute('DELETE FROM tg_binding WHERE owner=?', (owner,))
            self.con.execute('DELETE FROM tg_ticket WHERE owner=?', (owner,))
            self.con.commit()

    def bound(self, job):
        with db_lock():
            return self.con.execute('SELECT 1 FROM tg_binding WHERE bot=? AND peer=? AND owner=? AND uid=?',
                (-job['bot_id'], job['dialog_id'], job['owner'], job['user_id'])).fetchone() is not None

    def call(self, method, params, content_type='application/json'):
        body = params if isinstance(params, bytes) else json.dumps(params).encode()
        request = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,
                                        data=body, headers={'Content-Type':content_type})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise ValueError('Telegram отклонил запрос.')
        return result['result']

    def deliver(self, job, part, fields):
        if not self.bound(job):
            raise ValueError('Привязка Telegram отключена.')
        peer = job['dialog_id']
        if part['kind'] == 'text':
            self.call('sendMessage', {'chat_id':peer, 'text':fields['message'], 'link_preview_options':{'is_disabled':True}})
            return
        boundary = uuid.uuid4().hex
        name = re.sub(r'[\x00-\x1f"\\/]', '_', fields['name'])
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{peer}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n')
        self.call('sendDocument', header.encode()+base64.b64decode(fields['content'])+f'\r\n--{boundary}--\r\n'.encode(),
                  'multipart/form-data; boundary='+boundary)

    def download(self, reference):
        info = self.call('getFile', {'file_id':reference['telegram_file']})
        path = info.get('file_path', '')
        if not re.fullmatch(r'[a-zA-Z0-9_/-]+\.[a-zA-Z0-9]+', path) or '..' in path:
            raise ValueError('Некорректный путь файла Telegram')
        limit = 20 * 1024 * 1024
        if info.get('file_size', 0) > limit:
            raise ValueError('Файл превышает 20 МБ')
        with urllib.request.urlopen('https://api.telegram.org/file/bot'+self.token+'/'+path, timeout=30) as response:
            raw = response.read(limit+1)
        if len(raw)>limit:
            raise ValueError('Файл превышает 20 МБ')
        return reference['name'], raw

    def accept(self, peer, message, update_id):
        text = message.get('text') or ''
        if message.get('forward_origin') or message.get('via_bot'):
            return 'Отправьте команду или файл от своего имени.'
        if text.startswith('/start '):
            digest = hashlib.sha256(text[7:].strip().encode()).hexdigest()
            row = self.con.execute('SELECT owner FROM tg_ticket WHERE hash=? AND bot=? AND expires>?',
                                   (digest,self.bot_id,time.time())).fetchone()
            if row:
                owner = row[0]
                users = [e for e in self.bridge.config['employees'] if e.get('login') == owner]
                uid = int(users[0].get('b24_user_id') or 0) if len(users)==1 else 0
                if uid and self.bridge._owner(uid) == owner:
                    self.con.execute('DELETE FROM tg_ticket WHERE hash=?',(digest,))
                    self.con.execute('DELETE FROM bot_model_menus WHERE channel=?',(f'tg:{self.bot_id}:{peer}',))
                    self.con.execute('INSERT OR REPLACE INTO tg_binding VALUES(?,?,?,?)',(self.bot_id,peer,owner,uid))
                    return settings_reply('Telegram подключён к ttkGPT. Можно задать вопрос или поставить задачу. Настройки модели — /settings.')
            return 'Ссылка недействительна. Получите новую в разделе ttkGPT дашборда.'
        row = self.binding(peer)
        if not row:
            return 'Откройте ttkGPT в дашборде и нажмите «Подключить Telegram».'
        attachment = message.get('document') or message.get('voice') or (message.get('photo') or [{}])[-1]
        references = []
        if attachment.get('file_id'):
            name = attachment.get('file_name') or ('speech.ogg' if 'voice' in message else 'photo.jpg')
            references.append({'id':update_id,'name':name,'telegram_file':attachment['file_id']})
            text = message.get('caption') or ATTACHMENT_QUESTION
        if not text:
            return 'Отправьте текст, документ, фото или голосовое сообщение.'
        if not references:
            command = text.strip().casefold().removesuffix('@' + self.username.casefold())
            if command == '/start':
                return settings_reply('Вы подключены. Задайте вопрос или напишите «Поставь задачу». Настройки модели — /settings.')
            if command in ('/settings','/model','/effort','/speed'):
                return self.model_menu(row[0],peer,OPEN_SETTINGS)
        self.con.execute("""INSERT OR IGNORE INTO bitrix_bot_jobs
            (bot_id,event_id,message_id,user_id,dialog_id,owner,question,attachments) VALUES(?,?,?,?,?,?,?,?)""",
            (-self.bot_id,update_id,update_id,row[1],str(peer),row[0],text,json.dumps(references)))

    def binding(self, peer):
        row = self.con.execute('SELECT owner,uid FROM tg_binding WHERE bot=? AND peer=?',(self.bot_id,peer)).fetchone()
        return row if row and self.bridge._owner(row[1]) == row[0] else None

    def model_menu(self, owner, peer, value):
        channel = f'tg:{self.bot_id}:{peer}'
        admin = Access(self.bridge.config,owner).admin
        if value == OPEN_SETTINGS:
            return bot_model_menu.begin(self.con,owner,channel,admin,self.bridge._selection(owner))
        return bot_model_menu.choose(self.con,owner,channel,admin,value)

    def handle_update(self, update):
        callback = update.get('callback_query')
        message = (callback or update).get('message') or {}
        chat = message.get('chat') or {}
        sender = (callback or message).get('from') or {}
        peer = chat.get('id')
        private = peer is not None and chat.get('type') == 'private' and peer == sender.get('id') and not sender.get('is_bot')
        reply, alert = None, None
        with db_lock():
            try:
                if callback:
                    row = self.binding(peer) if private else None
                    if not row:
                        alert = 'Откройте личный чат с ботом и проверьте привязку в дашборде.'
                    elif (message.get('from') or {}).get('id') != self.bot_id or not message.get('message_id'):
                        alert = 'Откройте настройки командой /settings.'
                    else:
                        menu = self.model_menu(row[0],peer,callback.get('data') or '')
                        if menu.get('stale'):
                            alert = menu['text']
                        else:
                            reply = menu
                elif private:
                    reply = self.accept(peer,message,update['update_id'])
                self.con.execute('INSERT OR REPLACE INTO tg_cursor VALUES(?,?)',(self.bot_id,update['update_id']+1))
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        if callback:
            ack = {'callback_query_id':callback['id']}
            if alert:
                ack.update(text=alert[:200],show_alert=True)
            try:
                self.call('answerCallbackQuery',ack)
            except (ValueError, OSError):
                # Telegram can expire a callback while the settings change is already committed.
                pass
        if reply:
            fields = telegram_message(peer,reply)
            if callback:
                try:
                    self.call('editMessageText',{**fields,'message_id':message['message_id']})
                    return
                except (ValueError, OSError):
                    # Continue the wizard even if the previous message can no longer be edited.
                    pass
            self.call('sendMessage',fields)

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.username:
                    self.token = bot_token()
                    if not self.token:
                        self.stop.wait(5)
                        continue
                    me = self.call('getMe', {})
                    if me.get('username','').lower() != 'ttk59bot' or me.get('is_bot') is not True:
                        raise ValueError('Ожидается токен @ttk59bot.')
                    self.bot_id, self.username = int(me['id']), me['username']
                with db_lock():
                    row = self.con.execute('SELECT offset FROM tg_cursor WHERE bot=?',(self.bot_id,)).fetchone()
                updates = self.call('getUpdates', {'offset':row[0] if row else 0,'timeout':2,'allowed_updates':['message','callback_query']})
                for update in updates:
                    self.handle_update(update)
                self.bridge.process_jobs(-self.bot_id)
            except Exception:
                self.stop.wait(5)
