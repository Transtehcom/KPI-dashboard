"""База поставщиков из 1С и выгрузка в исходном 81-колоночном формате Б24."""
import datetime as dt
import io
import json
from pathlib import Path
import threading

from .common import db_lock
from .supplier_categories import CATEGORIES, classify
from .supplier_format import company_name, region

HEADERS = json.loads(Path(__file__).with_name('supplier_directory_columns.json').read_text('utf-8'))
DISPLAY = ['Название', 'Реквизит: ИНН', 'Рабочий телефон', 'Мобильный телефон',
           'Рабочий e-mail', 'Корпоративный сайт', 'Регион', 'Теги отдела снабжения']
_running = threading.Lock()


def read_all(onec, entity, select, condition='DeletionMark eq false', page_size=500):
    rows, seen = [], set()
    while True:
        params = {'$format': 'json', '$select': select, '$orderby': 'Ref_Key',
                  '$top': page_size, '$skip': len(rows)}
        if condition:
            params['$filter'] = condition
        batch = onec._odata(entity, params)
        if not isinstance(batch, list):
            raise ValueError('Некорректный ответ ' + entity)
        if not batch:
            return rows
        for row in batch:
            if row['Ref_Key'] in seen:
                raise ValueError('Повтор страницы ' + entity)
            seen.add(row['Ref_Key'])
        rows.extend(batch)


def item_categories(name, group='', full_name=''):
    found = classify({'item': name, 'group': group, 'full_name': full_name})
    return [category for category in CATEGORIES if category in found]


def contacts(source, kinds):
    result = {}
    for row in source.get('КонтактнаяИнформация') or []:
        kind = kinds.get(row.get('Вид_Key'), '').casefold()
        typ = row.get('Тип', '')
        if typ == 'Телефон':
            field = 'Мобильный телефон' if 'мобил' in kind else 'Номер факса' if 'факс' in kind else 'Рабочий телефон'
            value = row.get('Представление') or row.get('НомерТелефона')
        elif typ == 'АдресЭлектроннойПочты':
            field, value = 'Рабочий e-mail', row.get('АдресЭП') or row.get('Представление')
        elif typ in ('ВебСтраница', 'ВебСайт'):
            field, value = 'Корпоративный сайт', row.get('Представление')
        elif typ == 'Адрес':
            field, value = 'Регион', row.get('Регион')
        else:
            continue
        value = str(value or '').strip()
        if value:
            values = result.setdefault(field, [])
            if value not in values:
                values.append(value)
    return {key: '; '.join(values) for key, values in result.items()}


def build(partners, counterparties, tags, kinds, procurement_owners=None):
    procurement_owners = procurement_owners or set()
    by_partner = {}
    for row in counterparties:
        if not row.get('DeletionMark'):
            by_partner.setdefault(row.get('Партнер_Key'), []).append(row)
    result = []
    for partner in partners:
        key = partner['Ref_Key']
        if partner.get('DeletionMark') or not (partner.get('Поставщик') or partner.get('Перевозчик') or key in procurement_owners):
            continue
        # Each legal entity retains its own INN and contacts.
        for legal in by_partner.get(key) or [partner]:
            fields = {'Название': str(legal.get('Description') or partner.get('Description') or '').strip(),
                      'Тип компании': 'Поставщик', 'Реквизит: ИНН': str(legal.get('ИНН') or '').strip(),
                      'Теги отдела снабжения': '; '.join(tags.get(key, []))}
            fields.update(contacts(partner, kinds))
            fields.update(contacts(legal, kinds))
            result.append({'id': legal['Ref_Key'], 'fields': fields})
    return sorted(result, key=lambda row: (row['fields']['Название'].casefold(), row['id']))


def fetch(onec, log=lambda message: None):
    def read(entity, select, condition='DeletionMark eq false'):
        log('1С: база поставщиков — ' + entity)
        return read_all(onec, entity, select, condition)

    partners = read('Catalog_Партнеры', 'Ref_Key,Description,Поставщик,Перевозчик,КонтактнаяИнформация')
    legal = read('Catalog_Контрагенты', 'Ref_Key,Description,Партнер_Key,ИНН,КонтактнаяИнформация')
    kinds = {row['Ref_Key']: row['Description'] for row in
             read('Catalog_ВидыКонтактнойИнформации', 'Ref_Key,Description')}
    items = {row['Ref_Key']: row for row in
             read('Catalog_Номенклатура', 'Ref_Key,Description,НаименованиеПолное,Parent_Key,IsFolder')}
    owners = {row['Ref_Key']: row.get('Партнер_Key') for row in legal}
    partner_keys = {row['Ref_Key'] for row in partners}
    links = {}
    procurement_owners = set()

    def add(owner, item_key, procurement=False):
        owner = owner if owner in partner_keys else owners.get(owner)
        if owner and procurement:
            procurement_owners.add(owner)
        if owner and item_key in items:
            links.setdefault(owner, set()).add(item_key)

    for row in read('Catalog_НоменклатураКонтрагентов', 'Ref_Key,Owner_Key,Номенклатура_Key',
                    'DeletionMark eq false and IsFolder eq false'):
        add(row.get('Owner_Key'), row.get('Номенклатура_Key'))
    for entity in ('Document_ЗаказПоставщику', 'Document_ПриобретениеТоваровУслуг'):
        condition = 'DeletionMark eq false and Posted eq true'
        if entity.endswith('ПриобретениеТоваровУслуг'):
            condition += " and ХозяйственнаяОперация eq 'ЗакупкаУПоставщика'"
        for doc in read(entity, 'Ref_Key,Партнер_Key,Контрагент_Key,Товары', condition):
            for row in doc.get('Товары') or []:
                if not row.get('Отменено') and float(row.get('Количество') or 0) > 0:
                    add(doc.get('Партнер_Key'), row.get('Номенклатура_Key'), procurement=True)
    select = 'Ref_Key,' + ','.join(f'Поставщик{i}_Key' for i in range(1, 11)) + ',ТаблицаТовары'
    for doc in read('Document_ТТК_ЗаявкаВОтделСнабжения', select, 'DeletionMark eq false and Posted eq true'):
        for row in doc.get('ТаблицаТовары') or []:
            for index in range(1, 11):
                if float(row.get(f'ЦенаПоставки{index}') or 0) > 0:
                    add(doc.get(f'Поставщик{index}_Key'), row.get('Номенклатура_Key'), procurement=True)
    categories = {}
    for key in {item for values in links.values() for item in values}:
        item, group, seen = items[key], [], set()
        parent = item.get('Parent_Key')
        while parent in items and parent not in seen:
            seen.add(parent)
            group.append(items[parent].get('Description', ''))
            parent = items[parent].get('Parent_Key')
        categories[key] = item_categories(item['Description'], ' / '.join(reversed(group)),
                                          item.get('НаименованиеПолное', ''))
    tags = {owner: [category for category in CATEGORIES if any(category in categories[key] for key in keys)]
            for owner, keys in links.items()}
    return build(partners, legal, tags, kinds, procurement_owners)


def store(con, data, now=None):
    con.execute('DELETE FROM supplier_directory')
    con.executemany('INSERT INTO supplier_directory (id,name,inn,fields) VALUES (?,?,?,?)',
                    [(row['id'], row['fields'].get('Название', ''), row['fields'].get('Реквизит: ИНН', ''),
                      json.dumps(row['fields'], ensure_ascii=False)) for row in data])
    for key, value in [('directory_synced_at', now or dt.datetime.now().isoformat(timespec='seconds')),
                       ('directory_error', '')]:
        con.execute('INSERT OR REPLACE INTO supplier_meta VALUES (?,?)', (key, value))


def meta(con):
    data = dict(con.execute("SELECT key,value FROM supplier_meta WHERE key LIKE 'directory_%'"))
    return {'synced_at': data.get('directory_synced_at'), 'error': data.get('directory_error') or None}


def is_stale(con):
    synced = meta(con)['synced_at']
    attempt = con.execute("SELECT value FROM supplier_meta WHERE key='directory_attempt_at'").fetchone()
    if attempt and dt.datetime.fromisoformat(attempt[0]) > dt.datetime.now() - dt.timedelta(minutes=5):
        return False
    return not synced or dt.datetime.fromisoformat(synced) < dt.datetime.now() - dt.timedelta(hours=1)


def refreshing():
    return _running.locked()


def refresh(con, onec, log=lambda message: None):
    if not _running.acquire(blocking=False):
        return 'busy'
    try:
        with db_lock(), con:
            con.execute('INSERT OR REPLACE INTO supplier_meta VALUES (?,?)',
                        ('directory_attempt_at', dt.datetime.now().isoformat(timespec='seconds')))
        data = fetch(onec, log)
        with db_lock(), con:
            store(con, data)
        log(f'1С: база поставщиков — {len(data)} компаний')
        return 'ok'
    except Exception as exc:
        with db_lock(), con:
            con.execute('INSERT OR REPLACE INTO supplier_meta VALUES (?,?)',
                        ('directory_error', f'{type(exc).__name__}: {exc}'[:500]))
        log('Ошибка базы поставщиков из 1С: ' + type(exc).__name__)
        return 'error'
    finally:
        _running.release()


def tax_id(value):
    value = str(value or '').strip()
    return value if value.isascii() and value.isdigit() and len(value) in (10, 12) else ''


def supplier_key(row):
    inn = tax_id(row['fields'].get('Реквизит: ИНН'))
    return 'inn:' + inn if inn else 'ref:' + row['id']


def formatted_fields(fields):
    fields = dict(fields)
    fields['Название'] = company_name(fields.get('Название'))
    fields['Регион'] = region(fields.get('Регион')) or str(fields.get('Регион') or '').strip()
    return fields


def region_enrichment(con):
    cutoff = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(timespec='seconds')
    return {row['supplier_key']: dict(row) for row in con.execute(
        'SELECT * FROM supplier_region_enrichment WHERE checked_at>=?', (cutoff,))}


def rows(con, search=''):
    words = str(search or '').casefold().replace('ё', 'е').split()
    regions = region_enrichment(con)
    result = []
    for row in con.execute('SELECT id,fields FROM supplier_directory ORDER BY name COLLATE NOCASE,id'):
        fields = formatted_fields(json.loads(row['fields']))
        if not region(fields['Регион']):
            saved = regions.get(supplier_key({'id': row['id'], 'fields': fields}))
            if saved:
                fields['Регион'] = saved['region']
        text = ' '.join(str(value) for value in fields.values()).casefold().replace('ё', 'е')
        if all(word in text for word in words):
            result.append({'id': row['id'], 'fields': fields})
    return sorted(result, key=lambda row: (row['fields']['Название'].casefold(), row['id']))


def xlsx(data, audit=None):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook(write_only=True)
    sheet = book.create_sheet('Лист 1')
    for index, header in enumerate(HEADERS, 1):
        sheet.column_dimensions[get_column_letter(index)].width = 38 if header in DISPLAY else 20
    sheet.freeze_panes = 'D2'
    sheet.auto_filter.ref = f'A1:CC{len(data) + 1}'
    sheet.row_dimensions[1].height = 45
    alignment = Alignment(vertical='top', wrap_text=True)
    font = Font(color='FFFFFF', bold=True)
    fill = PatternFill('solid', fgColor='3F438D')

    def cell(value, heading=False):
        result = WriteOnlyCell(sheet, value=str(value))
        result.data_type = 's'  # Source text must never become an Excel formula.
        result.alignment = alignment
        if heading:
            result.font, result.fill = font, fill
        return result

    sheet.append([cell(header, heading=True) for header in HEADERS])
    for entry in data:
        fields = entry['fields']
        sheet.append([cell(fields[header]) if fields.get(header) else None for header in HEADERS])
    if audit:
        sheet = book.create_sheet('Источники регионов')
        sheet.freeze_panes = 'A2'
        for index, width in enumerate((38, 18, 38, 65, 25), 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.append([cell(header, heading=True) for header in ('Поставщик', 'ИНН', 'Регион', 'Источник', 'Проверено')])
        for values in audit:
            sheet.append([cell(value) for value in values])
        sheet.auto_filter.ref = f'A1:E{len(audit) + 1}'
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()
