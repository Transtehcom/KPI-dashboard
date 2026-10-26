"""Общие функции: конфиг, база SQLite, даты."""
import calendar
import datetime as dt
import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "kpi.sqlite")

_db_lock = threading.RLock()


def load_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        path = os.path.join(ROOT, "config.example.json")
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    apply_timezone(config.get("timezone"))
    return config


def apply_timezone(name):
    """Часовой пояс процесса: от него зависят отметки времени синхронизации, «сегодня» в периодах и сроки сессий.
    Нужен, когда сервер работает в UTC. На Windows не применяется — там берётся пояс системы."""
    if name and hasattr(time, "tzset") and os.environ.get("TZ") != name:
        os.environ["TZ"] = name
        time.tzset()


def load_rules():
    return load_json("kpi_rules.json")


SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, kind TEXT, direction TEXT, duration INTEGER,
  is_lpr INTEGER, secretary INTEGER, next_step INTEGER, status TEXT,
  has_protocol INTEGER, has_summary INTEGER, subject TEXT, client TEXT, closed_dt TEXT, contact TEXT);
CREATE INDEX IF NOT EXISTS ix_act ON activities(emp, dt);

CREATE TABLE IF NOT EXISTS tenders (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, client TEXT, created TEXT,
  in_work INTEGER, price_review INTEGER, submitted_date TEXT,
  result TEXT, result_date TEXT, price REAL, planned_cost REAL, actual_cost REAL, rejected INTEGER);

CREATE TABLE IF NOT EXISTS deals (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, client TEXT, created TEXT,
  closed_date TEXT, result TEXT, amount REAL);

-- текущий срез сделок и лидов CRM для вкладки «Воронки» (все ответственные, не только сотрудники с KPI);
-- entity: deal — сделка, lead — лид
CREATE TABLE IF NOT EXISTS crm_deals (
  id TEXT PRIMARY KEY, emp TEXT, responsible_id TEXT, responsible TEXT, category_id TEXT, category TEXT,
  stage_id TEXT, stage TEXT, stage_sort INTEGER, semantic TEXT, title TEXT, client TEXT, amount REAL,
  created TEXT, stage_since TEXT, snapshot TEXT, entity TEXT);

CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, first_contact TEXT, qualified_date TEXT,
  need INTEGER, budget INTEGER, lpr INTEGER, solvency INTEGER, next_step INTEGER, status TEXT);

-- когда дашборд впервые увидел у лида все 5 условий квалификации: лид засчитывается в этом месяце навсегда
CREATE TABLE IF NOT EXISTS lead_qualified (
  id TEXT PRIMARY KEY, emp TEXT, first_seen TEXT);

CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY, emp TEXT, name TEXT, need INTEGER, strategy INTEGER, updated TEXT);

-- состояние клиентов на конец месяца: при синхронизации перезаписывается только текущий месяц
CREATE TABLE IF NOT EXISTS clients_monthly (
  month TEXT, id TEXT, emp TEXT, name TEXT, need INTEGER, strategy INTEGER, PRIMARY KEY (month, id));

CREATE TABLE IF NOT EXISTS kp (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, client TEXT, amount REAL,
  out_of_tender INTEGER, confirmed INTEGER, source TEXT, doc TEXT, ordered INTEGER);

-- проваленные сделки (воронка «Реализация», стадия «Сделка провалена») — KPI 3 тендерного отдела
CREATE TABLE IF NOT EXISTS deal_failures (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, title TEXT, client TEXT, stage TEXT, reason TEXT);

-- тендеры из 1С (ТТК_Тендер): ведущий менеджер и статус — для конверсии тендерного отдела, как в «Отчёте ОП»;
-- ordered — на основании тендера создан заказ клиента
CREATE TABLE IF NOT EXISTS tenders_1c (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, doc TEXT, client TEXT, title TEXT, status TEXT, amount REAL, ordered INTEGER);

-- исходящие письма с КП и вложением в карточках сделок Б24 (подтверждение отправки КП)
CREATE TABLE IF NOT EXISTS kp_letters (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, deal TEXT, subject TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, created TEXT, deadline TEXT, closed TEXT, status TEXT);

CREATE TABLE IF NOT EXISTS sales (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, doc TEXT, client TEXT, revenue REAL, cost REAL, signed INTEGER,
  signed_date TEXT, due_date TEXT, paid_date TEXT);

CREATE TABLE IF NOT EXISTS receivables (
  id TEXT PRIMARY KEY, emp TEXT, doc TEXT, client TEXT, ship_date TEXT, due_date TEXT,
  paid_date TEXT, amount REAL);

CREATE TABLE IF NOT EXISTS guarantees (
  id TEXT PRIMARY KEY, emp TEXT, kind TEXT, ref TEXT, amount REAL,
  acts_date TEXT, unblock_date TEXT, return_date TEXT);

CREATE TABLE IF NOT EXISTS penalties (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, amount REAL, reason TEXT);

CREATE TABLE IF NOT EXISTS supplier_orders (
  id TEXT PRIMARY KEY, emp TEXT, doc TEXT, dt TEXT, supplier TEXT, lot TEXT, amount_novat REAL,
  planned_date TEXT, actual_date TEXT, complete INTEGER, defect INTEGER, mis_sort INTEGER,
  deferred_share REAL, deferral_days INTEGER,
  kp_count INTEGER, relevant INTEGER, evidence INTEGER, affiliated INTEGER,
  single_source INTEGER, emergency INTEGER, justified INTEGER, customer_order TEXT);

CREATE TABLE IF NOT EXISTS supply_funnel (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, customer_order TEXT, client TEXT, stage TEXT, doc TEXT,
  status TEXT, created_at TEXT, closed_at TEXT, due_at TEXT, order_dt TEXT);

-- значения, которые руководитель вводит вручную к записям из 1С (срок и дата оплаты реализации, брак, отсрочка…);
-- после каждой синхронизации заново накладываются на таблицу (app/manual.py)
-- заявки на расчёт транспортных расходов (app/onec_logistics.py): все статусы, emp — логист («1c:ФИО» не из дашборда)
CREATE TABLE IF NOT EXISTS transport_requests (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, created_at TEXT, doc TEXT, status TEXT, posted INTEGER, deleted INTEGER,
  sales_manager TEXT, address TEXT, history TEXT, route TEXT, carrier TEXT, cost REAL);

-- поручения экспедитору (app/onec_logistics.py): статус ТТК_Статус, плановая отгрузка, emp — ответственный логист
CREATE TABLE IF NOT EXISTS forwarder_orders (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, created_at TEXT, doc TEXT, status TEXT, posted INTEGER, deleted INTEGER,
  ship_date TEXT, delivery_date TEXT, plan_sum REAL, carrier TEXT, route TEXT, fact_sum REAL);

-- панель «Метрики → Отдел продаж» (макет «Метрики ОП.xlsx»): реестр объектов и действия вводятся вручную
CREATE TABLE IF NOT EXISTS sales_objects (
  id TEXT PRIMARY KEY, type TEXT, client TEXT, emp TEXT, channel TEXT, stage TEXT, entered TEXT, due TEXT,
  f_req TEXT, f_plant TEXT, f_log TEXT, f_extra TEXT, f_price TEXT, ready TEXT, proof TEXT, handover TEXT, docs TEXT,
  order_1c TEXT, debt REAL, pay_due TEXT, url TEXT, updated_by TEXT, updated_at TEXT,
  source TEXT, status_1c TEXT, amount REAL, stage_auto INTEGER);
CREATE TABLE IF NOT EXISTS sales_actions (
  id TEXT PRIMARY KEY, obj_id TEXT, what TEXT, dept TEXT, executor TEXT, due TEXT, kind TEXT, status TEXT,
  needs_id TEXT, question TEXT, link TEXT, ord INTEGER, updated_by TEXT, updated_at TEXT);

-- вкладка «Деньги»: настройки, строки «кто должен» / «плановые выплаты» (kind in / out) и обещанные клиентами даты оплаты
CREATE TABLE IF NOT EXISTS money_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS money_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, party TEXT, basis TEXT, type TEXT, amount REAL, owed REAL, contract TEXT,
  plan TEXT, approval TEXT, priority TEXT, updated_by TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS money_expect (rec_id TEXT PRIMARY KEY, expected TEXT);

CREATE TABLE IF NOT EXISTS manual_fields (
  tbl TEXT, ref TEXT, field TEXT, value TEXT, author TEXT, updated TEXT,
  PRIMARY KEY (tbl, ref, field));

-- штрафы за нарушение финансовой дисциплины, которые вносит руководитель, и отметка «за месяц штрафов нет»
CREATE TABLE IF NOT EXISTS manual_penalties (
  id INTEGER PRIMARY KEY AUTOINCREMENT, emp TEXT, dt TEXT, amount REAL, reason TEXT, author TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS penalty_checks (
  emp TEXT, month TEXT, author TEXT, updated TEXT, PRIMARY KEY (emp, month));

CREATE TABLE IF NOT EXISTS manual_scores (
  emp TEXT, week TEXT, criterion TEXT, score REAL, comment TEXT, author TEXT, updated TEXT,
  PRIMARY KEY (emp, week, criterion));

CREATE TABLE IF NOT EXISTS plans (
  emp TEXT, month TEXT, metric TEXT, value REAL, PRIMARY KEY (emp, month, metric));

CREATE TABLE IF NOT EXISTS baselines (
  emp TEXT, metric TEXT, value REAL, PRIMARY KEY (emp, metric));

-- сотрудники дашборда (раздел «Сотрудники»); при первом запуске переносятся из config.json (app/employees.py)
CREATE TABLE IF NOT EXISTS employees (
  login TEXT PRIMARY KEY, name TEXT, department TEXT, title TEXT, position TEXT, b24_user_id INTEGER,
  onec_manager TEXT, visible INTEGER, admin INTEGER, absences TEXT, sort INTEGER, listen_dept INTEGER);

-- записи разговоров из телефонии Б24 (app/call_records.py). Не в SYNC_TABLES: синхронизация обновляет сведения
-- о звонке, но не трогает скачанный файл. id — как у звонка в activities («call» + ID статистики),
-- call_id — CALL_ID телефонии (для методов telephony.*), bindings — привязки дела звонка «DEAL:12,CONTACT:3»,
-- file — путь к аудио относительно папки записей
CREATE TABLE IF NOT EXISTS call_records (
  id TEXT PRIMARY KEY, call_id TEXT, emp TEXT, dt TEXT, direction TEXT, duration INTEGER, phone TEXT,
  crm_entity TEXT, client TEXT, crm_activity_id TEXT, bindings TEXT, bindings_checked TEXT,
  record_file_id TEXT, record_url TEXT, file TEXT, file_size INTEGER, downloaded TEXT,
  attempts INTEGER DEFAULT 0, error TEXT, purged TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS ix_call_records ON call_records(emp, dt);

-- расшифровки записей звонков (app/transcripts.py): segments — JSON [[начало, конец, текст], …] без голосового меню,
-- text — сплошной текст, spent — секунд на распознавание; error/attempts — неудачные попытки
CREATE TABLE IF NOT EXISTS call_transcripts (
  id TEXT PRIMARY KEY, segments TEXT, text TEXT, duration REAL, engine TEXT, spent REAL, created TEXT,
  error TEXT, attempts INTEGER DEFAULT 0);

-- разбор звонков через Claude (app/call_analysis.py): result — JSON с метками вместо персональных данных,
-- pii — таблица замен (только на этом компьютере, удаляется вместе с записью по сроку хранения), roles — роль говорящего
-- по предложениям расшифровки (М — сотрудник, К — собеседник, А — автоответчик, «-» — не определена);
-- version — версия инструкции для Claude (PROMPT_VERSION): разборы прежней версии делаются заново
CREATE TABLE IF NOT EXISTS call_analysis (
  id TEXT PRIMARY KEY, version INTEGER, call_type TEXT, offer INTEGER, agreement INTEGER, result TEXT, roles TEXT,
  pii TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, spent REAL, created TEXT,
  error TEXT, attempts INTEGER DEFAULT 0, secretary INTEGER);

-- отметка руководителя к звонку поверх разбора Claude (повторный разбор её не стирает): secretary 1/0
CREATE TABLE IF NOT EXISTS call_marks (
  id TEXT PRIMARY KEY, secretary INTEGER, author TEXT, updated TEXT);

-- журнал прослушиваний записей звонков: кто, какую запись (call_records.id), чей звонок, когда, с какого адреса;
-- result: ok — прослушал, read — открыл расшифровку, denied — запросил чужую запись без прав
CREATE TABLE IF NOT EXISTS listen_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, login TEXT, record TEXT, emp TEXT, ip TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS ix_listen_log ON listen_log(ts);

CREATE TABLE IF NOT EXISTS users (
  login TEXT PRIMARY KEY, pwd_hash TEXT, salt TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, login TEXT, expires TEXT);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, status TEXT, message TEXT);
"""

# таблицы, которые полностью перезаписываются при синхронизации
SYNC_TABLES = ["activities", "tenders", "deals", "crm_deals", "leads", "clients", "kp", "kp_letters", "tasks", "sales",
               "receivables", "guarantees", "penalties", "supplier_orders", "supply_funnel", "transport_requests", "forwarder_orders", "tenders_1c",
               "deal_failures"]

# столбцы, добавленные после первой версии: в старых базах их создаёт migrate()
ADDED_COLUMNS = {"activities": [("closed_dt", "TEXT"), ("contact", "TEXT")], "tenders_1c": [("ordered", "INTEGER")],
                 "sales": [("signed_date", "TEXT"), ("due_date", "TEXT"), ("paid_date", "TEXT")],
                 "crm_deals": [("entity", "TEXT")], "tenders": [("rejected", "INTEGER")],
                 "supply_funnel": [("doc", "TEXT"), ("status", "TEXT"), ("created_at", "TEXT"), ("closed_at", "TEXT"),
                                   ("due_at", "TEXT"), ("order_dt", "TEXT")], "kp": [("doc", "TEXT"), ("ordered", "INTEGER")],
                 "employees": [("listen_dept", "INTEGER")], "call_analysis": [("secretary", "INTEGER")],
                 "transport_requests": [("history", "TEXT"), ("route", "TEXT"), ("carrier", "TEXT"), ("cost", "REAL")],
                 "forwarder_orders": [("fact_sum", "REAL")],
                 "sales_objects": [("source", "TEXT"), ("status_1c", "TEXT"), ("amount", "REAL"), ("stage_auto", "INTEGER")]}


def migrate(con):
    for table, cols in ADDED_COLUMNS.items():
        have = table_columns(con, table)
        for name, ctype in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")
    con.commit()


def connect(path=None):
    if path is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = DB_PATH
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate(con)
    return con


def db_lock():
    return _db_lock


def table_columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def replace_rows(con, table, rows, emps=None, date_field=None, date_from=None, commit=True):
    """Заменить данные таблицы: удаляет записи сотрудников emps (за период, если задан) и вставляет rows.

    commit=False — не фиксировать транзакцию (вызывающий сам делает commit под db_lock)."""
    cols = table_columns(con, table)
    with _db_lock:
        if emps is None:
            con.execute(f"DELETE FROM {table}")
        else:
            q = f"DELETE FROM {table} WHERE emp IN ({','.join('?' * len(emps))})"
            args = list(emps)
            if date_field and date_from:
                q += f" AND ({date_field} >= ? OR {date_field} IS NULL)"
                args.append(date_from)
            con.execute(q, args)
        for r in rows:
            keys = [k for k in r if k in cols]
            con.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [r[k] for k in keys])
        if commit:
            con.commit()


# ---------- даты ----------

def parse_date(s):
    if not s:
        return None
    if isinstance(s, (dt.date, dt.datetime)):
        return s if isinstance(s, dt.date) and not isinstance(s, dt.datetime) else s.date()
    s = str(s)[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        for fmt in ("%d.%m.%Y",):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def month_bounds(month):
    y, m = map(int, month.split("-"))
    first = dt.date(y, m, 1)
    last = dt.date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def month_add(month, delta):
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def quarter_months(month):
    y, m = map(int, month.split("-"))
    q0 = (m - 1) // 3 * 3 + 1
    return [f"{y:04d}-{q0 + i:02d}" for i in range(3)]


def working_days(first, last, holidays, workdays=None):
    """Рабочие дни с first по last включительно. holidays — праздники, workdays — рабочие субботы/воскресенья (переносы)."""
    hol = set(holidays or [])
    extra = set(workdays or [])
    n = 0
    d = first
    while d <= last:
        iso = d.isoformat()
        if (d.weekday() < 5 and iso not in hol) or iso in extra:
            n += 1
        d += dt.timedelta(days=1)
    return n


def business_days_between(a, b, holidays, workdays=None):
    """Количество рабочих дней после a до b включительно (0, если b <= a)."""
    if not a or not b or b <= a:
        return 0
    return working_days(a + dt.timedelta(days=1), b, holidays, workdays)


def weeks_of_month(month):
    """Понедельники недель месяца. Неделя относится к месяцу, на который приходится её четверг,
    поэтому каждая неделя (и её оценка) попадает ровно в один месяц."""
    first, last = month_bounds(month)
    monday = first - dt.timedelta(days=first.weekday())
    if monday + dt.timedelta(days=3) < first:
        monday += dt.timedelta(days=7)
    res = []
    while monday + dt.timedelta(days=3) <= last:
        res.append(monday.isoformat())
        monday += dt.timedelta(days=7)
    return res


def today():
    return dt.date.today()


def current_month():
    t = today()
    return f"{t.year:04d}-{t.month:02d}"
