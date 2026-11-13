"""Transport request status timestamps and the calendar processing deadline."""
import datetime as dt
import json

from .operations import local_datetime


def request_timing(history, status, now, processing_minutes):
    try:
        history = json.loads(history or "[]")
    except (TypeError, ValueError):
        history = []
    dates = {"Поступила": [], "Отработана": []}
    invalid = not isinstance(history, list)
    for event in history if isinstance(history, list) else []:
        if not isinstance(event, list) or len(event) != 2 or not isinstance(event[0], str):
            invalid = True
            continue
        if event[0] in dates:
            moment = local_datetime(event[1])
            if moment:
                dates[event[0]].append(moment)
            else:
                invalid = True
    received = min(dates["Поступила"], default=None)
    finished = max(dates["Отработана"], default=None)
    now = local_datetime(now)
    category, result = "unknown", "Нет данных"
    if received is None and finished is None:
        duration = "Нет дат статусов"
    elif invalid:
        duration = "Некорректная история статусов"
    elif received is None:
        duration = "Нет даты «Поступила»"
    elif received > now:
        duration = "Дата «Поступила» ещё не наступила"
    elif finished is None:
        duration = "Нет даты «Отработана»"
        if status != "Отработана":
            if now > received + dt.timedelta(minutes=processing_minutes):
                category, result = "late", "Не отработана, срок истёк"
            else:
                category, result = "pending", "Время ещё есть"
    elif finished < received:
        duration = "«Отработана» раньше «Поступила»"
    elif finished > now:
        duration = "Дата «Отработана» ещё не наступила"
    else:
        elapsed = finished - received
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours} ч {minutes:02} мин {seconds:02} с"
        if elapsed <= dt.timedelta(minutes=processing_minutes):
            category, result = "good", "В норматив"
        else:
            category, result = "late", "Просрочена"
    return {"received": received, "finished": finished, "duration": duration,
            "category": category, "result": result}
