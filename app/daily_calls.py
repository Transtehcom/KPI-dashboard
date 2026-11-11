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


def current_day(now=None):
    return (now or dt.datetime.now(TIMEZONE)).astimezone(TIMEZONE).date()


def refresh(con, config, client, employees, now=None):
    rows = client.fetch_recordings(employees, current_day(now))
    call_records.upsert(con, rows)
    downloaded, failed = call_records.download_pending(con, client, config, ids=[row['id'] for row in rows])
    return {'recordings': len(rows), 'downloaded': downloaded, 'download_failed': failed}


def enqueue(con, config, now=None, dry_run=False):
    day = current_day(now)
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
                    result['job'] = queue.create(pending, model=MODEL, effort=EFFORT, fast=True, author=author)
            con.commit()
        except Exception:
            con.rollback()
            raise
    return result


def wait_for_jobs(con, config, result, now=None):
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
        result = enqueue(con, config, now=now)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(result['unavailable'] > 0)


def main(args=None):
    parser = argparse.ArgumentParser(description='Daily call analysis for the current date in UTC+5')
    parser.add_argument('--dry-run', action='store_true', help='Inspect local recordings without fetching or enqueueing')
    parser.add_argument('--wait', action='store_true', help='Wait until the dashboard finishes the queued job')
    options = parser.parse_args(args)
    from .common import connect, load_config, load_rules
    from .employees import apply
    from .bitrix24 import Bitrix24
    config, con = load_config(), connect()
    now = dt.datetime.now(TIMEZONE)
    try:
        if not options.dry_run:
            apply(con, config, load_rules())
            employees = {int(employee['b24_user_id']): employee['login'] for employee in config['employees']
                         if employee.get('b24_user_id')}
            refreshed = refresh(con, config, Bitrix24(config['bitrix24']), employees, now=now)
            print(json.dumps(refreshed, ensure_ascii=False), flush=True)
        result = enqueue(con, config, now=now, dry_run=options.dry_run)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if options.wait and result['job'] and not options.dry_run:
            return wait_for_jobs(con, config, result, now=now)
        return int(result['unavailable'] > 0 and not options.dry_run)
    finally:
        con.close()
