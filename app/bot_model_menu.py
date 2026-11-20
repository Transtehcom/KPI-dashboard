"""Interactive model selection shared by the private ttkGPT messengers.

The transport verifies the sender and calls these functions inside its DB transaction.
Only the final button writes to the user's preferences.
"""
import secrets

from .call_analysis import model_options, validate_fast, validate_selection


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS bot_model_menus(
        channel TEXT PRIMARY KEY, owner TEXT NOT NULL, nonce TEXT NOT NULL,
        step TEXT NOT NULL, model TEXT, effort TEXT)""")


def _prompt(con, owner, channel, step, text, choices, model=None, effort=None):
    nonce = secrets.token_hex(8)
    con.execute("""INSERT OR REPLACE INTO bot_model_menus(channel,owner,nonce,step,model,effort)
                   VALUES(?,?,?,?,?,?)""", (channel, owner, nonce, step, model, effort))
    return {"text": text, "buttons": [(label, f"ttk:{nonce}:{key}") for key, label in choices.items()]}


def begin(con, owner, channel, admin, selection):
    options = model_options(admin)
    current = options["models"][selection["model"]] + " · " + options["efforts"][selection["effort"]]
    if admin:
        current += " · " + ("Fast" if selection["fast"] else "Обычная скорость")
    return _prompt(con, owner, channel, "model", f"Сейчас: {current}.\nВыберите модель.", options["models"])


def choose(con, owner, channel, admin, value):
    stale = {"text": "Эта кнопка уже недоступна. Откройте настройки заново: /settings.", "buttons": [], "stale": True}
    if not isinstance(value, str):
        return stale
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "ttk":
        return stale
    _, nonce, choice = parts
    row = con.execute("SELECT step,model,effort FROM bot_model_menus WHERE channel=? AND owner=? AND nonce=?",
                      (channel, owner, nonce)).fetchone()
    if not row or row[0] not in ("model", "effort", "speed"):
        return stale
    step, model, effort = row
    options = model_options(admin)
    if step == "model":
        if choice not in options["models"]:
            return stale
        return _prompt(con, owner, channel, "effort", options["models"][choice] + " · Выберите уровень мышления.",
                       options["model_efforts"][choice], model=choice)
    if step == "effort":
        effort = choice
    try:
        validate_selection(model, effort, admin)
    except ValueError:
        return stale
    title = options["models"][model] + " · " + options["efforts"][effort]
    if step == "effort" and admin:
        return _prompt(con, owner, channel, "speed", title + " · Выберите скорость.",
                       {"normal": "Обычная", "fast": "Fast"}, model, effort)
    if step == "speed" and (not admin or choice not in ("normal", "fast")):
        return stale
    fast = step == "speed" and choice == "fast"
    validate_fast(fast, admin)
    con.execute("""INSERT INTO bot_model_preferences(owner,model,effort,fast) VALUES(?,?,?,?)
                   ON CONFLICT(owner) DO UPDATE SET model=excluded.model, effort=excluded.effort, fast=excluded.fast""",
                (owner, model, effort, int(fast)))
    con.execute("DELETE FROM bot_model_menus WHERE channel=? AND owner=?", (channel, owner))
    if admin:
        title += " · " + ("Fast" if fast else "Обычная скорость")
    return {"text": "Настройки сохранены: " + title + ".", "buttons": []}
