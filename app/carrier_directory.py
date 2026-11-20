"""Directory from the transport company field of synchronized forwarder orders."""
from io import BytesIO
import json
from uuid import UUID

from .supplier_directory import contacts, read_all
from .supplier_format import region


CARD_FIELDS = ('ИНН', 'Регион', 'Рабочий телефон', 'Мобильный телефон', 'Рабочий e-mail', 'Корпоративный сайт')
COLUMNS = ('Перевозчик', 'Поручений', 'Последнее поручение', 'Дата поручения', 'Маршрут последнего поручения', *CARD_FIELDS)


def by_keys(onec, entity, select, field, keys):
    keys = sorted({str(UUID(key)) for key in keys if key and UUID(key).int})
    for start in range(0, len(keys), 20):
        condition = ' or '.join(f"{field} eq guid'{key}'" for key in keys[start:start+20])
        yield from read_all(onec, entity, select, f'DeletionMark eq false and ({condition})')


def cards(onec, keys):
    keys = set(keys) - {None, '', '00000000-0000-0000-0000-000000000000'}
    if not keys:
        return {}
    kinds = {r['Ref_Key']:r['Description'] for r in read_all(onec, 'Catalog_ВидыКонтактнойИнформации', 'Ref_Key,Description')}
    partners = {r['Ref_Key']:contacts(r, kinds) for r in by_keys(onec, 'Catalog_Партнеры',
                'Ref_Key,КонтактнаяИнформация', 'Ref_Key', keys)}
    legal = {}
    for r in by_keys(onec, 'Catalog_Контрагенты', 'Ref_Key,Description,ИНН,Партнер_Key,КонтактнаяИнформация', 'Партнер_Key', keys):
        legal.setdefault(r['Партнер_Key'], []).append(r)
    result = {}
    for key in keys:
        merged = {}
        entities = legal.get(key) or [{}]
        for entity in entities:
            fields = {**partners.get(key, {}), **contacts(entity, kinds), 'ИНН': entity.get('ИНН')}
            for field in CARD_FIELDS:
                value = str(fields.get(field) or '').strip()
                if value:
                    if field == 'Регион':
                        value = region(value) or value
                    if len(entities) > 1:
                        value = f"{entity['Description']}: {value}"
                    merged.setdefault(field, []).append(value)
        result[key] = json.dumps({f:'; '.join(dict.fromkeys(v)) for f,v in merged.items()}, ensure_ascii=False)
    return result


def rows(con):
    carriers = {}
    for name, doc, day, route, key, card in con.execute("""
        SELECT trim(carrier), doc, dt, route, carrier_key, carrier_card FROM forwarder_orders
        WHERE coalesce(deleted,0)=0 AND trim(coalesce(carrier,''))<>''
        ORDER BY dt DESC, created_at DESC, doc DESC, id DESC
    """):
        fields = json.loads(card or '{}')
        row = carriers.setdefault(key or name.casefold(), [name, 0, doc, day, route, *(fields.get(f) for f in CARD_FIELDS)])
        row[1] += 1
    return sorted(carriers.values(), key=lambda row: row[0].casefold())


def workbook(rows):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = 'Перевозчики'
    for values in (COLUMNS, *rows):
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    sheet.freeze_panes = 'B2'
    sheet.auto_filter.ref = sheet.dimensions
    for col, width in zip('ABCDEFGHIJK', (42, 15, 25, 20, 65, 18, 32, 30, 30, 40, 38)):
        sheet.column_dimensions[col].width = width
    output = BytesIO()
    book.save(output)
    return output.getvalue()
