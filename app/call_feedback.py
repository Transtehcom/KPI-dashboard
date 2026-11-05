"""Оценки автоматического разбора звонков и замечания для администратора."""
import datetime as dt
import json

from .common import db_lock


CRITERIA = {
    "transcript": "Текст расшифровки",
    "roles": "Кто говорит — сотрудник или клиент",
    "call_type": "Тип звонка",
    "summary": "Суть разговора",
    "secretary": "Секретарь или нужный человек",
    "offer": "Предложение и обещание КП",
    "agreement": "Договорённость",
    "tone": "Интонация сотрудника и клиента",
    "next_step": "Следующий шаг",
    "prices": "Цены",
    "deadlines": "Сроки",
    "objections": "Возражения",
}


class FeedbackConflict(ValueError):
    pass


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    result["criteria"] = json.loads(result["criteria"])
    result["status"] = "accepted" if result["rating"] == 5 else "resolved" if result["resolved_at"] else "pending"
    return result


def get(con, record, author):
    return _decode(con.execute("SELECT * FROM call_feedback WHERE record=? AND author=?", (record, author)).fetchone())


def listing(con, records, author=None):
    out = {}
    for offset in range(0, len(records), 500):
        chunk = records[offset:offset + 500]
        sql = f"SELECT * FROM call_feedback WHERE record IN ({','.join('?' * len(chunk))})"
        args = list(chunk)
        if author is not None:
            sql += " AND author=?"
            args.append(author)
        for row in con.execute(sql + " ORDER BY updated DESC", args):
            out.setdefault(row["record"], []).append(_decode(row))
    return out


def _text(value, label):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError(f"{label} должен быть текстом длиной до 2 000 символов")
    return value.strip()


def save(con, record, author, body):
    rating = body.get("rating")
    if type(rating) is not int or not 1 <= rating <= 5:
        raise ValueError("Выберите целую оценку от 1 до 5")
    comment = _text(body.get("comment", ""), "Комментарий")
    criteria = body.get("criteria", [])
    if not isinstance(criteria, list) or any(not isinstance(key, str) or key not in CRITERIA for key in criteria):
        raise ValueError("Выберите критерии из списка")
    criteria = list(dict.fromkeys(criteria)) if rating < 5 else []
    if rating < 5 and not comment:
        raise ValueError("Для оценки от 1 до 4 нужен комментарий")
    with db_lock():
        previous = get(con, record, author)
        if previous and (previous["rating"], previous["comment"], previous["criteria"]) == (rating, comment, criteria):
            return previous
        con.execute("INSERT INTO call_feedback(record,author,rating,comment,criteria,updated,revision) "
                    "VALUES (?,?,?,?,?,?,1) ON CONFLICT(record,author) DO UPDATE SET "
                    "rating=excluded.rating,comment=excluded.comment,criteria=excluded.criteria,updated=excluded.updated,"
                    "revision=call_feedback.revision+1,resolved_by=NULL,resolved_at=NULL,resolution=NULL",
                    (record, author, rating, comment, json.dumps(criteria), dt.datetime.now().isoformat()))
        con.commit()
        return get(con, record, author)


def resolve(con, record, author, revision, note, reviewer):
    if type(revision) is not int:
        raise ValueError("Обновите замечание перед проверкой")
    note = _text(note, "Ответ администратора")
    with db_lock():
        feedback = get(con, record, author)
        if not feedback or feedback["revision"] != revision:
            raise FeedbackConflict("Сотрудник изменил оценку. Обновите список звонков и проверьте новое замечание")
        if feedback["rating"] == 5:
            raise ValueError("В этой оценке нет замечаний для проверки")
        con.execute("UPDATE call_feedback SET resolved_by=?,resolved_at=?,resolution=? WHERE record=? AND author=?",
                    (reviewer, dt.datetime.now().isoformat(), note, record, author))
        con.commit()
        return get(con, record, author)
