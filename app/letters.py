"""Журнал исходящих писем из Google-таблицы: подтверждение отправки КП (вместо писем в сделках Битрикс24).

Таблица должна быть открыта на просмотр по ссылке; дашборд читает её как CSV.
Ответственный в таблице указан фамилией («Тютиков», «Мельникова М.К.») и сопоставляется с сотрудником
по фамилии из name или onec_manager в config.json.
"""
import csv
import datetime as dt
import io
import re
import urllib.request

DEFAULT_COLUMNS = {"number": "N п/п (исх.номер)", "date": "Дата письма", "recipient": "Получатель",
                   "subject": "Тема письма", "responsible": "Ответственный"}


def csv_url(url):
    """Ссылка на таблицу (…/edit?gid=N) → ссылка на выгрузку листа в CSV."""
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m or "export?format=csv" in url:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def surname(s):
    w = re.split(r"[\s.]+", str(s or "").strip())
    return w[0].lower().replace("ё", "е") if w and w[0] else ""


def parse_date(s):
    try:
        d = dt.datetime.strptime(str(s).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    return d if d.year >= 2000 else None


def fetch_letters(cfg, employees, date_from, log=print):
    """employees: сотрудники из config.json. Возвращает строки таблицы kp_letters."""
    cols = {**DEFAULT_COLUMNS, **cfg.get("columns", {})}
    topics = [t.lower() for t in cfg.get("topics", [])]
    by_surname = {}
    for e in employees:
        # фамилия в 1С (onec_manager «Фамилия Имя Отчество») и в имени для дашборда («Имя Фамилия»)
        for n in (e.get("onec_manager", ""), (e.get("name", "").split() or [""])[-1]):
            if surname(n):
                by_surname[surname(n)] = e["login"]
    log("Таблица исходящих писем")
    with urllib.request.urlopen(csv_url(cfg["url"]), timeout=60) as r:
        text = r.read().decode("utf-8-sig")
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        emp = by_surname.get(surname(r.get(cols["responsible"])))
        d = parse_date(r.get(cols["date"]))
        subject = str(r.get(cols["subject"]) or "").strip()
        if not emp or not d or d < date_from:
            continue
        if topics and not any(t in subject.lower() for t in topics):
            continue
        num = str(r.get(cols["number"]) or "").strip()
        rows.append({"id": f"letter-{i}-{num}", "emp": emp, "dt": d.isoformat(),
                     "deal": str(r.get(cols["recipient"]) or "").strip(), "subject": subject})
    return rows
