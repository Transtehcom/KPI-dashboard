"""Dashboard section visibility for non-administrator employees."""
import json


SECTIONS = {'kpi':'Мой KPI', 'funnel':'Воронки', 'calls':'Звонки', 'gpt':'ТТКGPT',
            'workload':'Загрузка', 'metrics':'Метрики', 'money':'Деньги', 'suppliers':'Поставщики',
            'carriers':'Перевозчики', 'staff':'Сотрудники'}
DEFAULT = ('kpi', 'funnel', 'calls', 'gpt', 'staff')
LOGISTICS_ALIASES = ('ОЛ', 'Отдел логистики', 'Отдел логистики (ОЛ)')


def department_key(value):
    return 'ОЛ' if value in LOGISTICS_ALIASES else value or ''

ENDPOINTS = {path: section for section, paths in {
    'kpi': ('employee', 'employee-trend', 'kpi-records'),
    'funnel': ('funnel', 'call-funnel', 'calendar'),
    'calls': ('calls', 'call-audio', 'call-transcript', 'call-feedback'),
    'metrics': ('metrics', 'operations'),
    'workload': ('workload',), 'money': ('money',),
    'suppliers': ('suppliers', 'supplier-directory', 'supplier-directory.xlsx'),
    'carriers': ('carriers', 'carriers.xlsx'), 'staff': ('staff',)
}.items() for path in paths}


def section_for(path):
    endpoint = path.removeprefix('/api/').split('/')[0]
    return 'gpt' if endpoint.startswith('gpt-') else ENDPOINTS.get(endpoint)


def visible(con, department):
    key = department_key(department)
    for candidate in LOGISTICS_ALIASES if key == 'ОЛ' else (key,):
        row = con.execute('SELECT sections FROM department_section_access WHERE department=?', (candidate,)).fetchone()
        if row:
            return json.loads(row[0])
    return list(DEFAULT)


def save(con, department, sections, departments):
    department = department_key(department)
    if department not in departments or not isinstance(sections, list) or not all(
            isinstance(key, str) and key in SECTIONS for key in sections):
        raise ValueError('Неизвестный отдел или раздел.')
    con.execute('INSERT OR REPLACE INTO department_section_access VALUES (?,?)',
                (department, json.dumps([key for key in SECTIONS if key in sections])))
    if department == 'ОЛ':
        con.executemany('DELETE FROM department_section_access WHERE department=?', [(name,) for name in LOGISTICS_ALIASES[1:]])
    con.commit()
