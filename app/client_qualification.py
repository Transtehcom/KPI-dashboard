"""Условия активного клиента — описанная потребность, стратегия и даты её пунктов."""
import re
from html import unescape

from .common import parse_date


EMPTY_TEXT = {"нет", "no", "да", "yes", "y", "n", "true", "false", "не выявлена", "не выявлено",
              "не выявлен", "не определена", "не определено", "отсутствует", "не знаю", "пока нет", "нет данных",
              "не заполнено", "потребность не выявлена", "стратегия отсутствует", "выявлена",
              "потребность выявлена", "невыявлена", "невыявлено", "нет потребности", "н д"}


def meaningful_text(value, need=False):
    if not isinstance(value, str):
        return False
    plain = unescape(re.sub(r"<[^>]*>", " ", value))
    words = " ".join(re.findall(r"[^\W\d_]+", plain.casefold()))
    if not words or words in EMPTY_TEXT:
        return False
    if need and words.startswith(("нет ", "пока нет ", "не выявлен", "потребность не выявлен")):
        return False
    return True


def values(value):
    items = value if isinstance(value, (list, tuple)) else [value]
    return [item for item in items if item is not None and item is not False and str(item).strip()]


def qualify_client(need, strategy, dates):
    items, date_items = values(strategy), values(dates)
    strategy_count = sum(meaningful_text(item) for item in items)
    parsed_dates = [parse_date(date) for date in date_items]
    date_count = sum(date is not None and date.year >= 1900 for date in parsed_dates)
    complete = strategy_count > 0 and strategy_count == len(items) == date_count == len(date_items)
    return {"need": int(meaningful_text(need, need=True)), "strategy": int(complete),
            "strategy_count": strategy_count, "strategy_date_count": date_count}
