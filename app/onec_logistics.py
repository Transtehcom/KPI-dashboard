"""Логистика из 1С (УТ 11, ТТК): заявки на расчёт транспортных расходов (Продажи → Заявка на расчёт транспортных
расходов, документ ТТК_ЗаявкаНаРасчетТранспортныхРасходов) — для раздела «Метрики → Логистика».

Берутся все заявки периода в любом статусе, в том числе непроведённые и с пометкой удаления (признаки сохраняются).
Ответственный — «Ответственный логист»; логисты, которых нет в дашборде, сохраняются как «1c:ФИО» (onec.KEEP_UNMAPPED).
history — история статусов [[статус, дата установки]] по времени. Маршрут, перевозчик и стоимость — по вариантам
расчёта, отмеченным флажком (несколько отмеченных — плечи одной перевозки, стоимость суммируется); без флажков — первый.
"""
import json


def _when(d):
    return d.isoformat() + "T00:00:00"


def _text(v):
    return " ".join(str(v or "").split())


def _route(results):
    """(маршрут, перевозчик, стоимость с НДС) по выбранным вариантам расчёта."""
    chosen = [x for x in results if x.get("Флаг")] or results[:1]
    if not chosen:
        return None, None, None
    legs, carriers = [], []
    for x in chosen:
        src, dst = _text(x.get("АдресОтправки")), _text(x.get("АдресДоставки"))
        leg = " → ".join(p for p in (src, dst) if p)
        if leg and leg not in legs:
            legs.append(leg)
        c = _text(x.get("ТранспортнаяКомпания"))
        if c and c not in carriers:
            carriers.append(c)
    return "; ".join(legs) or None, ", ".join(carriers) or None, round(sum(x.get("СтоимостьСНДС") or 0 for x in chosen), 2)


def fetch_transport_requests(onec, ds, date_from, date_to):
    rows = onec._odata("Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов", {
        "$format": "json", "$expand": "ОтветственныйЛогист,Менеджер",
        "$filter": f"Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,Статус,АдресДоставки,ИсторияСтатусов,РезультатыРасчета,"
                   "ОтветственныйЛогист/Description,Менеджер/Description"})
    out = []
    for r in rows:
        hist = sorted(([h["Статус"], h["ДатаУстановки"][:19]] for h in r.get("ИсторияСтатусов") or []
                       if h.get("Статус") and h.get("ДатаУстановки") and not h["ДатаУстановки"].startswith("0001")),
                      key=lambda x: x[1])
        route, carrier, cost = _route(r.get("РезультатыРасчета") or [])
        out.append({"id": r["Ref_Key"], "manager": (r.get("ОтветственныйЛогист") or {}).get("Description"),
                    "dt": r["Date"][:10], "created_at": r["Date"][:19], "doc": (r.get("Number") or "").strip(),
                    "status": r.get("Статус"), "posted": r.get("Posted"), "deleted": r.get("DeletionMark"),
                    "sales_manager": (r.get("Менеджер") or {}).get("Description"), "address": r.get("АдресДоставки"),
                    "history": json.dumps(hist, ensure_ascii=False), "route": route, "carrier": carrier, "cost": cost})
    return out
