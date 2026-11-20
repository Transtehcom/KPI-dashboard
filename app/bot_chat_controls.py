"""Private chat menus shared by the Bitrix and Telegram adapters."""
import secrets


MODELS_LABEL = "⚙️ Настройки модели"
HELP_LABEL = "📖 Памятка"
RESTART_LABEL = "🔄 Начать новый чат"
MENU_TEXT = "Меню ttkGPT. Выберите действие."
HELP_TEXT = (
    "📖 Как пользоваться ttkGPT\n\n"
    "Задайте вопрос обычным сообщением. Укажите период, сотрудника или номер документа. "
    "Например: «Какие мои задачи просрочены на сегодня?» "
    "Можно приложить файл, скриншот или отправить голосовое сообщение.\n\n"
    "Чтобы поставить задачу, начните с «Поставь задачу», укажите исполнителя, действие и срок. "
    "Для записи в «Мои заметки» Битрикса используйте /note текст заметки. "
    "Чтобы отправить личное сообщение сотруднику в Битриксе, укажите получателя и текст: "
    "/send Имя Фамилия: текст сообщения.\n\n"
    "⚙️ /settings — выбрать модель и уровень мышления. Администратору также доступен выбор скорости.\n"
    "🔄 /restart — начать разговор с чистого листа. После подтверждения новый чат станет текущим "
    "в Telegram, Битрикс24 и дашборде. Предыдущий останется в истории ttkGPT в дашборде.\n"
    "📖 /help — открыть эту памятку.\n"
    "/menu — открыть меню."
)


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS bot_chat_restarts(
        channel TEXT PRIMARY KEY, owner TEXT NOT NULL, nonce TEXT NOT NULL)""")


def request_restart(con, owner, channel):
    nonce = secrets.token_hex(8)
    con.execute("INSERT OR REPLACE INTO bot_chat_restarts(channel,owner,nonce) VALUES(?,?,?)",
                (channel, owner, nonce))
    return {
        "text": "Начать новый общий чат в Telegram, Битрикс24 и дашборде? "
                "Старая переписка сохранится в истории ttkGPT. Выбранная модель останется прежней.",
        "buttons": [("🔄 Начать новый чат", f"restart:{nonce}:yes"), ("↩️ Отмена", f"restart:{nonce}:no")],
    }


def consume_restart(con, owner, channel, value):
    """Consume a confirmation once, in the same transaction as the caller's action."""
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) != 3 or parts[0] != "restart" or parts[2] not in ("yes", "no"):
        return None
    removed = con.execute("DELETE FROM bot_chat_restarts WHERE channel=? AND owner=? AND nonce=?",
                          (channel, owner, parts[1]))
    return parts[2] if removed.rowcount else None
