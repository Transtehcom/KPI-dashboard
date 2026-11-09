"""Deterministic, source-read-only operational reports and price guidance."""
import datetime as dt
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from urllib.parse import quote, urlsplit

from . import operations
from .onec_sales_register import LOST_TENDER


SALE_KINDS = {'tender', 'kp', 'order', 'crm_deal', 'crm_lead'}
CLOSED = {'closed', 'completed', 'done', 'отработана', 'отработано', 'закрыта', 'закрыт', 'завершена', 'завершено', '5'}
CANCELLED = {'cancelled', 'canceled', 'отменена', 'отменен', 'отменено', 'отклонен'} | {value.casefold() for value in LOST_TENDER}
REOPENED = {'reopened', 'reopen', 'возвращена', 'возврат', 'открыта', 'open'}


def _iso(value):
    parsed = operations.local_datetime(value)
    return parsed.isoformat(timespec='seconds') if parsed else None


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _url(value):
    value = str(value or '').strip()
    if re.fullmatch(r'e1cib/data/[\w.]+\?ref=[a-zA-Z0-9-]+', value):
        return quote(value, safe='/:?=&%._-')
    if re.fullmatch(r'#operations-document=[a-zA-Z0-9%._-]+', value):
        return value
    try:
        return value if urlsplit(value).scheme.lower() in {'https', 'http', 'e1c'} else None
    except ValueError:
        return None


def _status(value):
    return str(value or '').strip().lower()


def _event_state(event):
    if event.get('observed_only') or event.get('time_verified') is False:
        return None
    kind, status = _status(event.get('kind')), _status(event.get('status'))
    if kind in {'closed', 'completed', 'close'} or status in CLOSED:
        return 'closed'
    if kind in {'reopened', 'reopen'} or status in REOPENED:
        return 'open'
    if status in CANCELLED:
        return 'cancelled'
    return None


def _links(entity):
    return [link for link in entity.get('links', []) if isinstance(link, str)]


def _staff(config):
    employees = config.get('employees', [])
    aliases = {}
    for employee in employees:
        for field in ('login', 'name', 'onec_manager', 'b24_user_id'):
            if employee.get(field) is not None:
                aliases[str(employee[field]).strip().casefold()] = employee['login']
    return employees, aliases


def _identity(value, aliases):
    if value is None:
        return None
    return aliases.get(str(value).strip().casefold(), str(value))


def _audit_rows(con, table, cutoff):
    return [dict(row) for row in con.execute(
        f'SELECT * FROM {table} WHERE created_at <= ? ORDER BY created_at, id',
        (cutoff.isoformat(timespec='seconds'),))]


def _write_identity(request_id, item_id, author):
    if not all(isinstance(value, str) and value.strip() for value in (request_id, item_id, author)):
        raise ValueError('Request, item and author are required')


def _target_signature(item):
    fields = ('unit', 'unit_id', 'characteristic', 'drawing', 'revision', 'steel',
              'coating', 'currency', 'vat_included', 'delivery', 'lot')
    target = {field: item.get(field) for field in fields}
    target['quantity'] = _number(item.get('quantity'))
    target['item_ref'] = item.get('item_ref') or item.get('nomenclature') or item.get('item_id')
    normalized = {field: ' '.join(value.split()).casefold() if isinstance(value, str) else value
                  for field, value in target.items()}
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def set_item_scope(con, request_id, item_id, included, reason, author):
    """Append an administrative scope decision without changing source rows."""
    _write_identity(request_id, item_id, author)
    if not isinstance(included, bool):
        raise ValueError('Included must be a boolean')
    if not included and not str(reason or '').strip():
        raise ValueError('An exclusion reason is required')
    at = _iso(dt.datetime.now(dt.timezone(dt.timedelta(hours=5))))
    con.execute('INSERT INTO operations_item_scope '
                '(request_id,item_id,included,reason,author,created_at) VALUES (?,?,?,?,?,?)',
                (request_id, item_id, int(included), str(reason or '').strip(), author, at))
    return {'request_id': request_id, 'item_id': item_id, 'included': included,
            'reason': str(reason or '').strip(), 'author': author, 'created_at': at}


def approve_price(con, request_id, item_id, supplier_id, author):
    """Record quantity comparability approval, retaining all quote conditions."""
    _write_identity(request_id, item_id, author)
    if not isinstance(supplier_id, str) or not supplier_id.strip():
        raise ValueError('Supplier is required')
    request = operations.get_entity(con, 'onec', request_id)
    item = next((item for item in (request or {}).get('items', []) if item.get('id') == item_id), None)
    if item is None or (request or {}).get('kind') != 'supply':
        raise ValueError('Current supply request item was not found')
    if (_number(item.get('quantity')) or 0) <= 0:
        raise ValueError('A positive target quantity is required')
    signature = _target_signature(item)
    at = _iso(dt.datetime.now(dt.timezone(dt.timedelta(hours=5))))
    con.execute('INSERT INTO operations_price_approval '
                '(request_id,item_id,supplier_id,author,created_at,target_signature) VALUES (?,?,?,?,?,?)',
                (request_id, item_id, supplier_id, author, at, signature))
    return {'request_id': request_id, 'item_id': item_id, 'supplier_id': supplier_id,
            'author': author, 'created_at': at, 'target_signature': signature}


def approve_deadline(con, request_id, due, reason, author):
    """Append a reasoned approval of a document's effective deadline."""
    _write_identity(request_id, request_id, author)
    parsed = operations.deadline(due)
    if parsed is None or not str(reason or '').strip():
        raise ValueError('A deadline and approval reason are required')
    at = _iso(dt.datetime.now(dt.timezone(dt.timedelta(hours=5))))
    con.execute('INSERT INTO operations_deadline_approval '
                '(request_id,due,reason,author,created_at) VALUES (?,?,?,?,?)',
                (request_id, _iso(parsed), str(reason).strip(), author, at))
    return {'request_id': request_id, 'due': _iso(parsed), 'reason': str(reason).strip(),
            'author': author, 'created_at': at}


def price_history(con, item, cutoff, quantity=None):
    """Latest comparable offer per supplier, with excluded evidence retained."""
    cutoff = operations.local_datetime(cutoff)
    if cutoff is None:
        raise ValueError('Cutoff is required')
    versions = operations.versions(con, 'onec', cutoff)
    approvals = _audit_rows(con, 'operations_price_approval', cutoff)
    return _price_history(item, cutoff, quantity, versions, approvals)


def _price_history(item, cutoff, quantity, versions, approval_rows):
    quantity = _number(item.get('quantity') if quantity is None else quantity)
    target_signature = _target_signature(dict(item, quantity=quantity))
    approvals = {row['supplier_id']: row for row in approval_rows
                 if row['request_id'] == item.get('request_id') and row['item_id'] == item.get('id')
                 and row.get('target_signature') == target_signature}
    evidence = {}
    target = item.get('nomenclature') or item.get('item_id')
    for entity in versions:
        observed = operations.local_datetime(entity.get('_observed_at'))
        for source_item in entity.get('items') or []:
            identity = source_item.get('nomenclature') or source_item.get('item_id')
            if not target or identity != target:
                continue
            if any(source_item.get(key) != item.get(key) for key in ('characteristic', 'lot')):
                continue
            for raw in source_item.get('offers') or []:
                offer = dict(raw)
                supplier = str(offer.get('supplier_id') or '')
                offer['request_id'] = entity['id']
                offer['item_id'] = source_item.get('id')
                offer['observed_at'] = _iso(observed)
                offered = operations.local_datetime(offer.get('offered_at') or offer.get('quote_date'))
                first_seen = operations.local_datetime(offer.get('first_seen_at'))
                offer['price_date'] = _iso(offered or (first_seen if not offer.get('is_initial', True) else None))
                offer['date_verified'] = offered is not None
                price_date = operations.local_datetime(offer['price_date'])
                reasons = []
                price = _number(offer.get('price'))
                if not supplier:
                    reasons.append('supplier_unknown')
                if price is None or price <= 0:
                    reasons.append('nonpositive_price')
                if not price_date:
                    reasons.append('price_date_unknown')
                elif price_date > cutoff:
                    reasons.append('future_price')
                elif (cutoff.date() - price_date.date()).days > 30:
                    reasons.append('older_than_30_days')
                valid_until = operations.deadline(offer.get('valid_until'))
                if valid_until and valid_until < cutoff:
                    reasons.append('expired')
                if observed and observed > cutoff:
                    reasons.append('not_observed')
                for field in ('unit', 'currency', 'vat_included', 'delivery'):
                    source_value = offer.get(field)
                    target_value = item.get(field)
                    if source_value is None or source_value == '' or target_value is None or target_value == '':
                        reasons.append(field + '_unknown')
                    elif source_value != target_value:
                        reasons.append(field + '_mismatch')
                offered_quantity = _number(offer.get('quantity'))
                lower, upper = _number(offer.get('quantity_min')), _number(offer.get('quantity_max'))
                exact = quantity is not None and offered_quantity == quantity and lower is None and upper is None
                ranged = quantity is not None and lower is not None and upper is not None and lower <= quantity <= upper
                approval = approvals.get(supplier)
                if not (exact or ranged or approval):
                    reasons.append('quantity_mismatch')
                own = entity['id'] == item.get('request_id') and source_item.get('id') == item.get('id')
                excluded_id = item.get('comparison_offer_id') or item.get('offer_id')
                if own and (excluded_id is None or excluded_id == offer.get('id')):
                    reasons.append('own_quote')
                offer.update(eligible=not reasons, exclusion_reasons=reasons, approval=approval,
                             quantity_approved=approval is not None)
                signature = (entity['id'], source_item.get('id'), offer.get('id'), supplier,
                             str(offer.get('price')), offer.get('price_date'), str(offer.get('quantity')),
                             str(offer.get('unit')), str(offer.get('currency')), str(offer.get('vat_included')),
                             str(offer.get('delivery')), str(offer.get('valid_until')),
                             str(offer.get('quantity_min')), str(offer.get('quantity_max')), offer.get('first_seen_at'))
                evidence[signature] = offer
    latest = {}
    offers = sorted(evidence.values(), key=lambda offer: (offer.get('price_date') or '', offer.get('observed_at') or '', str(offer.get('id') or '')))
    for offer in offers:
        if offer['eligible']:
            latest[offer['supplier_id']] = offer
    prices = [_number(offer['price']) for offer in latest.values()]
    median = statistics.median(prices) if prices else None
    selected = _number(item.get('selected_price'))
    return {'median': median, 'min': min(prices) if prices else None,
            'max': max(prices) if prices else None, 'supplier_count': len(prices),
            'offers': offers, 'estimate': median * quantity if median is not None and quantity is not None else None,
            'amber': bool(len(prices) >= 2 and selected is not None and selected > max(prices))}


def _record(entity, events, cutoff, aliases, employee_map, lean=False):
    row = dict(entity)
    row['links'] = _links(entity)
    row['url'] = None if lean else _url(row.get('url')) or '#operations-document=' + quote(row['id'], safe='')
    row['title'] = row.get('title') or row.get('number') or row['id']
    owner = _identity(row.get('owner') or row.get('owner_name'), aliases)
    row['owner'] = owner
    row['owner_name'] = employee_map.get(owner, {}).get('name') or row.get('owner_name') or owner
    display_due = operations.deadline(row.get('due'))
    due = display_due if row.get('due_verified') is not False else None
    if due:
        due = due.replace(microsecond=0)
    closed_at = operations.local_datetime(row.get('closed_at'))
    status = _status(row.get('status'))
    submission_finished = row.get('kind') == 'tender' and status == 'выиграно'
    if submission_finished:
        due = None
    closing_events = [event for event in events if _event_state(event)]
    last = max(closing_events, key=lambda event: (event.get('at') or '', event.get('id') or '')) if closing_events else None
    activity_closed = row.get('kind') == 'activity' and status == 'y'
    state = 'closed' if status in CLOSED or row.get('semantic') in {'S', 'F'} or activity_closed else 'cancelled' if status in CANCELLED else 'open'
    conflict = False
    if last:
        event_state = _event_state(last)
        conflict = event_state != state
        if state == 'closed' and event_state == 'closed':
            event_at = operations.local_datetime(last.get('at'))
            if event_at and closed_at and event_at.replace(microsecond=0) != closed_at.replace(microsecond=0):
                conflict = True
            if event_at:
                closed_at = event_at
    if closed_at and closed_at > cutoff:
        conflict = True
    if state == 'open' and closed_at:
        # A reopening retains previous closure as history without remaining overdue-closed.
        conflict = not (last and _event_state(last) == 'open')
        closed_at = None
    late = None
    if conflict or (state == 'closed' and (closed_at is None or row.get('closed_at_verified') is False)):
        timeliness = 'review'
    elif state == 'closed':
        late = int((closed_at - due).total_seconds()) if due else None
        timeliness = 'unknown' if due is None else 'late' if late > 0 else 'on_time'
    elif state == 'cancelled':
        timeliness = 'cancelled'
    else:
        late = max(0, int((cutoff - due).total_seconds())) if due else None
        timeliness = 'unknown' if due is None else 'overdue' if late > 0 else 'open'
    row.update(due=_iso(display_due), closed_at=_iso(closed_at), late_seconds=late,
               timeliness=timeliness, is_closed=state == 'closed', is_cancelled=state == 'cancelled',
               asof=_iso(cutoff), submission_obligation_finished=submission_finished)
    return row


def _items(row, cutoff, scopes, history_context=None):
    items, selected_values, estimates = [], [], []
    selected_complete = True
    for raw in row.get('items') or []:
        item = dict(raw)
        item['request_id'] = row['id']
        scope = scopes.get((row['id'], item.get('id')))
        item['scope'] = scope
        item['included'] = bool(scope['included']) if scope else True
        offers = item.get('offers') or []
        positive = [offer for offer in offers if (_number(offer.get('price')) or 0) > 0]
        selected = next((offer for offer in positive if offer.get('selected') or
                         offer.get('supplier_id') == item.get('selected_supplier_id')), None)
        selected_price = _number(item.get('selected_price'))
        if selected is None and selected_price is not None and selected_price > 0 and item.get('selected_supplier_id'):
            selected = {'supplier_id': item['selected_supplier_id'], 'price': selected_price}
        item['filled'] = bool(positive or selected)
        item['selected_offer'] = selected
        if history_context is not None:
            indexed, approvals = history_context
            key = (item.get('nomenclature') or item.get('item_id'), item.get('characteristic'), item.get('lot'))
            item['history'] = _price_history(item, cutoff, None, indexed.get(key, []), approvals)
        else:
            item['history'] = None
        item['historical_estimate'] = item['history']['estimate'] if not item['filled'] and item['history'] else None
        if item['included']:
            quantity = _number(item.get('quantity'))
            if selected and quantity is not None:
                selected_values.append(_number(selected['price']) * quantity)
            elif selected:
                selected_complete = False
            if item['historical_estimate'] is not None:
                estimates.append(item['historical_estimate'])
        items.append(item)
    required = [item for item in items if item['included']]
    units = {(item.get('currency'), item.get('vat_included')) for item in required}
    comparable_sum = len(units) <= 1 and all(currency is not None and vat is not None for currency, vat in units)
    row.update(items=items if history_context is not None else [], item_count=len(items),
               estimates_loaded=history_context is not None,
               required_count=len(required), filled_count=sum(item['filled'] for item in required),
               readiness=sum(item['filled'] for item in required) / len(required) if required else None,
               selected_total=sum(selected_values) if comparable_sum and selected_complete and selected_values else None,
               estimate_total=sum(estimates) if comparable_sum and estimates else None,
               unestimated_count=sum(not item['filled'] and item['historical_estimate'] is None for item in required))


def _summarize_items(row, scopes):
    required = filled = 0
    selected_values = []
    selected_complete = True
    bases = set()
    raw_items = row.get('items') or []
    for item in raw_items:
        scope = scopes.get((row['id'], item.get('id')))
        if scope and not scope['included']:
            continue
        required += 1
        selected_price = _number(item.get('selected_price'))
        has_price = item.get('has_price')
        if has_price is None:
            positive = [offer for offer in item.get('offers') or [] if (_number(offer.get('price')) or 0) > 0]
            selected = next((offer for offer in positive if offer.get('selected') or
                             offer.get('supplier_id') == item.get('selected_supplier_id')), None)
            if selected:
                selected_price = _number(selected.get('price'))
            has_price = bool(positive)
        has_selected = selected_price is not None and selected_price > 0 and bool(item.get('selected_supplier_id'))
        filled += bool(has_price or has_selected)
        bases.add((item.get('currency'), item.get('vat_included')))
        if has_selected:
            quantity = _number(item.get('quantity'))
            if quantity is None:
                selected_complete = False
            else:
                selected_values.append(selected_price * quantity)
    comparable = len(bases) <= 1 and all(currency is not None and vat is not None for currency, vat in bases)
    row.update(items=[], item_count=len(raw_items), required_count=required, filled_count=filled,
               readiness=filled / required if required else None,
               selected_total=sum(selected_values) if comparable and selected_complete and selected_values else None,
               estimates_loaded=False, estimate_total=None, unestimated_count=required - filled)


def _document_reference(document):
    fields = ('id', 'kind', 'title', 'status', 'due', 'closed_at', 'owner_name', 'timeliness', 'item_count', 'best_price_total',
              'submission_obligation_finished')
    result = {key: document[key] for key in fields if document.get(key) is not None and document.get(key) != ''}
    if len(str(result.get('title') or '')) > 100:
        result['title'] = str(result['title'])[:99] + '…'
    return result


def _report_record(row):
    fields = ('id', 'kind', 'title', 'client', 'owner', 'owner_name', 'status', 'due', 'closed_at',
              'late_seconds', 'timeliness', 'links', 'date', 'amount', 'currency', 'vat_included',
              'asof', 'item_count', 'required_count', 'filled_count', 'readiness', 'selected_total',
              'estimate_total', 'unestimated_count', 'group', 'due_change_pending', 'deadline_approval',
              'action_context', 'estimates_loaded', 'submission_obligation_finished', 'active_obligation_id', 'best_price_total')
    result = {key: row[key] for key in fields if row.get(key) is not None and row.get(key) != '' and row.get(key) != []}
    for key, limit in (('title', 140), ('client', 100), ('owner_name', 80)):
        value = result.get(key)
        if isinstance(value, str) and len(value) > limit:
            result[key] = value[:limit - 1] + '…'
    if row.get('due_change_pending'):
        result['due_raw'] = row.get('due_raw') or row.get('current_due')
    result['url'] = '#operations-document=' + quote(row['id'], safe='')
    result['items'] = []
    result['documents'] = [_document_reference(document) for document in row.get('documents') or []]
    return result


def _resolve_deadlines(entities, versions, approvals):
    original_due = {}
    for version in versions:
        if version['id'] not in original_due and operations.deadline(version.get('due')):
            original_due[version['id']] = version['due']
    for entity in entities:
        current_due = operations.deadline(entity.get('due'))
        approval = approvals.get(entity['id'])
        effective_due = operations.deadline(approval['due'] if approval else original_due.get(entity['id']))
        current_due = current_due.replace(microsecond=0) if current_due else None
        effective_due = effective_due.replace(microsecond=0) if effective_due else None
        entity.update(due_raw=entity.get('due'), current_due=_iso(current_due),
                      prior_due=_iso(operations.deadline(original_due.get(entity['id']))),
                      due_change_pending=current_due is not None and current_due != effective_due,
                      deadline_approval=approval, due=_iso(effective_due))
        if approval:
            entity['due_verified'] = True


def document_detail(con, config, entity_id, cutoff):
    """Enrich one document on demand without expanding the entire report."""
    cutoff = operations.local_datetime(cutoff)
    if cutoff is None:
        raise ValueError('Cutoff is required')
    _, aliases = _staff(config)
    employee_map = {employee['login']: employee for employee in config.get('employees', [])}
    for source in ('onec', 'bitrix'):
        entity = operations.get_entity(con, source, entity_id, cutoff)
        if entity is None:
            continue
        versions = operations.versions(con, source, cutoff)
        approvals = {row['request_id']: row for row in _audit_rows(con, 'operations_deadline_approval', cutoff)}
        _resolve_deadlines([entity], versions, approvals)
        fresh = operations.freshness(con, cutoff)[source]
        asof = operations.local_datetime(fresh.get('updated_at')) or cutoff
        events = [event for event in operations.events(con, source, cutoff) if event.get('entity_id') == entity_id]
        row = _record(entity, events, min(cutoff, asof), aliases, employee_map)
        if entity.get('kind') == 'supply':
            indexed = defaultdict(list)
            for version in versions:
                for item in version.get('items') or []:
                    key = (item.get('nomenclature') or item.get('item_id'), item.get('characteristic'), item.get('lot'))
                    indexed[key].append({'id': version['id'], '_observed_at': version.get('_observed_at'), 'items': [item]})
            scopes = {(audit['request_id'], audit['item_id']): audit for audit in _audit_rows(con, 'operations_item_scope', cutoff)}
            _items(row, cutoff, scopes, (indexed, _audit_rows(con, 'operations_price_approval', cutoff)))
        row['events'] = events
        return row
    return None


def _sales_records(documents):
    nodes = {row['id']: row for row in documents if row.get('kind') in SALE_KINDS | {'supply'}}
    by_id = {row['id']: row for row in documents}
    neighbors = defaultdict(set)
    all_neighbors = defaultdict(set)
    for row in documents:
        for link in row['links']:
            if link in by_id:
                all_neighbors[row['id']].add(link)
                all_neighbors[link].add(row['id'])
            if row['id'] in nodes and link in nodes:
                neighbors[row['id']].add(link)
                neighbors[link].add(row['id'])
    visited, records = set(), []
    rank = {'tender': 0, 'kp': 1, 'crm_deal': 2, 'crm_lead': 3, 'order': 4, 'supply': 5}
    for child_kind, fulfilled_kinds in [('order', {'kp', 'tender'}), ('kp', {'tender'})]:
        parents = [child['id'] for child in nodes.values() if child['kind'] == child_kind and not child['is_cancelled']]
        seen = set()
        while parents:
            parent_id = parents.pop()
            if parent_id in seen or parent_id not in nodes:
                continue
            seen.add(parent_id)
            parent = nodes[parent_id]
            if parent['kind'] in fulfilled_kinds:
                parent['submission_obligation_finished'] = True
            parents.extend(parent['links'])
    for identity in sorted(nodes):
        if identity in visited:
            continue
        pending, group = [identity], set()
        while pending:
            current = pending.pop()
            if current in group:
                continue
            group.add(current)
            pending.extend(neighbors[current] - group)
        visited.update(group)
        linked_ids = set(group)
        linked_ids.update(link for key in group for link in all_neighbors[key] if by_id[link].get('kind') == 'transport')
        activity_ids = {link for key in linked_ids for link in all_neighbors[key]
                        if by_id[link].get('kind') in {'call', 'task', 'activity'}}
        linked_ids.update(activity_ids)
        linked = [by_id[key] for key in sorted(linked_ids)]
        anchors = sorted((nodes[key] for key in group), key=lambda row: (rank[row['kind']], row['id']))
        row = dict(anchors[0])
        row['documents'] = linked
        row['links'] = sorted({link for document in linked for link in document['links']})
        active = [document for document in linked if not document['is_closed'] and not document['is_cancelled']
                  and not document.get('submission_obligation_finished') and document['kind'] != 'call']
        effective = [document for document in anchors if not document.get('submission_obligation_finished')]
        row['open_obligations'] = active
        row['is_closed'] = bool(effective) and not active and all(document['is_closed'] or document['is_cancelled'] for document in effective)
        row['is_cancelled'] = bool(effective) and not active and all(document['is_cancelled'] for document in effective)
        overdue = [document for document in active if document['timeliness'] == 'overdue']
        if active:
            next_obligation = min(overdue or active, key=lambda document: (
                not bool(document.get('due')) or document.get('due_verified') is False,
                document.get('due') or '', rank.get(document['kind'], 6), document['id']))
            for field in ('status', 'due', 'closed_at', 'late_seconds', 'timeliness', 'asof', 'due_change_pending',
                          'due_raw', 'current_due', 'prior_due', 'deadline_approval', 'due_verified', 'closed_at_verified'):
                row[field] = next_obligation.get(field)
            row['active_obligation_id'] = next_obligation['id']
        elif effective:
            terminal = max(effective, key=lambda document: (rank.get(document['kind'], 6), document['id']))
            for field in ('status', 'due', 'closed_at', 'late_seconds', 'timeliness', 'asof', 'due_change_pending',
                          'due_raw', 'current_due', 'prior_due', 'deadline_approval', 'due_verified', 'closed_at_verified'):
                row[field] = terminal.get(field)
        else:
            row.update(due=None, closed_at=None, late_seconds=None, timeliness='open', due_change_pending=False)
        records.append(row)
    return records


def build(con, config, kind, date_from, date_to, emp='Все', channel='Все', now=None):
    """Build period actions and an observation-time bounded document queue."""
    if kind not in {'sales', 'supply'}:
        raise ValueError('Unknown operations panel')
    start = operations.local_datetime(date_from)
    end = operations.deadline(date_to)
    current = operations.local_datetime(now) if now else dt.datetime.now(dt.timezone(dt.timedelta(hours=5))).replace(tzinfo=None)
    if start is None or end is None or start > end:
        raise ValueError('Invalid report period')
    cutoff = min(end, current)
    history_start = operations.baseline_started(con)
    baseline = operations.local_datetime(history_start)
    available = baseline is not None and cutoff >= baseline
    warnings = []
    if baseline is None:
        warnings.append('История ещё не загружена')
    elif start < baseline:
        warnings.append('История доступна с ' + _iso(baseline))
    employees, aliases = _staff(config)
    employee_map = {employee['login']: employee for employee in employees}
    source_freshness = operations.freshness(con, cutoff)
    for source, info in source_freshness.items():
        if isinstance(info, dict) and info.get('stale'):
            warnings.append('Данные ' + source + ' устарели')
    entities, events = [], []
    if available:
        for source in ('onec', 'bitrix'):
            entities.extend(dict(entity, _source=source) for entity in operations.snapshot(con, source, cutoff, lean=True))
            events.extend(dict(event, _source=source) for event in operations.events(con, source, cutoff))
    submission_history_available = True
    for entity in entities:
        if entity.get('kind') == 'source_meta':
            if entity.get('capabilities',{}).get('submission_history') is False:
                submission_history_available = False
            for warning in (entity.get('warnings') or [])[:50]:
                text = str(warning)[:1000]
                if text and text not in warnings:
                    warnings.append(text)
    entities = [entity for entity in entities if entity.get('kind') != 'source_meta']
    event_map = defaultdict(list)
    unique_events = {}
    for event in events:
        event = dict(event)
        at = operations.local_datetime(event.get('at'))
        if at is None or at > cutoff:
            continue
        event['at'] = _iso(at)
        event['author'] = _identity(event.get('author') or event.get('author_name'), aliases)
        employee = employee_map.get(event['author'], {})
        event['author_name'] = employee.get('name') or event.get('author_name') or event['author']
        event['department'] = employee.get('department')
        unique_events[event['id']] = event
    for event in unique_events.values():
        event_map[event.get('entity_id')].append(event)
    queue_asof = {source: _iso(min(cutoff, operations.local_datetime(info.get('updated_at'))))
                  if info.get('updated_at') else None for source, info in source_freshness.items()}
    versions = operations.versions(con, 'onec', cutoff, lean=True) + operations.versions(con, 'bitrix', cutoff, lean=True)
    deadline_approvals = {row['request_id']: row for row in _audit_rows(con, 'operations_deadline_approval', cutoff)}
    _resolve_deadlines(entities, versions, deadline_approvals)
    documents = [_record(entity, event_map[entity['id']],
                         operations.local_datetime(queue_asof.get(entity['_source'])) or cutoff,
                         aliases, employee_map, lean=True) for entity in entities]
    scopes = {(row['request_id'], row['item_id']): row for row in _audit_rows(con, 'operations_item_scope', cutoff)}
    supply_documents = [row for row in documents if row.get('kind') == 'supply']
    for row in supply_documents:
        _summarize_items(row, scopes)
    document_map = {row['id']: row for row in documents}
    linked_documents = defaultdict(set)
    for row in documents:
        if row.get('kind') != 'supply':
            row['item_count'] = len(row.get('items') or [])
            row['items'] = []
        row['events'] = []
        for link in row['links']:
            if link in document_map:
                linked_documents[row['id']].add(link)
                linked_documents[link].add(row['id'])
    records = supply_documents if kind == 'supply' else _sales_records(documents)
    for row in records:
        if kind == 'supply':
            row['documents'] = [{key: value for key, value in document.items() if key != 'documents'}
                                for document in (document_map[key] for key in sorted(linked_documents[row['id']] | {row['id']}))]
        employee = employee_map.get(row.get('owner'), {})
        row['group'] = employee.get('department') or ('unassigned' if not row.get('owner') else 'other')
    if channel != 'Все':
        records = [row for row in records if row.get('channel') == channel or
                   (channel in {'tender', 'tenders', 'Тендеры'} and any(doc.get('kind') == 'tender' for doc in row['documents'])) or
                   (channel in {'direct', 'pm', 'Прямые', 'Прямые продажи'} and not any(doc.get('kind') == 'tender' for doc in row['documents']))]
    record_for_document = defaultdict(list)
    for row in records:
        for document in row['documents']:
            record_for_document[document['id']].append(row['id'])
    period_events = []
    event_start = max(start, baseline) if baseline else start
    entity_kinds = {entity['id']: entity.get('kind') for entity in entities}
    for event in sorted(unique_events.values(), key=lambda row: (row['at'], row['id']), reverse=True):
        source_start = operations.local_datetime(source_freshness.get(event.get('_source'), {}).get('started_at'))
        source_start = max(event_start, source_start) if source_start else event_start
        if source_start <= operations.local_datetime(event['at']) <= cutoff:
            related = record_for_document.get(event.get('entity_id'), [])
            if emp != 'Все' and event.get('author') != emp:
                continue
            standalone_activity = kind == 'sales' and channel == 'Все' and entity_kinds.get(event.get('entity_id')) in {'call', 'task', 'activity'}
            if not related and not standalone_activity:
                continue
            event = dict(event, record_ids=related, record_id=related[0] if related else None,
                         url=_url(event.get('url')))
            period_events.append(event)
    def in_period(value, source=None):
        parsed = operations.local_datetime(value)
        source_start = operations.local_datetime(source_freshness.get(source, {}).get('started_at'))
        period_start = max(event_start, source_start) if source_start else event_start
        return parsed is not None and period_start <= parsed <= cutoff
    action_records = {record_id for event in period_events for record_id in event['record_ids']}
    visible = []
    for row in records:
        owned = emp == 'Все' or row.get('owner') == emp
        current = not row['is_closed'] and not row['is_cancelled']
        period_fact = in_period(row.get('date'), row.get('_source')) or in_period(row.get('closed_at'), row.get('_source'))
        if (owned and (current or period_fact or row['timeliness'] == 'review')) or row['id'] in action_records:
            row['action_context'] = not owned
            visible.append(row)
    records = visible
    owned_records = [row for row in records if not row['action_context']]
    created = [row for row in owned_records if in_period(row.get('date'), row.get('_source'))]
    closed = [row for row in owned_records if row['is_closed'] and not row['is_cancelled'] and in_period(row.get('closed_at'), row.get('_source'))]
    known = [row for row in closed if row['timeliness'] in {'on_time', 'late'}]
    on_time = [row for row in known if row['timeliness'] == 'on_time']
    queue = [row for row in owned_records if not row['is_closed'] and not row['is_cancelled']]
    overdue = [row for row in queue if row['timeliness'] == 'overdue']
    due_today = [row for row in queue if row.get('due_verified') is not False and
                 (operations.local_datetime(row.get('due')) or dt.datetime.min).date() == end.date()]
    closed_late = [row for row in known if row['timeliness'] == 'late']
    without_plan = [row for row in closed if not row.get('due') or row.get('due_verified') is False]
    without_fact = [row for row in owned_records if row['is_closed'] and not row['is_cancelled'] and
                    (not row.get('closed_at') or row.get('closed_at_verified') is False)]
    results = {'created': len(created), 'closed': len(closed), 'known_timeliness': len(known),
               'on_time': len(on_time), 'on_time_rate': len(on_time) / len(known) if known else None,
               'open': len(queue), 'overdue': len(overdue), 'due_today': len(due_today),
               'closed_late': len(closed_late), 'closed_without_plan': len(without_plan),
               'closed_without_fact': len(without_fact)}
    tiles = [{'key': key, 'title': title, 'value': len(rows), 'record_ids': [row['id'] for row in rows]}
             for key, title, rows in [('created', 'Созданы', created), ('closed', 'Закрыты', closed),
                                      ('open', 'В работе', queue), ('overdue', 'Просрочены', overdue)]]
    tiles.append({'key': 'events', 'title': 'Действия', 'value': len(period_events), 'event_ids': [event['id'] for event in period_events]})
    if kind == 'supply':
        for key, title, rows in [('due_today', 'Срок сегодня', due_today), ('closed_late', 'Закрыты с опозданием', closed_late),
                                  ('closed_without_plan', 'Закрыты без плана', without_plan), ('closed_without_fact', 'Закрыты без даты факта', without_fact)]:
            tiles.append({'key': key, 'title': title, 'value': len(rows), 'record_ids': [row['id'] for row in rows]})
    if kind == 'sales':
        for key, title, kinds in [('calls', 'Звонки', {'call'}), ('tasks', 'Задачи', {'task_change', 'task_completed', 'task_created'}),
                                  ('meetings', 'Назначены встречи', {'meeting_planned'}), ('submitted', 'Поданы КП и тендеры', {'submitted'})]:
            matching = [event for event in period_events if event.get('kind') in kinds and not event.get('observed_only')]
            value = None if key == 'submitted' and not submission_history_available else len(matching)
            tiles.append({'key': key, 'title': title, 'value': value, 'event_ids': [event['id'] for event in matching]})
        completed_meetings = [event for event in period_events if event.get('kind') == 'meeting' and not event.get('observed_only')]
        if completed_meetings:
            tiles.append({'key': 'completed_meetings', 'title': 'Проведены встречи',
                          'value': len(completed_meetings), 'event_ids': [event['id'] for event in completed_meetings]})
    tiles.append({'key': 'on_time_rate', 'title': 'В срок', 'value': results['on_time_rate'], 'record_ids': [row['id'] for row in known]})
    people = []
    identities = {row.get('owner') for row in records} | {event.get('author') for event in period_events}
    for login in sorted(identities, key=lambda value: str(value or '')):
        employee = employee_map.get(login, {})
        person = {'login': login, 'name': employee.get('name') or login or 'Не назначен',
                  'department': employee.get('department'), 'group': employee.get('department') or ('unassigned' if login is None else 'other')}
        for key, rows in [('created', created), ('closed', closed), ('open', queue), ('overdue', overdue),
                          ('due_today', due_today), ('closed_late', closed_late)]:
            person[key] = sum(row.get('owner') == login for row in rows)
        person['events'] = sum(event.get('author') == login for event in period_events)
        people.append(person)
    projected = {row['id']: _report_record(row) for row in records}
    return {'kind': kind, 'date_from': start.date().isoformat(), 'date_to': end.date().isoformat(),
            'cutoff': _iso(cutoff), 'history_start': history_start,
            'coverage': {'sources': source_freshness, 'available': available, 'from': history_start,
                         'to': _iso(cutoff), 'queue_asof': queue_asof},
            'employees': employees, 'tiles': tiles, 'records': list(projected.values()),
            'queue': [projected[row['id']] for row in queue],
            'events': period_events, 'results': results, 'people': people, 'warnings': warnings}
