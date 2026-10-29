"""Панель «Метрики → Отдел продаж» по макету «Метрики ОП.xlsx» (листы «Продажи», «Реестр», «Действия»).

Объекты реестра подгружаются из 1С и Битрикса; срок этапа, флаги готовности расчёта, действия и вопросы ИД вводятся вручную
(в системах их нет). Из систем берутся: долг и срок оплаты (дебиторка 1С), приём заказа снабжением (заявка снабжения),
подписанная реализация и ссылка на карточку Битрикса — пока поле не заполнено вручную. Формулы листов перенесены 1:1.
"""
import datetime as dt
import re

# этап → (результат этапа, группа, где фиксировать) — справочник внизу листа «Реестр»
STAGES = [
    ("Лиды", "Контакт и подтверждённая потребность", "Лиды", "Битрикс: карточка контакта, ЛПР, потребность и следующий шаг"),
    ("Запрос", "Ведомость требований клиента", "Запрос / проверка", "ТЗ, объёмы, срок, адрес, покрытие, монтаж, документы."),
    ("Проверка закупки", "Лист оценки закупки", "Запрос / проверка", "Соответствие ТЗ, сроки, обеспечение и риски участия."),
    ("Расчёт", "Согласованная калькуляция", "Расчёт / решение", "Закупка, доставка, дополнительные расходы, маржа, цена продажи."),
    ("Решение об участии", "Согласованная цена и решение участвовать", "Расчёт / решение",
     "Калькуляция, обеспечение, комиссии, риски, решение ответственного."),
    ("КП", "Ответ клиента на предложение", "КП / подготовка", "Версия КП, факт отправки, обратная связь и препятствие к закупке."),
    ("Подготовка заявки", "Полный комплект тендерной заявки", "КП / подготовка", "Техническая часть, цена, допуски, БГ/обеспечение по условиям."),
    ("Переговоры", "Согласованные условия и открытые вопросы", "Переговоры / подача",
     "Возражения, решения, лица согласования, сроки следующего решения."),
    ("Подача", "Подтверждение подачи площадкой", "Переговоры / подача", "Квитанция/статус площадки и фактическое время подачи."),
    ("Дозапрос", "Принятый площадкой ответ", "Переговоры / подача", "Комплект уточнений и подтверждение приёма в установленный срок."),
    ("Результат", "Протокол результата закупки", "Договор / результат", "Результат, причины отказа или требования для заключения договора."),
    ("Договор", "Подписанные договор и спецификация", "Договор / результат",
     "Состав, цена, сроки, оплата; неподписанный проект не подтверждает этап."),
    ("Передача в исполнение", "Заказ принят снабжением и логистикой", "Передача / исполнение",
     "Комплект передан и принят: объёмы, сроки, бюджет, адрес, условия."),
    ("Исполнение", "Согласованы изменения и сроки клиента", "Передача / исполнение",
     "Действия продавца по клиенту; не заменяют производственный план."),
    ("Приёмка и оплата", "Приёмка подтверждена и оплата поступила", "Приёмка / оплата",
     "1С: остаток долга и срок; Битрикс/ЭДО: согласование и приёмка."),
    ("Закрыто успешно", "Закрыто", "Закрыто", "Закрытые объекты исключены из текущей очереди."),
    ("Закрыто без результата", "Закрыто", "Закрыто", "Причина закрытия фиксируется в Битриксе."),
]
STAGE = {s[0]: s for s in STAGES}
GROUPS = ["Лиды", "Запрос / проверка", "Расчёт / решение", "КП / подготовка", "Переговоры / подача", "Договор / результат",
          "Передача / исполнение", "Приёмка / оплата"]
TYPES = ["Лид", "Сделка", "Исполнение"]
CHANNELS = ["Прямые", "Тендеры"]
FLAG_OPTS = ["Готово", "Нет", "Не требуется"]
READY_OPTS = ["Готово", "Нет"]
ACCEPT_OPTS = ["Принято", "Не принято", "Не требуется"]
# пять флагов готовности расчёта (столбцы I:M реестра): поле, название в «Что ещё нужно», название в «Что нужно для расчёта»
FLAGS = [("f_req", "ТЗ", "ТЗ и объёмы"), ("f_plant", "КП завода", "Предложение завода"),
         ("f_log", "логистика", "Расчёт логистики"), ("f_extra", "допрасходы", "Дополнительные расходы"),
         ("f_price", "цена продажи", "Согласованная цена")]
DEPTS = ["Продажи", "Снабжение", "Логистика", "Юрист", "Финансы", "Клиент"]
KINDS = ["Контакт", "Внутренняя задача", "Требования", "Расчёт", "Согласование", "Подача", "Дозапрос", "Обеспечение",
         "Договор", "Передача заказа", "Обязательство клиенту", "Документы", "Оплата"]
CRITICAL_KINDS = {"Подача", "Дозапрос", "Обеспечение", "Обязательство клиенту"}
STATUSES = ["Открыто", "Ожидание", "Закрыто"]
YN = ["Да", "Нет"]
HARD_HOURS = 48
OBJ_FIELDS = ["id", "type", "client", "emp", "channel", "stage", "entered", "due", "f_req", "f_plant", "f_log", "f_extra",
              "f_price", "ready", "proof", "handover", "docs", "order_1c", "debt", "pay_due", "url"]
ACT_FIELDS = ["id", "obj_id", "what", "dept", "executor", "due", "kind", "status", "needs_id", "question", "link"]
FAR = "9999-12-31T00:00"


def _dt(v):
    try:
        return dt.datetime.fromisoformat(str(v)[:16]) if v else None
    except ValueError:
        return None


def _clean(d, fields, choices, dates, money=()):
    out = {}
    for f in fields:
        v = d.get(f)
        v = v.strip() if isinstance(v, str) else v
        if v in ("", None):
            out[f] = None
            continue
        if f in choices and v not in choices[f]:
            raise ValueError(f"«{v}» — недопустимое значение поля {f}")
        if f in dates and _dt(v) is None:
            raise ValueError(f"«{v}» — не дата")
        if f in money:
            try:
                v = float(str(v).replace(",", ".").replace(" ", ""))
            except ValueError:
                raise ValueError("сумма должна быть числом")
        out[f] = v
    return out


def save_object(con, d, team, login):
    choices = {"type": TYPES, "channel": CHANNELS, "stage": [s[0] for s in STAGES], "emp": list(team),
               **{f: FLAG_OPTS for f, _, _ in FLAGS}, "ready": READY_OPTS, "handover": ACCEPT_OPTS, "docs": ACCEPT_OPTS}
    o = _clean(d, OBJ_FIELDS, choices, {"entered", "due", "pay_due"}, {"debt"})
    if not o["id"]:
        raise ValueError("укажите ID объекта")
    old = con.execute("SELECT stage, source FROM sales_objects WHERE id = ?", (o["id"],)).fetchone()
    if not o["type"] or (not o["stage"] and not (old and old["source"])):
        raise ValueError("укажите тип и этап")  # без этапа можно только объекты из 1С — их этап задаёт руководитель позже
    keep = [f for f in OBJ_FIELDS if f != "id"]
    con.execute(f"INSERT INTO sales_objects ({','.join(OBJ_FIELDS)},updated_by,updated_at,stage_auto) "
                f"VALUES ({','.join('?' * (len(OBJ_FIELDS) + 3))}) ON CONFLICT(id) DO UPDATE SET "
                + ",".join(f"{f}=excluded.{f}" for f in keep) +
                ",updated_by=excluded.updated_by,updated_at=excluded.updated_at,"
                "stage_auto=CASE WHEN COALESCE(sales_objects.stage,'')<>COALESCE(excluded.stage,'') THEN 0 ELSE sales_objects.stage_auto END",
                [o[f] for f in OBJ_FIELDS] + [login, dt.datetime.now().isoformat(timespec="seconds"), 0])


def delete_object(con, oid):
    con.execute("DELETE FROM sales_objects WHERE id = ?", (oid,))


def save_action(con, d, login):
    choices = {"dept": DEPTS, "kind": KINDS, "status": STATUSES, "needs_id": YN}
    a = _clean(d, ACT_FIELDS, choices, {"due"})
    if not a["id"]:
        raise ValueError("укажите ID действия")
    if not a["obj_id"]:
        raise ValueError("укажите ID объекта")
    row = con.execute("SELECT ord FROM sales_actions WHERE id = ?", (a["id"],)).fetchone()
    ord_ = row[0] if row else con.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM sales_actions").fetchone()[0]
    con.execute(f"INSERT OR REPLACE INTO sales_actions ({','.join(ACT_FIELDS)},ord,updated_by,updated_at) "
                f"VALUES ({','.join('?' * (len(ACT_FIELDS) + 3))})",
                [a[f] for f in ACT_FIELDS] + [ord_, login, dt.datetime.now().isoformat(timespec="seconds")])


def delete_action(con, aid):
    con.execute("DELETE FROM sales_actions WHERE id = ?", (aid,))


def _need(o):
    """«Что ещё нужно» (столбец W реестра)."""
    st = o["stage"]
    if STAGE[st][1] == "Закрыто":
        return ""
    flags = [o[f] for f, _, _ in FLAGS]
    proved = o["ready"] == "Готово" and o["proof"]
    if st in ("Расчёт", "Решение об участии"):
        if sum(v in ("Готово", "Не требуется") for v in flags) < 5:
            return "".join(f"{title}; " for (f, title, _), v in zip(FLAGS, flags) if v in (None, "Нет"))
        if sum(v == "Не требуется" for v in flags) == 5:
            return "Проверить применимость расчёта"
        return "" if proved else "Подтверждение калькуляции"
    if st == "Передача в исполнение":
        return "" if o["handover"] == "Принято" and o["proof"] else "Приём заказа смежниками"
    if st == "Приёмка и оплата":
        return ("Подтверждение приёмки; " if o["docs"] != "Принято" else "") + \
            ("Остаток долга не загружен" if o["debt"] is None else "Оплата клиента" if o["debt"] > 0 else "")
    return "" if proved else STAGE[st][1]


def _debt_1c(con, order):
    """Остаток долга и ближайший срок оплаты по связанному заказу из 1С (дебиторка); None — заказ не указан.
    Заказа нет в дебиторке — отгруженного долга нет (не отгружен или нет этапов оплаты): остаток 0."""
    if not order:
        return None
    rows = con.execute("SELECT amount, due_date, paid_date FROM receivables WHERE doc = ?", (order.strip(),)).fetchall()
    if not rows:
        return 0.0, None
    open_ = [r for r in rows if not r[2]]
    return sum(r[0] or 0 for r in open_), min((r[1] for r in open_ if r[1]), default=None)


def _others(sel, sa, team, emp):
    """Строка очереди «Остальные»: объекты и действия сотрудников вне отдела ОП (например, КП исполнительного директора)."""
    if emp != "Все":
        return []
    os_ = [o for o in sel if o["emp"] not in team]
    row = {"login": "", "name": "Вне отдела ОП / без сотрудника",
           **{k: sum(1 for o in os_ if o["type"] == v) for k, v in (("leads", "Лид"), ("deals", "Сделка"), ("exec", "Исполнение"))},
           "late": sum(a["sales_late"] and a["manager"] not in team for a in sa),
           "waiting": sum(a["waiting"] and a["manager"] not in team for a in sa)}
    return [row] if os_ or row["late"] or row["waiting"] else []


def _autofill(con, o, handed, signed, portal):
    """Пустые поля из систем: ссылка на карточку Битрикса; для заказа 1С — приём снабжением (есть отработанная заявка снабжения)
    и подписанная реализация. Ручной ввод руководителя не перезаписывается; список заполненных полей — в o["auto"]."""
    o["auto"] = []
    m = re.fullmatch(r"Б24-([СЛ])-(.+)", o["id"] or "")
    if m and portal and not o["url"]:
        o["url"] = f"https://{portal}/crm/{'deal' if m.group(1) == 'С' else 'lead'}/details/{m.group(2)}/"
        o["auto"].append("url")
    order = (o["order_1c"] or "").strip()
    if order and order in handed and not o["handover"]:
        o["handover"] = "Принято"
        o["auto"].append("handover")
        if not o["proof"]:
            o["proof"] = "Заявка снабжения " + handed[order]
            o["auto"].append("proof")
    if order and not o["docs"] and any(order in d for d in signed):
        o["docs"] = "Принято"
        o["auto"].append("docs")


def build(con, team, now=None, channel="Все", emp="Все", portal=None):
    """team: {login: имя} сотрудников ОП. Возвращает панель, реестр и действия с расчётными полями."""
    now = now or dt.datetime.now()
    today = now.date()
    objs = {}
    handed = {(r[0] or "").strip(): r[1] for r in con.execute("SELECT customer_order, doc FROM supply_funnel WHERE stage='ordered' AND customer_order<>''")}
    signed = [r[0] or "" for r in con.execute("SELECT doc FROM sales WHERE signed=1")]
    for r in con.execute("SELECT * FROM sales_objects ORDER BY rowid"):
        o = dict(r)
        _autofill(con, o, handed, signed, portal)
        d1c = _debt_1c(con, o["order_1c"])
        o["debt_1c"] = d1c is not None
        if d1c:
            o["debt"], o["pay_due"] = d1c[0], d1c[1] or o["pay_due"]
        known = o["stage"] in STAGE
        o["closed"] = known and STAGE[o["stage"]][1] == "Закрыто"
        o["result"] = STAGE[o["stage"]][1] if known else "Не задан этап"
        o["no_stage"] = not known
        o["group"] = STAGE[o["stage"]][2] if known else "Не классифицирован"
        o["need"] = _need(o) if known else ""
        due = _dt(o["due"])
        o["late"] = bool(due and due < now and o["need"] and not o["closed"])
        o["no_due"] = not o["closed"] and not o["due"]
        o["pre_exec"] = not known or [x[0] for x in STAGES].index(o["stage"]) < [x[0] for x in STAGES].index("Передача в исполнение")
        # долг до передачи в исполнение не возникает; нулевой остаток — просрочки нет; иначе нужны остаток и срок оплаты
        o["debt_late"] = 0 if o["debt"] is not None and o["debt"] <= 0 else None if o["debt"] is None or not o["pay_due"] else \
            (o["debt"] if o["pay_due"][:10] < today.isoformat() else 0)
        o["selected"] = (not o["closed"] and (emp == "Все" or o["emp"] == emp) and (channel == "Все" or o["channel"] == channel))
        objs[o["id"]] = o
    acts = []
    for r in con.execute("SELECT * FROM sales_actions ORDER BY ord"):
        a = dict(r)
        o = objs.get(a["obj_id"])
        a["manager"], a["channel"] = (o["emp"], o["channel"]) if o else ("Нет связи", None)
        a["active"] = bool(o and a["status"] != "Закрыто" and not o["closed"])
        due = _dt(a["due"])
        a["late_hours"] = 0 if not a["active"] else None if not due else max(0, int((now - due).total_seconds() // 3600))
        a["critical"] = bool(a["active"] and due and due <= now + dt.timedelta(hours=HARD_HOURS) and a["kind"] in CRITICAL_KINDS)
        a["sales_late"] = bool(a["active"] and a["dept"] == "Продажи" and a["late_hours"])
        a["waiting"] = bool(a["active"] and a["status"] == "Ожидание" and a["dept"] != "Продажи")
        a["selected"] = bool(a["active"] and (emp == "Все" or a["manager"] == emp) and (channel == "Все" or a["channel"] == channel))
        a["prio"] = 0 if not a["selected"] else 1 if a["critical"] else 2 if a["late_hours"] else \
            3 if due and due.date() == today else 4 if (a["needs_id"] == "Да" or not due) else 0
        a["sort"] = a["due"] if a["active"] and a["due"] else FAR
        acts.append(a)

    sel = [o for o in objs.values() if o["selected"]]
    sa = [a for a in acts if a["selected"]]
    ranked = sorted((a for a in sa if a["prio"]), key=lambda a: (a["prio"], a["sort"]))
    ask = sorted((a for a in sa if a["needs_id"] == "Да"), key=lambda a: a["sort"])
    with_action = {a["obj_id"] for a in acts if a["active"] and a["dept"] == "Продажи"}
    tag = lambda a: "Жёсткий срок" if a["critical"] else "Просрочка продаж" if a["sales_late"] else \
        "Просрочка ожидания" if (a["late_hours"] or 0) > 0 else "Решение ИД" if a["needs_id"] == "Да" else "Сегодня"
    calc = [o for o in sel if o["group"] == "Расчёт / решение"]
    panel = {
        "now": now.isoformat(timespec="minutes"), "channel": channel, "emp": emp,
        "tiles": {"today": sum(1 for a in sa if (d := _dt(a["due"])) and d.date() == today),
                  "hard": sum(a["critical"] for a in sa), "sales_late": sum(a["sales_late"] for a in sa),
                  "waiting": sum(a["waiting"] for a in sa)},
        "objects": len(sel), "no_action": sum(o["id"] not in with_action for o in sel), "no_due": sum(o["no_due"] for o in sel),
        "no_stage": sum(o["no_stage"] for o in sel), "critical": [{"obj": a["obj_id"], "what": a["what"] or "Нет данных", "who": a["executor"] or "Нет данных",
                      "due": a["due"], "late": (a["late_hours"] or 0) > 0, "tag": tag(a)} for a in ranked[:6]],
        "groups": [{"group": g, "total": sum(o["group"] == g for o in sel), "late": sum(o["group"] == g and o["late"] for o in sel)}
                   for g in GROUPS],
        "need": [{"title": t, "count": sum(o[f] in (None, "Нет") for o in calc)} for f, _, t in FLAGS],
        "queue": [{"login": lg, "name": n,
                   **{k: sum(1 for o in sel if o["emp"] == lg and o["type"] == v)
                      for k, v in (("leads", "Лид"), ("deals", "Сделка"), ("exec", "Исполнение"))},
                   "late": sum(a["sales_late"] and a["manager"] == lg for a in sa),
                   "waiting": sum(a["waiting"] and a["manager"] == lg for a in sa)}
                  for lg, n in team.items() if emp in ("Все", lg)] + _others(sel, sa, team, emp),
        "ask_count": len(ask),
        "ask": [{"obj": a["obj_id"], "question": a["question"] or "Сформулировать вопрос для ИД", "due": a["due"],
                 "late": (a["late_hours"] or 0) > 0} for a in ask[:4]],
        "control": {"no_link": sum(a["manager"] == "Нет связи" for a in acts),
                    "no_due": sum(a["selected"] and a["late_hours"] is None for a in acts)},
        "drill": {g: [{"id": o["id"], "client": o["client"], "emp": team.get(o["emp"], o["emp"]), "stage": o["stage"],
                       "need": o["need"], "due": o["due"], "late": o["late"]} for o in sel if o["group"] == g] for g in GROUPS}}
    return {"panel": panel, "objects": list(objs.values()), "actions": acts, "team": team,
            "options": {"types": TYPES, "channels": CHANNELS, "stages": [s[0] for s in STAGES], "flags": FLAG_OPTS,
                        "ready": READY_OPTS, "accept": ACCEPT_OPTS, "depts": DEPTS, "kinds": KINDS, "statuses": STATUSES,
                        "yn": YN},
            "flags": [[f, t] for f, t, _ in FLAGS], "stages": [list(s) for s in STAGES]}
