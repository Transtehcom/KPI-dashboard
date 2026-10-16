"""Синхронизация: демо-данные или выгрузка из Битрикс24 и 1С."""
import datetime as dt
import threading
import traceback

from . import demo, manual
from .bitrix24 import Bitrix24
from .common import current_month, db_lock, month_add, month_bounds, replace_rows, today
from .letters import fetch_letters
from .onec import DATASETS, OneC

B24_DATE_FIELDS = {"activities": "dt", "kp_letters": "dt", "tenders": None, "deals": "closed_date",
                   "leads": "qualified_date", "clients": None, "tasks": "deadline"}

_running = threading.Lock()
# ход текущей синхронизации: когда началась и какой шаг выполняется (для кнопки «Обновить данные»)
_progress = {"started": None, "step": ""}


def is_running():
    return _running.locked()


def progress():
    return dict(_progress, running=is_running())


def start_background(con, config):
    """Запустить синхронизацию в фоне. Возвращает started или busy, не дожидаясь окончания (выгрузка идёт минуты)."""
    if is_running():
        return {"status": "busy", **progress()}
    threading.Thread(target=run, args=(con, config), daemon=True).start()
    return {"status": "started"}


def run(con, config, force_demo=False):
    if not _running.acquire(blocking=False):
        return {"status": "busy", "message": "Синхронизация уже выполняется"}
    started = dt.datetime.now().isoformat(timespec="seconds")
    _progress.update(started=started, step="Подготовка")

    class Log(list):  # журнал синхронизации; последняя строка — текущий шаг
        def append(self, line):
            super().append(line)
            _progress["step"] = line

    log = Log()
    status = "ok"
    try:
        if config.get("mode", "demo") == "demo" or force_demo:
            counts = demo.generate(con, config)
            log.append("Демо-данные: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        else:
            emps = [e for e in config["employees"] if e.get("position")]
            logins = [e["login"] for e in emps]
            months = config.get("sync", {}).get("months_back", 6)
            date_from = month_bounds(month_add(current_month(), -months))[0]
            date_to = today()
            # сначала всё выгружаем (это долго), потом пишем одной транзакцией под db_lock,
            # чтобы дашборд никогда не видел наполовину обновлённые таблицы
            writes = []  # (источник, таблица, строки, поле даты)
            b24 = None

            if config.get("bitrix24", {}).get("webhook") and "ВАШ-ПОРТАЛ" not in config["bitrix24"]["webhook"]:
                b24 = Bitrix24(config["bitrix24"], log=log.append)
                by_uid = {int(e["b24_user_id"]): e["login"] for e in emps if e.get("b24_user_id")}
                # воронки и данные для KPI выгружаются независимо: ошибка в одном не мешает обновить другое
                try:
                    rows = b24.fetch_crm_deals(by_uid, config.get("deal_funnel", {})) + \
                        b24.fetch_crm_leads(by_uid, config.get("lead_funnel", {}))
                    writes.append(("Б24", "crm_deals", rows, None))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка Битрикс24 (воронки): {e}")
                    traceback.print_exc()
                try:
                    data = b24.fetch(by_uid, date_from)
                    if config.get("letters_sheet", {}).get("url"):
                        data.pop("kp_letters", None)  # письма с КП берутся из журнала исходящих (ниже)
                    for table, rows in data.items():
                        writes.append(("Б24", table, rows, B24_DATE_FIELDS.get(table)))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка Битрикс24 (данные для KPI): {e}")
                    traceback.print_exc()
            else:
                log.append("Битрикс24 не настроен (bitrix24.webhook)")

            if config.get("letters_sheet", {}).get("url"):
                try:
                    writes.append(("Журнал писем", "kp_letters",
                                   fetch_letters(config["letters_sheet"], emps, date_from, log=log.append), "dt"))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка журнала исходящих писем: {e}")
                    traceback.print_exc()

            if config.get("onec", {}).get("base_url") and "СЕРВЕР-1С" not in config["onec"]["base_url"]:
                try:
                    onec_cfg = config["onec"]
                    field = config.get("bitrix24", {}).get("fields", {}).get("deal_order_1c")
                    sales_ds = onec_cfg.get("datasets", {}).get("sales", {})
                    if field and b24 and sales_ds.get("source") == "sales_by_lead":
                        # выручка по заказу клиента, указанному в сделке Б24, — ответственному за сделку
                        by_uid = {int(e["b24_user_id"]): e["onec_manager"] for e in emps
                                  if e.get("b24_user_id") and e.get("onec_manager")}
                        owners = {k: [(by_uid[u], d) for u, d in v if u in by_uid] for k, v in b24.order_owners(field).items()}
                        owners = {k: v for k, v in owners.items() if v}
                        log.append(f"Б24: сделок с номером заказа 1С — {len(owners)}")
                        onec_cfg = {**onec_cfg, "datasets": {**onec_cfg["datasets"],
                                                              "sales": {**sales_ds, "order_managers": owners}}}
                    onec = OneC(onec_cfg, log=log.append)
                    managers = {str(e["onec_manager"]).strip().lower(): e["login"] for e in emps if e.get("onec_manager")}
                    data = onec.fetch(con, managers, date_from, date_to)
                    for table, rows in data.items():
                        date_field = next((d for t, d in DATASETS.values() if t == table), None)
                        writes.append(("1С", table, rows, date_field))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка 1С: {e}")
                    traceback.print_exc()
            else:
                log.append("1С не настроена (onec.base_url)")

            if writes:
                with db_lock():
                    try:
                        for src, table, rows, date_field in writes:
                            # воронки сделок и лидов — срез по всем ответственным, заменяется целиком
                            replace_rows(con, table, rows, emps=None if table == "crm_deals" else logins, date_field=date_field,
                                         date_from=date_from.isoformat(), commit=False)
                        manual.apply(con)  # ручной ввод руководителя поверх свежей выгрузки
                        if any(table == "clients" for _, table, _, _ in writes):
                            snapshot_clients(con, current_month())
                        con.commit()
                    except Exception as e:  # noqa
                        con.rollback()
                        status = "error"
                        log.append(f"Ошибка записи в базу, данные не изменены: {e}")
                        traceback.print_exc()
                    else:
                        log += [f"{src} → {table}: {len(rows)}" for src, table, rows, _ in writes]
    finally:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        with db_lock():
            con.execute("INSERT INTO sync_log (started, finished, status, message) VALUES (?,?,?,?)",
                        (started, finished, status, "\n".join(log)))
            con.commit()
        _running.release()
    return {"status": status, "message": "\n".join(log), "finished": finished}


def snapshot_clients(con, month):
    """Запомнить состояние клиентов на месяц: прошлые месяцы KPI «7 активных клиентов» считаются по снимку,
    а не по текущему состоянию CRM, которое со временем меняется."""
    con.execute("DELETE FROM clients_monthly WHERE month=?", (month,))
    con.execute("INSERT INTO clients_monthly (month, id, emp, name, need, strategy) "
                "SELECT ?, id, emp, name, need, strategy FROM clients", (month,))


def last_sync(con):
    r = con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def start_scheduler(con, config):
    minutes = config.get("sync", {}).get("interval_minutes", 60)
    if config.get("mode", "demo") == "demo" or not minutes:
        return

    def loop():
        import time
        while True:
            time.sleep(minutes * 60)
            try:
                run(con, config)
            except Exception:  # noqa
                traceback.print_exc()

    threading.Thread(target=loop, daemon=True).start()
