"""Persistent Codex threads owned by chat and access fingerprint."""
import hashlib
import json
import os
import uuid

from . import call_analysis
from .common import DATA_DIR, db_lock


class Sessions:
    def __init__(self, con):
        self.con = con
        # A running turn may have persisted remotely without its result being committed locally.
        with db_lock():
            con.execute("UPDATE gpt_codex_sessions SET thread_id=NULL,busy=0,active_message=NULL,last_message=0 WHERE active_message IS NOT NULL")
            con.commit()

    def state(self, chat):
        with db_lock():
            return dict(self.con.execute("SELECT * FROM gpt_codex_sessions WHERE chat_id=?", (chat,)).fetchone())

    def begin(self, chat, scope, master, message):
        digest = hashlib.sha256(master.encode()).hexdigest()
        with db_lock():
            row = self.con.execute("SELECT * FROM gpt_codex_sessions WHERE chat_id=?", (chat,)).fetchone()
            if row is None:
                self.con.execute("INSERT INTO gpt_codex_sessions(chat_id,master_hash,scope,active_message) VALUES (?,?,?,?)", (chat, digest, scope, message))
            else:
                if row["active_message"] is not None and row["active_message"] != message:
                    raise ValueError("В этом чате уже продолжается сессия Codex")
                if row["master_hash"] != digest or row["scope"] != scope or row["busy"]:
                    self.con.execute("UPDATE gpt_codex_sessions SET thread_id=NULL,last_message=0,busy=0 WHERE chat_id=?", (chat,))
                self.con.execute("UPDATE gpt_codex_sessions SET master_hash=?,scope=?,active_message=? WHERE chat_id=?", (digest, scope, message, chat))
            self.con.commit()

    def invalidate(self, chat):
        with db_lock():
            self.con.execute("UPDATE gpt_codex_sessions SET thread_id=NULL,busy=0,active_message=NULL,last_message=0 WHERE chat_id=?", (chat,))
            self.con.commit()

    def finish(self, chat, cursor):
        with db_lock():
            self.con.execute("UPDATE gpt_codex_sessions SET active_message=NULL,last_message=?,busy=0 WHERE chat_id=?", (cursor, chat))
            self.con.commit()

    def call(self, chat, master, delta, bootstrap, config, model, effort, schema, cursor):
        state = self.state(chat)
        payload = bootstrap if not state["thread_id"] else delta
        prompt = "ДАННЫЕ:\n" + json.dumps(payload, ensure_ascii=False)
        if len(prompt) + (len(master) if not state["thread_id"] else 0) > 110000:
            raise ValueError("Слишком большой контекст для сессии. Уточните вопрос")
        session = {"id": state["thread_id"], "master": master if not state["thread_id"] else None,
                   "cwd": os.path.join(DATA_DIR, "gpt-work", hashlib.sha256(chat.encode()).hexdigest())}
        with db_lock():
            self.con.execute("UPDATE gpt_codex_sessions SET busy=1 WHERE chat_id=?", (chat,))
            self.con.execute("UPDATE gpt_messages SET outbound=? WHERE id=?", ((session["master"] + "\n" if session["master"] else "") + prompt, state["active_message"]))
            self.con.commit()
        try:
            response = call_analysis.run_codex(prompt, config, attempts=1, model=model, effort=effort, schema=schema, session=session)
            thread = str(uuid.UUID(response.get("thread_id") or session.get("id") or ""))
            if state["thread_id"] and thread != state["thread_id"]:
                raise ValueError("Codex вернул другую сессию")
            with db_lock():
                self.con.execute("UPDATE gpt_codex_sessions SET thread_id=?,busy=0,last_message=? WHERE chat_id=?", (thread, cursor, chat))
                self.con.commit()
            return response["result"]
        except BaseException:
            self.invalidate(chat)
            raise
