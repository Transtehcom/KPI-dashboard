"""Направления CRM из тегов карточки и согласованного правила БПЛА."""
import json
import re


DIRECTIONS = {'epc': 'EPC', 'bpla': 'БПЛА', 'kpi': 'KPI',
              'investments': 'Инвестиции', 'factoring': 'Факторинг'}


def normalize_tags(value):
    if isinstance(value, str) and value.lstrip().startswith('['):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    values = value if isinstance(value, list) else [value]
    aliases = {label.casefold(): label for label in DIRECTIONS.values()}
    aliases['ерс'] = 'EPC'
    aliases['epc подрядчики'] = aliases['ерс подрядчики'] = 'EPC'
    result = []
    for raw in values:
        for tag in re.split(r'[,;\n|]+', str(raw or '')):
            tag = tag.strip()
            tag = aliases.get(tag.casefold(), tag)
            if tag and tag not in result:
                result.append(tag)
    return result


def selected_directions(value):
    if value is None:
        return None
    chosen = value.split(',') if isinstance(value, str) else list(value)
    if chosen == ['all'] or not chosen:
        return None
    if chosen == ['tagged']:
        return list(DIRECTIONS)
    if any(key not in DIRECTIONS for key in chosen):
        raise ValueError('Неизвестное направление')
    return list(dict.fromkeys(chosen))


def matches_directions(row, chosen):
    if chosen is None:
        return True
    tags = set(normalize_tags(row.get('tags')))
    if any(DIRECTIONS[key] in tags for key in chosen):
        return True
    strict = 'kpi' in chosen and 'bpla' in chosen
    return 'bpla' in chosen and not strict and 'бпла' in str(row.get('title') or '').casefold()
