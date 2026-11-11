"""Enqueue the current UTC+5 day's recordings in the dashboard's persistent queue."""
import argparse
import datetime as dt
import json
import os
import time

from . import call_analysis, call_records
from .call_jobs import JobQueue, TERMINAL
from .common import db_lock


TIMEZONE = dt.timezone(dt.timedelta(hours=5))
MODEL, EFFORT = 'gpt-6.1-sol', 'high'
DEFAULT_SELECTION = {'model': MODEL, 'effort': EFFORT, 'fast': True, 'period': 'today'}


class SettingsConflict(ValueError):
    pass


def get_settings(con):
    with db_lock():
        row = con.execute('SELECT * FROM call_auto_settings WHERE id=1').fetchone()
    result = dict(row) if row else {**DEFAULT_SELECTION, 'enabled': False, 'revision': 0,
                                   'author': None, 'updated': None}
    result.pop('id', None)
    result['enabled'], result['fast'] = bool(result['enabled']), bool(result['fast'])
    return {**result, 'time': '17:30', 'timezone': 'UTC+05:00'}


def set_settings(con, values, author):
    if type(values.get('enabled')) is not bool:
        raise ValueError('Укажите, включён ли авторазбор')
    revision = values.get('revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Обновите состояние авторазбора перед изменением')
    if values['enabled']:
        if any(field not in values for field in ('model', 'effort', 'fast', 'period')):
            raise ValueError('При каждом включении выберите модель, уровень мышления, режим Fast и день обработки')
        if not isinstance(values['model'], str) or not isinstance(values['effort'], str):
            raise ValueError('Выберите модель и уровень мышления')
        call_analysis.validate_selection(values['model'], values['effort'])
        call_analysis.validate_fast(values['fast'])
        if values['period'] not in ('today', 'yesterday'):
            raise ValueError('Выберите текущий или предыдущий день')
    with db_lock():
        con.execute('BEGIN IMMEDIATE')
        try:
            previous = get_settings(con)
            if revision != previous['revision']:
                raise SettingsConflict('Настройки авторазбора изменились. Обновите их и повторите действие')
            selection = values if values['enabled'] else previous
            con.execute('INSERT OR REPLACE INTO call_auto_settings '
                        '(id,enabled,model,effort,fast,period,revision,author,updated) VALUES(1,?,?,?,?,?,?,?,?)',
                        (int(values['enabled']), selection['model'], selection['effort'], int(selection['fast']),
                         selection['period'], revision + 1, author, dt.datetime.now(TIMEZONE).isoformat(timespec='seconds')))
            con.commit()
        except Exception:
            con.rollback()
            raise
        return get_settings(con)


def current_day(now=None, period='today'):
    moment = now or dt.datetime.now(TIMEZONE)
    moment = moment.replace(tzinfo=TIMEZONE) if moment.tzinfo is None else moment.astimezone(TIMEZONE)
    return moment.date() - dt.timedelta(days=int(period == 'yesterday'))


def refresh(con, config, client, employees, now=None, selection=None):
    selection = selection or get_settings(con)
    rows = client.fetch_recordings(employees, current_day(now, selection['period']))
    call_records.upsert(con, rows)
    downloaded, failed = call_records.download_pending(con, client, config, ids=[row['id'] for row in rows])
    return {'recordings': len(rows), 'downloaded': downloaded, 'download_failed': failed}


def enqueue(con, config, now=None, dry_run=False, selection=None, scheduled_revision=None):
    selection = selection or get_settings(con)
    day = current_day(now, selection['period'])
    first, last = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
    author = 'auto:daily-calls:' + first
    rows = [dict(row) for row in con.execute(
        'SELECT * FROM call_records WHERE dt>=? AND dt<? ORDER BY dt DESC,id', (first, last))]
    ready = [row['id'] for row in rows if row['file'] and os.path.isfile(call_records.file_path(config, row))]
    result = {'day': first, 'timezone': 'UTC+05:00', 'recordings': len(rows),
              'unavailable': len(rows) - len(ready), 'job': None}
    with db_lock():
        # Serialize independent CLI producers as well as requests in this process.
        con.execute('BEGIN IMMEDIATE')
        try:
            current = get_settings(con)
            if scheduled_revision is not None and (not current['enabled'] or current['revision'] != scheduled_revision):
                result['skipped'] = 'Авторазбор выключен или его настройки изменились'
                con.commit()
                return result
            existing = con.execute('SELECT id,status FROM call_processing_jobs WHERE author=? ORDER BY created DESC,rowid DESC LIMIT 1',
                                   (author,)).fetchone()
            queue = JobQueue(con, config, recover=False)
            if existing and existing['status'] in ('queued', 'running', 'cancelling', 'cancelled'):
                result['job'] = queue.get(existing['id'])
                result['reused'] = True
            else:
                pending = list(call_analysis.todo(con, ready))
                result['already_processed'] = len(ready) - len(pending)
                result['pending'] = len(pending)
                if pending and not dry_run:
                    result['job'] = queue.create(pending, model=selection['model'], effort=selection['effort'],
                                                 fast=selection['fast'], author=author)
            con.commit()
        except Exception:
            con.rollback()
            raise
    return result


def wait_for_jobs(con, config, result, now=None, selection=None, scheduled_revision=None):
    queue = JobQueue(con, config, recover=False)
    while result['job']:
        job, previous = result['job'], None
        while True:
            job = queue.get(job['id'])
            progress = (job['status'], job['done'], job['failed'], job['skipped'])
            if progress != previous:
                print(json.dumps(job, ensure_ascii=False), flush=True)
                previous = progress
            if job['status'] in TERMINAL:
                break
            time.sleep(3)
        if job['status'] != 'completed':
            return 1
        # More audio may have become ready while an existing daily job ran.
        result = enqueue(con, config, now=now, selection=selection, scheduled_revision=scheduled_revision)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(not result.get('skipped') and result['unavailable'] > 0)


def main(args=None):
    parser = argparse.ArgumentParser(description='Daily call analysis for the current date in UTC+5')
    parser.add_argument('--dry-run', action='store_true', help='Inspect local recordings without fetching or enqueueing')
    parser.add_argument('--wait', action='store_true', help='Wait until the dashboard finishes the queued job')
    parser.add_argument('--scheduled', action='store_true', help='Respect the dashboard auto-analysis switch and settings')
    options = parser.parse_args(args)
    from .common import connect, load_config, load_rules
    from .employees import apply
    from .bitrix24 import Bitrix24
    config, con = load_config(), connect()
    now = dt.datetime.now(TIMEZONE)
    try:
        selection = get_settings(con) if options.scheduled else dict(DEFAULT_SELECTION)
        if options.scheduled and not selection['enabled']:
            print(json.dumps({'skipped': 'Авторазбор выключен в дашборде'}, ensure_ascii=False), flush=True)
            return 0
        scheduled_revision = selection['revision'] if options.scheduled else None
        if not options.dry_run:
            apply(con, config, load_rules())
            employees = {int(employee['b24_user_id']): employee['login'] for employee in config['employees']
                         if employee.get('b24_user_id')}
            refreshed = refresh(con, config, Bitrix24(config['bitrix24']), employees, now=now, selection=selection)
            print(json.dumps(refreshed, ensure_ascii=False), flush=True)
        result = enqueue(con, config, now=now, dry_run=options.dry_run, selection=selection,
                         scheduled_revision=scheduled_revision)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if options.wait and result['job'] and not options.dry_run:
            return wait_for_jobs(con, config, result, now=now, selection=selection,
                                 scheduled_revision=scheduled_revision)
        return int(not result.get('skipped') and result['unavailable'] > 0 and not options.dry_run)
    finally:
        con.close()
