"""Read-only, per-record explanations using the same selectors as the KPI engine."""
import datetime as dt

from .crm_activities import activity_datetime
from .crm_links import reference_url, record_url, client_url, activity_url


def _now():
    return dt.datetime.now()


def _check(key, label, passed, detail, action=None, required=True):
    state = "na" if not required else "unknown" if passed is None else "pass" if passed else "fail"
    return {"key": key, "label": label, "state": state, "detail": detail,
            "action": action if state in ("fail", "unknown") else None}


def _record(id, when, title, client, source, checks, counted, duplicate=False, url=None, note=None, client_link=None):
    actions = list(dict.fromkeys(c["action"] for c in checks if c.get("action")))
    return {"id": id, "when": when, "title": title or "Без названия", "client": client or "Клиент не указан",
            "source": source, "checks": checks, "counted": counted, "duplicate": duplicate,
            "actions": actions, "url": url, "client_url": client_link, "note": note}


def _crm_url(engine, contact):
    return reference_url(engine.config, contact)


def _calls(engine, employee, ctx, item):
    p, code = item.get("params", {}), item["code"]
    plain = code == "base_development" and p.get("calls_plain", False)
    require_lpr = p.get("require_lpr", code == "base_development") and not plain
    unique = code == "base_development" and p.get("one_call_per_contact_day", False)
    result, _, simple = engine._result_calls(employee["login"], ctx, p.get("require_lpr", code == "base_development"), unique)
    selected = {r["id"] for r in (simple if plain else result)}
    first, last = ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59"
    rows = engine.q("SELECT * FROM activities WHERE emp=? AND kind='call' AND dt>=? AND dt<=? ORDER BY dt,id",
                    employee["login"], first, last)
    engine._mark_secretary(rows)
    engine.mark_call_next_steps(employee["login"], rows)
    tender_steps = engine.tender_followups_ready(employee["login"])
    recordings = {r["id"]: r for r in engine.q(
        "SELECT id,phone,crm_activity_id,client_ref FROM call_records WHERE emp=? AND dt>=? AND dt<=?", employee["login"], first, last)}
    phones = {key: row["phone"] for key,row in recordings.items()}
    minimum = engine.min_call
    duration_label = f"От {minimum // 60} мин" if minimum % 60 == 0 else f"От {minimum} с"
    company_lpr = bool(engine.config.get("bitrix24", {}).get("fields", {}).get("company_lpr"))
    secretary_since = engine.rules.get("secretary_by_call_analysis_from")
    secretary_extended = bool(secretary_since and ctx["month"] >= secretary_since)
    secretary_rule = ("Собеседник не должен быть секретарём. Используется отметка руководителя, затем данные CRM и разбора звонка; источник указан у записи."
                      if secretary_extended else "В этом месяце признак секретаря определяется по должности контакта в CRM. Отметки руководителя и автоматического разбора к этому историческому месяцу не применяются.")
    secretary_action = ("Если говорили только с секретарём, выйдите на нужного сотрудника. При ошибочной классификации попросите руководителя проверить отметку собеседника в разделе «Звонки»."
                        if secretary_extended else "Если разговор был с секретарём, выйдите на нужного сотрудника. Для этого исторического месяца проверьте корректность должности контакта в CRM и обновите данные.")
    lpr_action = ("Уточните ЛПР и заполните подтверждённые сведения в карточке компании, к которой относится звонок, контакт или лид. В этом KPI используется поле ЛПР компании; после исправления обновите данные."
                  if company_lpr else "Уточните, кто принимает решение, внесите подтверждённые сведения об ЛПР в CRM клиента/контакта и обновите данные. Если собеседник не ЛПР, организуйте контакт с ЛПР.")
    rules = [f"Засчитывается входящий или исходящий звонок продолжительностью не менее {minimum} секунд.",
             secretary_rule]
    rules += (["Для этого KPI нужна информация об ЛПР: подтверждённые сведения о лице, принимающем решение, в CRM клиента/контакта по действующей настройке."]
              if require_lpr else ["Информация об ЛПР для этого показателя не обязательна."])
    if require_lpr and company_lpr:
        rules.append("Сведения об ЛПР берутся именно из карточки компании, в том числе для звонка её контакту или лиду. Заполните поле ЛПР компании подтверждёнными данными.")
    if not plain and tender_steps:
        rules.extend([
            "Следующий шаг — назначенное дело компании, связанной со звонком, созданное от момента звонка до истечения 24 часов включительно. Универсальное дело «Связаться с клиентом» подходит. Телефонные записи исключаются.",
            "Зачёт дают выполненное дело или открытое дело с указанным непросроченным сроком. Выполненное дело учитывается и после его срока. При числе переносов больше пяти дело исключается из зачёта.",
            "Из нескольких дел достаточно одного подходящего. Один звонок учитывается один раз. В расшифровке выполненное подходящее дело имеет приоритет над открытым.",
            "Счётчик переносов начинается с первого загруженного среза и растёт при замеченном изменении срока. Несколько изменений между обновлениями различить невозможно. Администратор может указать точное число вручную. Последующие замеченные переносы прибавляются к этой корректировке."])
    elif not plain:
        rules.append("Следующий шаг: открытое дело, кроме повторного звонка, в карточке компании, связанной со звонком. Срок дела должен быть указан и не просрочен на текущий момент. Дата создания и ответственный не ограничены; закрытые дела не учитываются.")
    else:
        rules.append("В этой версии показателя следующий шаг и ЛПР не обязательны: учитываются звонки нужной длительности не секретарю.")
    if unique:
        rules.append("Не более одного подходящего звонка в день одному CRM-контакту/карточке (без карточки — номеру телефона). Сначала проверяются все условия, затем исключаются повторы; неудачный ранний звонок не блокирует более поздний подходящий.")
    records = []
    for r in rows:
        seconds = r["duration"] or 0
        step_action = "Создайте в карточке связанной компании открытое дело с непросроченным сроком, кроме повторного звонка, либо актуализируйте срок существующего дела. Затем обновите данные."
        step_detail = "Открытое непросроченное дело в связанной компании найдено." if r["next_step"] else "Открытое непросроченное дело в связанной компании не найдено."
        if tender_steps:
            step_action = "Назначьте дело связанной компании в течение 24 часов после звонка и выполните его или сохраните актуальный срок. Проверьте число переносов. Для этого звонка дело, созданное позже окна, зачёт не даст."
            step_detail = {"completed": "Назначено выполнено.", "open": "Назначено дело не выполнено, срок актуален.",
                           "overdue": "Назначено просрочено или число переносов больше пяти.",
                           "unknown": "Для этого звонка требуется обновление данных дел.",
                           "missing": "Подходящего назначенного дела в течение 24 часов после звонка нет."}.get(r.get("followup_status"), "")
        row_lpr_action = lpr_action
        if str(r.get("contact") or "").startswith("DEAL:") and not r["is_lpr"]:
            row_lpr_action = "Звонок привязан к сделке: текущий источник не определяет ЛПР по этой привязке. Для этого звонка попросите проверить CRM-привязку; для следующих звонков используйте связанную карточку контакта, компании или лида с подтверждёнными сведениями об ЛПР."
        checks = [
            _check("outgoing", "Входящий или исходящий", r["direction"] in ("out", "in"), "Направление: " + ("исходящий" if r["direction"] == "out" else "входящий/другое"),
                   "Для этого показателя нужен входящий или исходящий разговор с клиентом."),
            _check("duration", duration_label, seconds >= minimum, f"Длительность {seconds} с; порог {minimum} с.",
                   "Этот разговор короче нормы. Следующий содержательный разговор должен соответствовать порогу; длительность берётся из телефонии."),
            _check("not_secretary", "Не секретарь", not r["secretary"], "Источник признака: " + (r.get("secretary_src") or "данные CRM/телефонии"),
                   secretary_action),
            _check("lpr", "Есть информация об ЛПР", bool(r["is_lpr"]), "Информация об ЛПР подтверждена в выгрузке." if r["is_lpr"] else "Подтверждения ЛПР в выгрузке нет.",
                   row_lpr_action, require_lpr),
            _check("next_step", "Зафиксирован следующий шаг", None if r["next_step"] is None else bool(r["next_step"]), step_detail, step_action, not plain),
        ]
        eligible = all(c["state"] in ("pass", "na") for c in checks)
        duplicate = bool(unique and eligible and r["id"] not in selected)
        if unique:
            checks.append(_check("unique", "Один контакт в день", not duplicate if eligible else None,
                "Другой подходящий звонок этой карточке уже учтён за день." if duplicate else "Повторы исключаются после проверки основных условий.",
                "Дополнительный звонок этому же контакту за тот же день не увеличит факт. Для дальнейшей работы используйте следующий день или другие контакты.", required=eligible))
        records.append(_record(r["id"], r["dt"], (r.get("subject") or "Звонок") + f" · {int(seconds)//60}:{int(seconds)%60:02d}",
            r.get("client") or phones.get(r["id"]) or r.get("contact"), "Битрикс24", checks,
            r["id"] in selected, duplicate, record_url(engine.config,"activities",{**r,
                "crm_activity_id": recordings.get(r["id"],{}).get("crm_activity_id") or r.get("crm_activity_id")}),
            client_link=client_url(engine.config,r) or client_url(engine.config,recordings.get(r["id"],{}))))
        if tender_steps:
            records[-1]["followups"] = [{**a, "url": activity_url(engine.config, a["id"], "COMPANY:" + str(r["company_id"]))}
                                        for a in r.get("followups", [])]
            if any(c["state"] == "unknown" for c in checks):
                records[-1]["counted"] = None
    return records, rules, 0


def _meetings(engine, employee, ctx, item):
    p = item.get("params", {})
    protocol = item["code"] == "operational_activity"
    same_day = protocol and p.get("meeting_same_day_close", False)
    selected, _ = engine._meetings(employee["login"], ctx, need_protocol=protocol, same_day_close=same_day)
    selected = {r["id"] for r in selected}
    rows = engine.q("SELECT * FROM activities WHERE emp=? AND kind='meeting' AND dt>=? AND dt<=? ORDER BY dt,id",
                    employee["login"], ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
    rules = ["Встреча должна быть проведена и завершена в CRM. В период она попадает по дате проведения, а не дате создания дела.",
             "Используйте тип «Встреча» либо дело с темой о проведении встречи/ВКС/переговоров. Напоминание «назначить встречу» встречей не считается. Отметьте фактическое выполнение после проведения."]
    if protocol:
        rules.append("Для этого KPI прикрепите протокол/итоговый файл и внесите резюме встречи в описание. Наличие протокола сейчас определяется по прикреплённому файлу, резюме — по описанию не короче 20 символов.")
    else:
        rules.append("Для этой части KPI наличие протокола и резюме не является обязательным условием; нужна завершённая встреча. Требование ЛПР для звонков не переносится на эту часть автоматически.")
    if same_day:
        rules.append("Закройте дело в день встречи. Доступная дата закрытия берётся по последнему изменению дела, поэтому позднее редактирование может выглядеть как позднее закрытие. Отсутствующая дата сейчас не исключает завершённую встречу — это показано как неопределённость.")
    records = []
    for r in rows:
        close = r.get("closed_dt")
        checks = [
            _check("completed", "Встреча завершена", r["status"] == "done", "Состояние: " + str(r["status"] or "не указано"),
                   "Проведите встречу, затем отметьте выполнение дела в CRM. Запланированная встреча не считается проведённой."),
            _check("protocol", "Есть протокол", bool(r["has_protocol"]), "Прикреплённый файл найден." if r["has_protocol"] else "Прикреплённый файл не найден.",
                   "Прикрепите протокол или итоговый файл к делу встречи и обновите данные.", protocol),
            _check("summary", "Есть резюме", bool(r["has_summary"]), "Описание содержит не менее 20 символов." if r["has_summary"] else "Описание отсутствует или короче 20 символов.",
                   "Добавьте в описание дела содержательное резюме: что обсудили, что согласовали и дальнейшие действия (не менее 20 символов).", protocol),
        ]
        if same_day:
            checks.append(_check("same_day_close", "Нет позднего закрытия", close[:10] <= r["dt"][:10] if close else None,
                f"Последнее изменение/доступный признак закрытия: {close}." if close else "Дата закрытия не пришла. Действующее правило не исключает запись, но подтвердить день закрытия нельзя.",
                "Проверьте дату выполнения в CRM. Позднее редактирование дела влияет на доступный признак закрытия; спорную дату нужно проверить с руководителем, не изменяя историю фиктивно."))
        records.append(_record(r["id"], r["dt"], r.get("subject") or "Встреча / ВКС", r.get("client"), "Битрикс24",
                               checks, r["id"] in selected, url=record_url(engine.config,"activities",r),
                               client_link=client_url(engine.config,r)))
    return records, rules, 0


def _kp(engine, employee, ctx, item, fact):
    p = item.get("params", {})
    kps, letters, pairs = engine._kp_inputs(employee["login"], ctx)
    since = engine.rules.get("kp_with_letters_from")
    union = bool(since and ctx["month"] >= since)
    tender = item["code"] == "base_development"
    journal = bool(engine.config.get("letters_sheet", {}).get("url"))
    confirmation = p.get("kp_confirmation", "b24")
    aggregate = not union and not tender and confirmation == "b24"
    pairs_by_letter = {str(l["id"]): kid for kid, l in pairs.items()}
    unallocated = int(fact or 0) if aggregate and 0 < len(letters) < len(kps) else 0
    rules = ["Показаны выгруженные КП и исходящие письма сотрудника за месяц. Дата и ответственный в исходной системе должны соответствовать фактической работе."]
    if union:
        days = engine.rules.get("kp_letter_match_days", 7)
        rules += ["Действующее правило: КП в 1С ИЛИ подходящее исходящее письмо. Не требуется наличие обоих источников одновременно; отдельная отметка о скане или «вне тендера» в этой версии отбора не добавляется.",
                  f"КП и письмо одной компании с разницей дат не более {days} дней учитываются как один результат. Компания сравнивается без организационной формы и знаков; совпавшее письмо отдельно не увеличивает факт."]
    elif aggregate:
        rules.append("Историческое правило: минимум из числа КП в 1С и числа подходящих исходящих писем. В данных нет связи каждого письма с конкретным КП: при частичном подтверждении нельзя определить, какие именно КП вошли в итог. Распределение по отдельным записям не выдумывается.")
    elif tender:
        rules.append("В этой версии учитываются только письма из реестра исходящих." if journal else "В этой версии учитываются КП в 1С с подтверждённым признаком «вне тендера».")
    else:
        rules.append("В этой версии нужно КП в 1С с зафиксированным подтверждением отправки/сканом письма.")
    if journal:
        topics = engine.config.get("letters_sheet", {}).get("topics") or []
        rules.append("В реестре заполните реальную дату письма в формате ДД.ММ.ГГГГ, ответственного (свою фамилию), получателя и тему КП." +
                     (" Тема должна содержать одну из настроенных фраз: " + "; ".join(str(t) for t in topics) + "." if topics else ""))
    else:
        rules.append("Для исходящего письма CRM проверьте связь со сделкой, ответственного, дату отправки, тему коммерческого предложения и прикреплённый файл; затем обновите данные.")
    records = []
    for k in kps:
        counted, note = True, None
        checks = [_check("source", "КП в 1С", True, "Документ присутствует в выгрузке КП сотрудника за период.")]
        if union:
            pair = pairs.get(k["id"])
            checks.append(_check("unique", "Учтено один раз", True,
                f"Совпало с письмом от {pair['dt'][:10]}: {pair.get('subject') or 'КП'}. Письмо отдельно не добавляется." if pair else "Отдельный документ 1С; письмо не обязательно."))
        elif aggregate:
            counted = None if unallocated else bool(len(letters) >= len(kps))
            checks.append(_check("confirmation", "Подтверждение письмами (суммарно)", None if unallocated else counted,
                f"КП: {len(kps)}, писем: {len(letters)}. Индивидуальная связь документов не передана.",
                "Зафиксируйте реальные отправленные письма/вложения и ответственного в исходной системе. Индивидуальное сопоставление по этим историческим данным недоступно."))
            note = "Индивидуальное участие не определено: применяется суммарное правило." if unallocated else "Участие по суммарному правилу; конкретная связь с письмом не подтверждалась."
        elif tender and journal:
            counted = False
            checks.append(_check("journal", "Входит в правило месяца", False, "По правилу этого месяца сам КП в 1С не увеличивает факт; результат считается по записям реестра.",
                "Проверьте исходящее письмо в реестре: вклад считается у письма, а не у этой строки КП. Если реальное письмо уже зарегистрировано, создавать повторную запись не нужно."))
        else:
            flag = k.get("out_of_tender") if tender else k.get("confirmed")
            counted = None if fact is None and flag is None else bool(flag)
            checks.append(_check("outside_tender" if tender else "confirmed", "Вне тендера" if tender else "Есть подтверждение отправки",
                None if flag is None else bool(flag), "Признак в источнике: " + ("нет данных" if flag is None else "да" if flag else "нет"),
                "Проверьте корректный признак «вне тендера» и его выгрузку из 1С." if tender else "Зафиксируйте подтверждение отправки/скан исходящего письма в 1С и обновите данные."))
        records.append(_record("kp:" + str(k["id"]), k["dt"], k.get("doc") or "КП в 1С", k.get("client"), "1С", checks, counted, note=note))
    for l in letters:
        duplicate = bool(union and str(l["id"]) in pairs_by_letter)
        counted = not duplicate if union else True if tender and journal else None if aggregate else False
        checks = [_check("source", "Подходящее исходящее письмо", True, "Письмо присутствует в выгрузке сотрудника за период.")]
        note = None
        if union:
            partner = pairs_by_letter.get(str(l["id"]))
            matched = next((k for k in kps if k["id"] == partner), None)
            checks.append(_check("unique", "Отдельный результат", not duplicate,
                "Учтено вместе с КП " + str((matched or {}).get("doc") or partner) if duplicate else "Совпадающее КП в 1С не найдено; письмо считается отдельно."))
            if duplicate:
                note = "Уже учтено вместе с КП в 1С. Повторно в факт не добавляется; исправлять эту запись не нужно."
        elif aggregate:
            note = "Это подтверждение для суммарного правила. Индивидуальная связь с КП не передана."
        elif not (tender and journal):
            note = "В этом месяце само письмо не увеличивает факт; учитывается признак подтверждения у КП в 1С."
        records.append(_record("letter:" + str(l["id"]), l["dt"], l.get("subject") or "Исходящее письмо", l.get("deal"),
            "Реестр исходящих" if journal else "Битрикс24", checks, counted, duplicate,
            url=record_url(engine.config,"kp_letters",l), note=note,
            client_link=_crm_url(engine,"DEAL:"+str(l.get("deal"))) if str(l["id"]).startswith("mail") else None))
    records.sort(key=lambda r: (r["when"] or "", r["id"]))
    return records, rules, unallocated


def build(engine, employee, ctx, item, component, kind):
    if kind == "calls":
        records, rules, unallocated = _calls(engine, employee, ctx, item)
    elif kind == "meetings":
        records, rules, unallocated = _meetings(engine, employee, ctx, item)
    elif kind == "kp":
        records, rules, unallocated = _kp(engine, employee, ctx, item, component["fact"])
    else:
        raise ValueError("Неизвестная часть KPI")
    rules.append("Список содержит все загруженные записи сотрудника за выбранный месяц. Изменения в CRM, 1С и реестре появятся после обновления данных; при отсутствии записи проверьте дату, ответственного и условия выгрузки.")
    return {"kind": kind, "title": component["name"], "fact": component["fact"], "target": component["target"],
            "rules": rules, "records": records,
            "summary": {"total": len(records), "counted": sum(r["counted"] is True for r in records),
                        "duplicates": sum(r["duplicate"] for r in records), "unallocated": unallocated}}
