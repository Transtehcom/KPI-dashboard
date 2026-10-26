"""Реестр панели «Метрики → Отдел продаж» из 1С (УТ 11, ТТК): тендеры, КП клиентам и заказы клиентов с начала года.

Одна цепочка — один объект: заказ клиента вырос из КП или тендера (ДокументОснование), КП может опираться на тендер.
Объектом цепочки становится самый поздний документ; документ, по которому уже есть следующий, закрывается
(«Закрыто успешно»). Статус 1С показывается как есть (status_1c). Этап ставится только там, где он однозначен;
ручной выбор этапа в реестре синхронизация потом не меняет (sales_objects.stage_auto = 0).

  тендер:  Проиграно / Отклонён / Отменено → «Закрыто без результата»; есть КП или заказ → «Закрыто успешно»;
           Черновик, Действует, Выиграно без документов дальше — этап не ставится (задаёт руководитель);
  КП:      Отменено → «Закрыто без результата»; есть заказ → «Закрыто успешно»; иначе «КП»;
  заказ:   статус «Закрыт» → «Закрыто успешно»; по заказу есть неоплаченный долг в 1С → «Приёмка и оплата»;
           иначе «Исполнение».
Канал — «Тендеры», если цепочка начинается с тендера, иначе «Прямые».
"""
import datetime as dt

SINCE = "2026-01-01"
TENDER = "Document_ТТК_Тендер"
KP = "Document_КоммерческоеПредложениеКлиенту"
LOST_TENDER = {"Проиграно", "Отклонён", "Отменено"}


def _kind(typ):
    typ = typ or ""
    return "tender" if typ.endswith(TENDER) else "kp" if typ.endswith(KP) else None


def _login(managers, name):
    return managers.get(str(name or "").strip().lower())


def fetch(onec, managers, since=SINCE):
    """managers: {ФИО в 1С (нижний регистр): login}. Возвращает [{key, source, number, ...}] без этапов заказов."""
    flt = f"DeletionMark eq false and Date ge datetime'{since}T00:00:00'"
    tenders = onec._odata(TENDER, {"$format": "json", "$expand": "ВедущийМенеджер,Клиент", "$filter": flt,
                                   "$select": "Ref_Key,Date,Number,Статус,СуммаДокумента,НаименованиеЛота,"
                                              "ВедущийМенеджер/Description,Клиент/Description"})
    kps = onec._odata(KP, {"$format": "json", "$expand": "Менеджер,Клиент", "$filter": flt,
                           "$select": "Ref_Key,Date,Number,Статус,СуммаДокумента,ДокументОснование,ДокументОснование_Type,"
                                      "Менеджер/Description,Клиент/Description"})
    orders = onec._odata("Document_ЗаказКлиента", {"$format": "json", "$expand": "Менеджер,Контрагент", "$filter": flt,
                                                   "$select": "Ref_Key,Date,Number,Статус,СуммаДокумента,ДокументОснование,"
                                                              "ДокументОснование_Type,Менеджер/Description,Контрагент/Description"})
    tender_by = {t["Ref_Key"]: t for t in tenders}
    kp_by = {k["Ref_Key"]: k for k in kps}
    ordered = {o.get("ДокументОснование") for o in orders}
    based_kp = {k.get("ДокументОснование") for k in kps}  # тендеры, по которым уже есть КП

    def tender_root(doc):  # тендер в начале цепочки документа или None
        kind = _kind(doc.get("ДокументОснование_Type"))
        if kind == "tender":
            return tender_by.get(doc.get("ДокументОснование"))
        if kind == "kp":
            kp = kp_by.get(doc.get("ДокументОснование"))
            return tender_root(kp) if kp else None
        return None

    out = []
    for t in tenders:
        covered = t["Ref_Key"] in ordered or t["Ref_Key"] in based_kp
        stage = "Закрыто без результата" if t["Статус"] in LOST_TENDER else "Закрыто успешно" if covered else None
        out.append({"key": t["Ref_Key"], "source": "tender", "id": t["Number"].strip(), "type": "Сделка", "channel": "Тендеры",
                    "client": (t.get("Клиент") or {}).get("Description") or t.get("НаименованиеЛота"),
                    "emp": _login(managers, (t.get("ВедущийМенеджер") or {}).get("Description")), "status_1c": t["Статус"],
                    "amount": t.get("СуммаДокумента"), "entered": t["Date"][:16], "stage": stage, "order_1c": None})
    for k in kps:
        stage = "Закрыто без результата" if k["Статус"] == "Отменено" else "Закрыто успешно" if k["Ref_Key"] in ordered else "КП"
        out.append({"key": k["Ref_Key"], "source": "kp", "id": "КП " + k["Number"].strip(), "type": "Сделка",
                    "channel": "Тендеры" if tender_root(k) else "Прямые", "client": (k.get("Клиент") or {}).get("Description"),
                    "emp": _login(managers, (k.get("Менеджер") or {}).get("Description")), "status_1c": k["Статус"],
                    "amount": k.get("СуммаДокумента"), "entered": k["Date"][:16], "stage": stage, "order_1c": None})
    for o in orders:
        root = tender_root(o)
        who = (root.get("ВедущийМенеджер") if root else None) or o.get("Менеджер")
        out.append({"key": o["Ref_Key"], "source": "order", "id": o["Number"].strip(), "type": "Исполнение",
                    "channel": "Тендеры" if root else "Прямые", "client": (o.get("Контрагент") or {}).get("Description"),
                    "emp": _login(managers, (who or {}).get("Description")), "status_1c": o["Статус"],
                    "amount": o.get("СуммаДокумента"), "entered": o["Date"][:16],
                    "stage": "Закрыто успешно" if o["Статус"] == "Закрыт" else "Исполнение", "order_1c": o["Number"].strip()})
    return out


def upsert(con, rows):
    """Записать объекты в реестр, не трогая ручные поля. Возвращает (создано, обновлено).
    Строка с match — объект цепочки уже есть в реестре (id в match): ему лишь ставится этап, если он ещё пуст."""
    from .sales_panel import STAGE
    unpaid = {r[0] for r in con.execute("SELECT DISTINCT doc FROM receivables WHERE paid_date IS NULL OR paid_date = ''")}
    now = dt.datetime.now().isoformat(timespec="seconds")
    seen, sources, created, updated = set(), {r["source"] for r in rows}, 0, 0
    for r in rows:
        if r["source"] == "order" and r["stage"] == "Исполнение" and r["order_1c"] in unpaid:
            r["stage"] = "Приёмка и оплата"
        if r.get("match"):
            target = con.execute("SELECT stage, stage_auto FROM sales_objects WHERE id = ?", (r["match"],)).fetchone()
            if target:
                seen.add(r["id"])
                if r["stage"] in STAGE and not target["stage"] and target["stage_auto"]:
                    con.execute("UPDATE sales_objects SET stage = ?, updated_at = ? WHERE id = ?", (r["stage"], now, r["match"]))
                    updated += 1
                continue
        seen.add(r["id"])
        old = con.execute("SELECT * FROM sales_objects WHERE id = ?", (r["id"],)).fetchone()
        if old is None:
            con.execute("INSERT INTO sales_objects (id, type, client, emp, channel, stage, stage_auto, entered, order_1c, source,"
                        " status_1c, amount, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["type"], r["client"], r["emp"], r["channel"], r["stage"], 1, r["entered"], r["order_1c"],
                         r["source"], r["status_1c"], r["amount"], r.get("by", "1С"), now))
            created += 1
            continue
        stage = r["stage"] if (old["stage_auto"] or not old["stage"]) and r["stage"] in STAGE else old["stage"]
        con.execute("UPDATE sales_objects SET type = ?, client = ?, emp = COALESCE(emp, ?), channel = COALESCE(channel, ?), stage = ?,"
                    " stage_auto = CASE WHEN stage_auto = 1 OR stage IS NULL THEN 1 ELSE 0 END, entered = COALESCE(entered, ?),"
                    " order_1c = COALESCE(order_1c, ?), source = ?, status_1c = ?, amount = ?, updated_at = ? WHERE id = ?",
                    (r["type"], r["client"], r["emp"], r["channel"], stage, r["entered"], r["order_1c"], r["source"],
                     r["status_1c"], r["amount"], now, r["id"]))
        updated += 1
    # документ исчез из источника (удалён, сделка закрыта): объект закрывается, если этап не выбирал руководитель
    for oid, src in con.execute("SELECT id, source FROM sales_objects WHERE source IS NOT NULL AND stage_auto = 1"
                                " AND (stage IS NULL OR stage NOT LIKE 'Закрыто%')").fetchall():
        if src in sources and oid not in seen:
            con.execute("UPDATE sales_objects SET stage = 'Закрыто без результата', updated_at = ? WHERE id = ?", (now, oid))
    return created, updated
