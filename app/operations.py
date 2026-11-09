"""Prospective, observation-aware history for operational panels."""
import copy
import datetime as dt
import hashlib
import json
import re

from .crm_links import object_url, record_url
from .crm_activities import activity_bindings

TZ = dt.timezone(dt.timedelta(hours=5))
SCHEMA = """
CREATE TABLE IF NOT EXISTS operations_sources (
 source TEXT PRIMARY KEY, started_at TEXT, updated_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS operations_source_history (
 id INTEGER PRIMARY KEY, source TEXT, observed_at TEXT, success INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_source_history ON operations_source_history(source,observed_at);
CREATE TABLE IF NOT EXISTS operations_snapshots (
 id INTEGER PRIMARY KEY, source TEXT, entity_id TEXT, observed_at TEXT, hash TEXT, payload TEXT, deleted INTEGER DEFAULT 0, summary TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_snapshots ON operations_snapshots(source,entity_id,observed_at,id);
CREATE TABLE IF NOT EXISTS operations_events (
 id INTEGER PRIMARY KEY, source TEXT, event_id TEXT, entity_id TEXT, event_at TEXT, observed_at TEXT, hash TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_events ON operations_events(source,event_id,observed_at,id);
CREATE TABLE IF NOT EXISTS operations_item_scope (
 id INTEGER PRIMARY KEY, request_id TEXT, item_id TEXT, included INTEGER, reason TEXT, author TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_scope ON operations_item_scope(request_id,item_id,created_at,id);
CREATE TABLE IF NOT EXISTS operations_price_approval (
 id INTEGER PRIMARY KEY, request_id TEXT, item_id TEXT, supplier_id TEXT, author TEXT, created_at TEXT, target_signature TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_price_approval ON operations_price_approval(request_id,item_id,supplier_id,created_at,id);
CREATE TABLE IF NOT EXISTS operations_deadline_approval (
 id INTEGER PRIMARY KEY, request_id TEXT, due TEXT, reason TEXT, author TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_operations_deadline_approval ON operations_deadline_approval(request_id,created_at,id);
"""


def ensure_schema(con):
    con.executescript(SCHEMA)
    columns={row[1] for row in con.execute('PRAGMA table_info(operations_price_approval)')}
    if 'target_signature' not in columns:
        con.execute('ALTER TABLE operations_price_approval ADD COLUMN target_signature TEXT')
    snapshot_columns={row[1] for row in con.execute('PRAGMA table_info(operations_snapshots)')}
    if 'summary' not in snapshot_columns:
        con.execute('ALTER TABLE operations_snapshots ADD COLUMN summary TEXT')


def local_datetime(value):
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or '').strip()
        if not text or text[:4] in ('0001', '9999'):
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    return parsed.astimezone(TZ).replace(tzinfo=None) if parsed.tzinfo else parsed


def deadline(value):
    parsed = local_datetime(value)
    if parsed and parsed.time() == dt.time():
        return dt.datetime.combine(parsed.date(), dt.time.max)
    return parsed


def _stamp(value=None):
    return (local_datetime(value) if value else dt.datetime.now(TZ).replace(tzinfo=None)).isoformat(timespec='microseconds' if isinstance(value, dt.datetime) and value.microsecond else 'seconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _latest(con, table, source, cutoff=None, lean=False):
    field = 'event_id' if table == 'operations_events' else 'entity_id'
    where, args = 'source=?', [source]
    if cutoff:
        where += ' AND observed_at<=?'
        args.append(_stamp(cutoff))
    columns = 'id,source,entity_id,observed_at,deleted,COALESCE(summary,payload) AS payload' if lean else '*'
    rows = con.execute(f'SELECT {columns} FROM {table} WHERE id IN ('
                       f'SELECT id FROM (SELECT id,ROW_NUMBER() OVER(PARTITION BY {field} ORDER BY observed_at DESC,id DESC) AS rn '
                       f'FROM {table} WHERE {where}) WHERE rn=1)', args)
    return {row[field]: dict(row) for row in rows}


def report_summary(entity):
    summary={key:value for key,value in entity.items() if key not in ('items','events','delivery_options')}
    items=[]
    for item in entity.get('items') or []:
        offers=[offer for offer in item.get('offers') or [] if isinstance(offer.get('price'),(int,float)) and offer['price']>0]
        selected=next((offer for offer in offers if offer.get('selected') or offer.get('supplier_id')==item.get('selected_supplier_id')),None)
        lean={key:item.get(key) for key in ('id','quantity','currency','vat_included','selected_supplier_id','selected_price')}
        if selected:
            lean['selected_price']=selected['price']
        selected_price=lean.get('selected_price')
        lean['has_price']=bool(offers or isinstance(selected_price,(int,float)) and selected_price>0)
        items.append(lean)
    summary['items']=items
    return summary


def _quote_signature(offer):
    return _digest({k:v for k,v in offer.items() if k not in ('first_seen_at', 'is_initial', 'last_seen_at')})


def _quote_dates(entity, previous, observed_at, initial):
    known = {}
    for item in (previous or {}).get('items') or []:
        for offer in item.get('offers') or []:
            known[(str(item.get('id')), _quote_signature(offer))] = offer
    for item in entity.get('items') or []:
        for offer in item.get('offers') or []:
            old = known.get((str(item.get('id')), _quote_signature(offer)))
            offer['first_seen_at'] = old.get('first_seen_at', observed_at) if old else observed_at
            offer['is_initial'] = old.get('is_initial', False) if old else bool(initial and not offer.get('offered_at'))


def capture(con, source, entities, source_events, observed_at=None):
    """Caller owns transaction and db_lock. A failed fetch must use mark_failure."""
    observed_at = _stamp(observed_at)
    current = _latest(con, 'operations_snapshots', source)
    state = con.execute('SELECT started_at FROM operations_sources WHERE source=?', (source,)).fetchone()
    initial = not state or not state['started_at']
    seen = set()
    for original in entities:
        entity = copy.deepcopy(original)
        identity = str(entity['id'])
        if identity in seen:
            raise ValueError('Повтор ID документа в одном снимке')
        seen.add(identity)
        previous = json.loads(current[identity]['payload']) if identity in current else None
        _quote_dates(entity, previous, observed_at, initial)
        digest = _digest(entity)
        if identity in current and not current[identity]['deleted'] and current[identity]['hash'] == digest:
            if not current[identity].get('summary'):
                con.execute('UPDATE operations_snapshots SET summary=? WHERE id=?',(_json(report_summary(entity)),current[identity]['id']))
            continue
        con.execute('INSERT INTO operations_snapshots(source,entity_id,observed_at,hash,payload,deleted,summary) VALUES(?,?,?,?,?,0,?)',
                    (source, identity, observed_at, digest, _json(entity),_json(report_summary(entity))))
    for identity, previous in current.items():
        if identity not in seen and not previous['deleted']:
            con.execute('INSERT INTO operations_snapshots(source,entity_id,observed_at,hash,payload,deleted) VALUES(?,?,?,?,?,1)',
                        (source, identity, observed_at, previous['hash'], previous['payload']))
    existing_events = _latest(con, 'operations_events', source)
    for original in source_events:
        event = copy.deepcopy(original)
        identity = str(event['id'])
        at = local_datetime(event.get('at'))
        if at is None:
            continue
        event['at'] = at.isoformat(timespec='seconds')
        digest = _digest(event)
        if identity in existing_events and existing_events[identity]['hash'] == digest:
            continue
        con.execute('INSERT INTO operations_events(source,event_id,entity_id,event_at,observed_at,hash,payload) VALUES(?,?,?,?,?,?,?)',
                    (source, identity, event.get('entity_id'), event['at'], observed_at, digest, _json(event)))
    con.execute('INSERT INTO operations_sources(source,started_at,updated_at,error) VALUES(?,?,?,NULL) '
                'ON CONFLICT(source) DO UPDATE SET started_at=COALESCE(operations_sources.started_at,excluded.started_at),updated_at=excluded.updated_at,error=NULL',
                (source, observed_at, observed_at))
    con.execute('INSERT INTO operations_source_history(source,observed_at,success,error) VALUES(?,?,1,NULL)', (source, observed_at))


def mark_failure(con, source, error, observed_at=None):
    stamp = _stamp(observed_at)
    con.execute('INSERT INTO operations_sources(source,error) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET error=excluded.error',
                (source, str(error)))
    con.execute('INSERT INTO operations_source_history(source,observed_at,success,error) VALUES(?,?,0,?)', (source, stamp, str(error)))


def snapshot(con, source, cutoff=None, lean=False):
    return [{**json.loads(row['payload']), '_observed_at':row['observed_at'], '_source':source} for row in _latest(con, 'operations_snapshots', source, cutoff,lean).values() if not row['deleted']]


def get_entity(con, source, identity, cutoff=None):
    where,args='source=? AND entity_id=?',[source,identity]
    if cutoff:
        where+=' AND observed_at<=?'
        args.append(_stamp(cutoff))
    row=con.execute('SELECT payload,observed_at,deleted FROM operations_snapshots WHERE '+where+' ORDER BY observed_at DESC,id DESC LIMIT 1',args).fetchone()
    return {**json.loads(row['payload']),'_observed_at':row['observed_at'],'_source':source} if row and not row['deleted'] else None


def versions(con, source, cutoff=None, lean=False):
    where, args = 'source=? AND deleted=0', [source]
    if cutoff:
        where += ' AND observed_at<=?'
        args.append(_stamp(cutoff))
    payload="json_remove(COALESCE(summary,payload),'$.items')" if lean else 'payload'
    return [{**json.loads(row['payload']), '_observed_at':row['observed_at'], '_source':source} for row in con.execute(
        'SELECT '+payload+' AS payload,observed_at FROM operations_snapshots WHERE '+where+' ORDER BY observed_at,id', args)]


def events(con, source, cutoff=None):
    stamp = _stamp(cutoff) if cutoff else None
    return [{**json.loads(row['payload']), '_observed_at':row['observed_at']} for row in _latest(con, 'operations_events', source, stamp).values() if not stamp or row['event_at'] <= stamp]


def baseline_started(con):
    row = con.execute('SELECT MIN(started_at) FROM operations_sources WHERE started_at IS NOT NULL').fetchone()
    return row[0] if row else None


def freshness(con, cutoff=None):
    now = local_datetime(cutoff) if cutoff else dt.datetime.now(TZ).replace(tzinfo=None)
    out = {}
    for source in ('onec', 'bitrix'):
        rows = con.execute('SELECT observed_at,success,error FROM operations_source_history WHERE source=? AND observed_at<=? ORDER BY observed_at,id', (source, _stamp(now))).fetchall()
        successes = [row for row in rows if row['success']]
        updated = successes[-1]['observed_at'] if successes else None
        age = max(0, (now-local_datetime(updated)).total_seconds()/60) if updated else None
        started = con.execute('SELECT started_at FROM operations_sources WHERE source=?', (source,)).fetchone()
        out[source] = {'updated_at':updated, 'age_minutes':round(age, 1) if age is not None else None,
                       'stale':age is None or age > 90, 'error':rows[-1]['error'] if rows else None,
                       'started_at':started[0] if started else None}
    return out


def _crm_id(reference):
    match = re.fullmatch(r'(DEAL|LEAD|COMPANY|CONTACT)[:_](\d+)', str(reference or ''), re.I)
    return 'bitrix:'+{'DEAL':'crm_deal','LEAD':'crm_lead','COMPANY':'company','CONTACT':'contact'}[match[1].upper()]+':'+match[2] if match else None


def capture_bitrix(con, config, observed_at=None, source_data=None):
    """Capture normalized local B24 datasets plus optional explicit activity/deal bindings."""
    source_data = source_data or {}
    employees = {str(e.get('b24_user_id')):e for e in config.get('employees',[]) if e.get('b24_user_id')}
    names = {e['login']:e['name'] for e in config.get('employees',[])}
    entities, history = [], []
    for row in con.execute('SELECT * FROM crm_deals'):
        row = dict(row)
        kind = 'crm_lead' if row.get('entity') == 'lead' else 'crm_deal'
        identity = re.sub(r'^(?:deal|lead)', '', row['id'])
        eid = 'bitrix:'+kind+':'+identity
        entities.append({'id':eid,'ref':identity,'kind':kind,'title':row['title'],'client':row['client'],
                         'owner':row['emp'],'owner_name':names.get(row['emp']) or row.get('responsible'),
                         'status':row.get('stage'),'semantic':row.get('semantic'),'date':row.get('created'),
                         'amount':row.get('amount'),'links':source_data.get('deal_links',{}).get(identity,[]),
                         'url':object_url(config,'lead' if kind=='crm_lead' else 'deal',identity)})
    for row in con.execute('SELECT * FROM call_records'):
        row = dict(row)
        eid = 'bitrix:call:'+row['id']
        bindings = str(row.get('bindings') or '').split(',') + [row.get('crm_entity') or '',row.get('client_ref') or '']
        links = sorted({link for value in bindings if (link := _crm_id(value.strip()))})
        event = {'id':eid,'entity_id':eid,'at':row['dt'],'author':row['emp'],'author_name':names.get(row['emp']),
                 'kind':'call','description':('Входящий' if row['direction']=='in' else 'Исходящий')+' звонок',
                 'duration':row.get('duration'),'call_id':row['id'],'url':record_url(config,'call_records',row),'source':'bitrix'}
        entities.append({'id':eid,'ref':row['id'],'kind':'call','title':event['description'],'client':row['client'],
                         'owner':row['emp'],'owner_name':names.get(row['emp']),'date':row['dt'],'links':links,'url':event['url']})
        history.append(event)
    task_bindings = source_data.get('task_links',{})
    for row in con.execute('SELECT * FROM task_ledger'):
        row = dict(row)
        person = employees.get(str(row['uid']),{})
        eid = 'bitrix:task:'+str(row['id'])
        entities.append({'id':eid,'ref':str(row['id']),'kind':'task','title':row['title'],
                         'owner':person.get('login'),'owner_name':person.get('name'),'status':row['status'],
                         'date':row['created'],'due':row['deadline'],'closed_at':row['closed'],
                         'links':task_bindings.get(str(row['id']),[]),'url':object_url(config,'task',row['id'],row['uid'])})
        for record in json.loads(row.get('history') or '[]'):
            if len(record)<5:
                continue
            at, field, old, new, who = record[:5]
            actor = employees.get(str(who),{})
            history.append({'id':eid+':'+_digest(record),'entity_id':eid,'at':at,'author':actor.get('login'),
                            'author_name':actor.get('name'),'kind':'task_change','description':str(field)+' → '+str(new),
                            'status':str(new) if str(field).lower() in ('status','статус') else None,
                            'url':object_url(config,'task',row['id'],row['uid']),'source':'bitrix'})
    for row in source_data.get('activities',[]):
        owner = employees.get(str(row.get('RESPONSIBLE_ID')),{})
        creator = employees.get(str(row.get('AUTHOR_ID')),{})
        eid = 'bitrix:activity:'+str(row['ID'])
        links = []
        for binding_type, binding_id in activity_bindings(row):
            kind = {1:'LEAD',2:'DEAL',3:'CONTACT',4:'COMPANY'}.get(binding_type)
            link = _crm_id(str(kind)+':'+binding_id) if kind else None
            if link and link not in links:
                links.append(link)
        url = object_url(config,'activity',row['ID'])
        entities.append({'id':eid,'ref':str(row['ID']),'kind':'activity','title':row.get('SUBJECT'),
                         'owner':owner.get('login'),'owner_name':owner.get('name'),'date':row.get('CREATED'),
                         'due':row.get('DEADLINE') or row.get('START_TIME'),'status':row.get('COMPLETED'),
                         'closed_at':row.get('LAST_UPDATED') if row.get('COMPLETED')=='Y' else None,
                         'closed_at_verified':False,'links':links,'url':url})
        if row.get('CREATED'):
            meeting = str(row.get('TYPE_ID'))=='1'
            history.append({'id':eid+':created','entity_id':eid,'at':row['CREATED'],'author':creator.get('login'),
                            'author_name':creator.get('name'),'kind':'meeting_planned' if meeting else 'activity_created',
                            'description':('Назначена встреча. ' if meeting else '')+str(row.get('SUBJECT') or ''),
                            'url':url,'source':'bitrix'})
    capture(con,'bitrix',entities,history,observed_at)


def fetch_bitrix(b24, config, onec_entities=()):
    """Read explicit CRM bindings without inferring links from company or title."""
    activities = b24.list_all('crm.activity.list',{'filter':{'COMPLETED':'N'},'select':['*','BINDINGS'],'order':{'ID':'ASC'}})
    recent = b24.list_all('crm.activity.list',{'filter':{'>=LAST_UPDATED':(dt.date.today()-dt.timedelta(days=7)).isoformat()},
                                             'select':['*','BINDINGS'],'order':{'ID':'ASC'}})
    activities=list({str(row['ID']):row for row in [*activities,*recent]}.values())
    activities = [row for row in activities if str(row.get('TYPE_ID'))!='2' and row.get('PROVIDER_ID')!='VOXIMPLANT_CALL']
    bindings,errors=b24.batch({'binding'+str(row['ID']):('crm.activity.binding.list',{'activityId':row['ID']}) for row in activities})
    if errors or any('binding'+str(row['ID']) not in bindings for row in activities):
        raise ValueError('Не удалось прочитать явные связи дел Б24')
    for row in activities:
        row['BINDINGS']=bindings['binding'+str(row['ID'])]
    field = config.get('bitrix24',{}).get('fields',{}).get('deal_order_1c')
    deal_links = {}
    if field:
        field = b24.resolve_field('crm.deal.fields', field, '№ заказа клиента 1С')
        by_number = {}
        for entity in onec_entities:
            if entity.get('kind')=='order':
                by_number.setdefault(str(entity.get('number') or '').strip(),[]).append(entity['id'])
        for row in b24.list_all('crm.deal.list',{'select':['ID',field],'filter':{'!'+field:False}}):
            for value in row.get(field) if isinstance(row.get(field),list) else [row.get(field)]:
                matches = by_number.get(str(value or '').strip(),[])
                if len(matches)==1:
                    deal_links.setdefault(str(row['ID']),[]).append(matches[0])
    task_links = {}
    for task in b24.list_all('tasks.task.list',{'select':['ID','UF_CRM_TASK'],'filter':{'!UF_CRM_TASK':False}},key='tasks'):
        identity = str(task.get('id') or task.get('ID'))
        values = task.get('ufCrmTask') or task.get('UF_CRM_TASK') or []
        for value in values if isinstance(values,list) else [values]:
            reference = re.sub(r'^D_', 'DEAL:', str(value))
            reference = re.sub(r'^L_', 'LEAD:', reference)
            reference = re.sub(r'^CO_', 'COMPANY:', reference)
            reference = re.sub(r'^C_', 'CONTACT:', reference)
            if (link := _crm_id(reference)):
                task_links.setdefault(identity,[]).append(link)
    return {'activities':activities,'deal_links':deal_links,'task_links':task_links}
