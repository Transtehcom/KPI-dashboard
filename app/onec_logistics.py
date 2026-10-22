"""Логистика из 1С (УТ 11, ТТК): заявки на расчёт транспортных расходов (Продажи → Заявка на расчёт транспортных
расходов, документ ТТК_ЗаявкаНаРасчетТранспортныхРасходов) — для раздела «Метрики → Логистика».

Берутся все заявки периода в любом статусе, в том числе непроведённые и с пометкой удаления (признаки сохраняются).
Ответственный — «Ответственный логист»; логисты, которых нет в дашборде, сохраняются как «1c:ФИО» (onec.KEEP_UNMAPPED).
"""


def _when(d):
    return d.isoformat() + "T00:00:00"


def fetch_transport_requests(onec, ds, date_from, date_to):
    rows = onec._odata("Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов", {
        "$format": "json", "$expand": "ОтветственныйЛогист,Менеджер",
        "$filter": f"Date ge datetime'{_when(date_from)}' and Date le datetime'{date_to.isoformat()}T23:59:59'",
        "$select": "Ref_Key,Number,Date,Posted,DeletionMark,Статус,АдресДоставки,"
                   "ОтветственныйЛогист/Description,Менеджер/Description"})
    return [{"id": r["Ref_Key"], "manager": (r.get("ОтветственныйЛогист") or {}).get("Description"),
             "dt": r["Date"][:10], "created_at": r["Date"][:19], "doc": (r.get("Number") or "").strip(),
             "status": r.get("Статус"), "posted": r.get("Posted"), "deleted": r.get("DeletionMark"),
             "sales_manager": (r.get("Менеджер") or {}).get("Description"), "address": r.get("АдресДоставки")}
            for r in rows]
