"""Единое отображение названий поставщиков и кодов регионов ФНС."""
import json
from pathlib import Path
import re

# Коды ФНС — приложение 5 https://www.nalog.gov.ru/html/sites/www.new.nalog.ru/doc/2pril887_081220.pdf
# с дополнениями https://www.nalog.gov.ru/rn77/about_fts/docs/14138034/
REGIONS = json.loads(Path(__file__).with_name('supplier_regions.json').read_text('utf-8'))
LEGAL = {'ООО', 'АО', 'ПАО', 'НАО', 'ОАО', 'ЗАО', 'ИП', 'ГУП', 'МУП', 'ФГУП',
         'ФГБУ', 'ГБУ', 'МБУ', 'ФКУ', 'ГКУ', 'АНО', 'НКО', 'НП', 'ПТ', 'КФХ', 'СНТ'}
GROUPS = {'ГК', 'ТК', 'ТД', 'ТПК', 'ПКФ', 'ПТК', 'НПК', 'НПП', 'НПО', 'НТЦ', 'СК', 'ПФ', 'АКБ'}
FULL_FORMS = [
    (r'общество\s+с\s+ограниченной\s+ответственностью', 'ООО'),
    (r'публичное\s+акционерное\s+общество', 'ПАО'),
    (r'открытое\s+акционерное\s+общество', 'ОАО'),
    (r'закрытое\s+акционерное\s+общество', 'ЗАО'),
    (r'акционерное\s+общество', 'АО'),
    (r'индивидуальный\s+предприниматель', 'ИП'),
    (r'группа\s+компаний', 'ГК'),
]
ABBREVIATION = re.compile(r'(?<!\w)(' + '|'.join(sorted(LEGAL | GROUPS, key=len, reverse=True)) + r')(?!\w)', re.I)


def company_name(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    for pattern, abbreviation in FULL_FORMS:
        text = re.sub(r'\b' + pattern + r'\b', abbreviation, text, flags=re.I)
    found = list(dict.fromkeys(match.group().upper() for match in ABBREVIATION.finditer(text)))
    name = ABBREVIATION.sub('', text)
    name = re.sub(r'\(\s*\)|\[\s*\]', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' ,;-–—')
    for opening, closing in [('"', '"'), ("'", "'"), ('«', '»'), ('„', '“'), ('“', '”')]:
        if (name.startswith(opening) and name.endswith(closing) and len(name) > 1
                and name.count(opening) == (2 if opening == closing else 1)
                and name.count(closing) == (2 if opening == closing else 1)):
            name = name[1:-1].strip()
        elif name.startswith(opening) and name.count(opening) == 1 and (opening == closing or closing not in name):
            name = name[1:].strip()
    if not name:
        return text
    suffix = [word for word in found if word in GROUPS] + [word for word in found if word in LEGAL]
    return name + (', ' + ' '.join(suffix) if suffix else '')


def region_key(value):
    words = re.findall(r'[а-яa-z]+', str(value or '').lower().replace('ё', 'е'))
    types = {'республика', 'респ', 'область', 'обл', 'край', 'город', 'г', 'россия', 'российская', 'федерация', 'рф',
             'автономный', 'автономная', 'авт', 'округ', 'ао'}
    return ''.join(word for word in words if word not in types)


_region_codes = {region_key(name): code for code, name in REGIONS.items()}
_region_codes.update({region_key(alias): code for code, aliases in {
    '14': ['Саха', 'Якутия'], '15': ['Северная Осетия', 'Алания'],
    '13': ['Саранск'], '16': ['Татарстан', 'Татарстан (Татарстан)', 'Казань', 'Набережные Челны'],
    '21': ['Чувашия', 'Чувашская', 'Чувашская Республика - Чувашия'],
    '42': ['Кемеровская область', 'Кузбасс'], '54': ['Новосибирск'], '59': ['Пермь'],
    '66': ['Екатеринбург'], '86': ['Ханты-Мансийский округ', 'ХМАО', 'Югра'],
    '72': ['Тюмень'], '74': ['Челябинскаяобласть'],
    '78': ['С-Петербург', 'Санкт-Петербург Финляндский округ'], '89': ['ЯНАО'],
}.items() for alias in aliases})


def region(value):
    text = str(value or '').strip()
    if ';' in text:
        parts = list(dict.fromkeys(region(part) for part in text.split(';') if part.strip()))
        return '; '.join(parts) if parts and all(parts) else ''
    if re.fullmatch(r'\d{1,2}', text):
        text = text.zfill(2)
    if text in REGIONS:
        return text + '-' + REGIONS[text]
    code = _region_codes.get(region_key(text))
    return code + '-' + REGIONS[code] if code else ''


def region_matches_evidence(value, evidence):
    """В цитате должен однозначно присутствовать тот же регион."""
    canonical = region(value)
    if not canonical or ';' in canonical:
        return False
    words = set(re.findall(r'[а-яa-z]+', evidence.lower().replace('ё', 'е')))
    codes = set()
    for code, name in REGIONS.items():
        core = set(re.findall(r'[а-яa-z]+', name.lower().replace('ё', 'е'))) - {
            'республика', 'область', 'край', 'г', 'автономный', 'автономная', 'округ'}
        if core and core <= words:
            codes.add(code)
    for alias, code in {'пермь': '59', 'екатеринбург': '66', 'якутия': '14', 'татарстан': '16',
                        'кемеровская': '42', 'кузбасс': '42', 'югра': '86', 'чувашия': '21'}.items():
        if alias in words:
            codes.add(code)
    return codes == {canonical.split('-', 1)[0]}
