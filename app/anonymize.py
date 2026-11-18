"""Обезличивание расшифровок перед передачей в Codex (псевдонимизация по 152-ФЗ).

Имена, организации, места — natasha (текст GigaAM v3 с заглавными буквами) и названия клиента из CRM;
телефоны, почта, сайты, ИНН — шаблонами. Каждое найденное заменяется меткой [ИМЯ_1], [ОРГ_2], [МЕСТО_1], [ТЕЛЕФОН_1]…;
одно и то же лицо в разных падежах получает одну метку. Таблица замен остаётся на этом компьютере (call_analysis.pii),
ответ Codex возвращается с подстановкой исходных значений (restore). Числа (цены, сроки, объёмы) не трогаются.
Название нашей компании оставляется — по нему Codex понимает, кто менеджер."""
import re
import threading
from difflib import SequenceMatcher

# своя компания и её искажения при распознавании — не обезличиваются
OWN_COMPANY = re.compile(r"транс\s*-?\s*те[хк]?\s*ком|транстелеком|транстеком|\bттк\b", re.I)
# слова из названия клиента, по которым его не ищем: организационные формы и общие слова
GENERIC = {"ооо", "оао", "зао", "пао", "ао", "ип", "фгуп", "муп", "гуп", "компания", "группа", "завод", "филиал",
           "общество", "ограниченной", "ответственностью", "акционерное", "публичное", "холдинг", "предприятие",
           "производственная", "производственное", "торговый", "дом", "центр", "управление", "служба", "входящий",
           "исходящий", "звонок", "лид", "сделка", "контакт", "новый", "генерация", "комбинат",
           "горно-обогатительный", "электросетевая"}
PATTERNS = [  # (метка, регулярное выражение) — порядок важен: сначала почта, потом сайты и номера
    ("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|[\w.-]+\s+собачк[аеуи]\s+[\w.-]+(?:\.[a-zа-я]{2,})?"),
    ("САЙТ", r"\b[\w-]+(?:\.[\w-]+)*\.(?:ru|рф|com|net|org|su|info|pro)\b"),
    ("ТЕЛЕФОН", r"(?:\+7|\b8|\b7)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"),
    ("ИНН", r"\b\d{12}\b|\b\d{10}\b"),
    ("НОМЕР", r"\b\d{13,}\b"),  # счета, карты
]
NER_LABELS = {"PER": "ИМЯ", "ORG": "ОРГ", "LOC": "МЕСТО"}

_ner = None
_lock = threading.Lock()


def _natasha():
    """Модели natasha грузятся один раз (несколько секунд)."""
    global _ner
    with _lock:
        if _ner is None:
            import pymorphy3
            from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter
            emb = NewsEmbedding()
            # нормализация natasha (MorphVocab) работает на pymorphy2, который не ставится на Python 3.14
            _ner = {"Doc": Doc, "seg": Segmenter(), "ner": NewsNERTagger(emb), "morph": pymorphy3.MorphAnalyzer()}
        return _ner


def _normal(text, morph):
    """«Марии Мельниковой» → ([«мария», «мельников»], «Мария Мельникова»): начальные формы слов для сравнения
    и именительный падеж с сохранением рода — для подстановки в ответ."""
    grams = lambda p: set(str(p.tag).replace(" ", ",").split(","))
    words = [(w, morph.parse(w)) for w in re.findall(r"[\w-]+", text)]
    # род берём по имени: «Мельникова» рядом с «Мария» — женская фамилия, а не родительный падеж от «Мельников»
    gender = next((g for _, ps in words for p in ps if "Name" in grams(p) for g in ("masc", "femn") if g in grams(p)), None)
    keys, nomn = [], []
    for w, parses in words:
        named = [p for p in parses if {"Name", "Surn", "Patr", "Geox", "Orgn"} & grams(p)]
        best = next((p for p in named if gender and gender in grams(p)), named[0] if named else parses[0])
        keys.append(best.normal_form)
        inf = best.inflect({"nomn"})
        nomn.append((inf.word if inf else best.normal_form).capitalize())
    return keys, " ".join(nomn)


# слова, которые natasha принимает за организации, но это термины разговора — их оставляем
NOT_ORG = {"кп", "ткп", "нмцк", "ндс", "вкс", "звкс", "упд", "тз", "тендер", "закупка", "почта", "компания", "отдел",
           "служба", "скайп", "skype", "zoom", "зум", "телемост", "teams", "whatsapp", "ватсап", "телеграм", "telegram",
           "бпла", "пдф", "pdf", "жд", "лэп", "жби", "лкм", "рэб"}

# Разговорные частицы и числительные не должны превращаться в названия из-за заглавной буквы
# или дефиса в автоматической расшифровке («Да-да», «Ноль-ноль», «Мы-то»).
DISCOURSE = {"да", "нет", "всё", "все", "так", "там", "тут", "мы", "вы", "я", "ты", "то", "это", "ещё", "еще",
             "сегодня", "завтра", "ноль", "ага", "угу", "алло", "ало", "ааа", "аа", "ээ", "эээ"}


def _discourse(text):
    words = re.findall(r"[a-zа-яё]+", text.lower())
    return bool(words) and all(w in DISCOURSE for w in words)


# слова с заглавной буквы, которые словарь считает именами или не знает, но это не персональные данные
KEEP_WORDS = {"битрикс", "битрикс24", "макс", "максе", "max", "авито", "яндекс", "гугл", "google", "excel", "эксель",
              "ватсапе", "телеграме", "скайпе", "россия", "россии", "рф", "москва", "москве", "алло", "ало", "угу", "ага"}


def _not_org(text, morph):
    """Фрагмент — не название организации: только термины из NOT_ORG или в нём есть глагол («Вышлите КП»)."""
    words = re.findall(r"[\w-]+", text.lower())
    return all(w in NOT_ORG or w.isdigit() or morph.parse(w)[0].normal_form in NOT_ORG for w in words) or any(
        morph.parse(w)[0].tag.POS in ("VERB", "INFN") for w in words)


def _one_edit_apart(left, right):
    if abs(len(left) - len(right)) > 1:
        return False
    edits = sum(max(left_end - left_start, right_end - right_start)
                for kind, left_start, left_end, right_start, right_end
                in SequenceMatcher(None, left, right).get_opcodes() if kind != "equal")
    return edits <= 1


class Pseudonymizer:
    """Метки одного звонка: одна и та же сущность получает одну метку во всех предложениях."""

    def __init__(self, crm_names=(), *, canonical_crm=False):
        self.mapping = {}  # метка → подтверждённое название CRM или исходное значение
        self._keys = {}  # нормализованный ключ → метка
        self._count = {}
        self.canonical_crm = canonical_crm  # только подтверждённая компания, не заголовок лида или имя контакта
        # отличительные слова названия клиента / контакта из CRM: ищутся по началу слова (падежи)
        self.crm_names = tuple(dict.fromkeys(name.strip() for name in crm_names if name and name.strip()))
        morph = _natasha()["morph"] if self.crm_names else None
        self.generic_lemmas = {morph.parse(w)[0].normal_form for w in GENERIC} if morph else GENERIC
        self.crm = {}  # основа слова → номера названий, включая неоднозначные совпадения
        for i, name in enumerate(self.crm_names):
            for w in re.findall(r"[\w-]+", name or ""):
                word = w.lower().replace("ё", "е")
                if (len(word) >= 5 and not word.isdigit() and not OWN_COMPANY.search(word)
                        and morph.parse(word)[0].normal_form not in self.generic_lemmas):
                    self.crm.setdefault(word[:max(5, len(word) - 2)], set()).add(i)

    def _crm_matches(self, text):
        words = re.findall(r"[\w-]+", text.lower().replace("ё", "е"))
        matches = set()
        for stem, identities in self.crm.items():
            if any(w.startswith(stem) for w in words):
                matches.update(identities)
        if not matches and self.canonical_crm:
            # Одна небольшая ошибка в длинном отличительном слове («Новокаалиновый»).
            # Сверяем только с клиентами этого звонка. Короткие и неоднозначные названия не угадываем.
            for stem, identities in self.crm.items():
                if len(stem) < 8:
                    continue
                prefixes = [w[:len(stem)] for w in words if len(w) >= len(stem) and w[:4] == stem[:4]]
                if any(_one_edit_apart(prefix, stem) for prefix in prefixes):
                    matches.update(identities)
        return matches

    def _compatible_crm(self, text, identity):
        """Общий географический признак не должен подменять другую явно названную организацию."""
        morph = _natasha()["morph"]
        known = [morph.parse(w)[0].normal_form for w in re.findall(r"[\w-]+", self.crm_names[identity].lower())]
        stems = [stem for stem, identities in self.crm.items() if identity in identities]
        for raw in re.findall(r"[\w-]+", text):
            word = morph.parse(raw.lower())[0].normal_form
            if word in self.generic_lemmas or (len(raw) < 5 and not raw.isupper() and not raw.isdigit()):
                continue
            if any(word.startswith(stem) for stem in stems):
                continue
            if not any(_one_edit_apart(word, candidate) for candidate in known):
                return False
        return True

    def _crm_label(self, text, *, canonical=True):
        matches = self._crm_matches(text)
        if not matches:
            return None
        words, _ = _normal(text, _natasha()["morph"])
        key, surface = " ".join(sorted(words)), text
        if len(matches) == 1 and canonical and self.canonical_crm:
            identity = next(iter(matches))
            if self._compatible_crm(text, identity):
                key, surface = f"crm:{identity}", self.crm_names[identity]
        return self._label("ОРГ", key, surface)

    def _label(self, kind, key, surface):
        key = (kind, key.lower().replace("ё", "е"))
        if key not in self._keys:
            self._count[kind] = self._count.get(kind, 0) + 1
            lab = f"[{kind}_{self._count[kind]}]"
            self._keys[key] = lab
            self.mapping[lab] = surface
        return self._keys[key]

    def mask(self, text):
        spans = []  # (начало, конец, метка)
        for kind, rx in PATTERNS:
            for m in re.finditer(rx, text, re.I):
                if not any(s < m.end() and m.start() < e for s, e, _ in spans):
                    spans.append((m.start(), m.end(), self._label(kind, re.sub(r"\D", "", m.group()) or m.group(),
                                                                  m.group())))
        n = _natasha()
        doc = n["Doc"](text)
        doc.segment(n["seg"])
        doc.tag_ner(n["ner"])
        # Явное представление организации имеет приоритет над словарём обычных слов.
        # Например, «компания Опора» — название, а «опора 3» — товарная позиция.
        for m in re.finditer(r'\b(?i:компани[яию]|фирм[ауы])\s+[«"“]?'
                             r'([A-ZА-ЯЁ][\w-]*(?:\s+[A-ZА-ЯЁ][\w-]*){0,3})', text):
            start, end = m.span(1)
            for sp in doc.spans:
                if sp.start < end and start < sp.stop:
                    start, end = min(start, sp.start), max(end, sp.stop)
            end = start + len(text[start:end].rstrip(".,!?… "))
            name = text[start:end]
            if OWN_COMPANY.search(name) or any(s < end and start < e for s, e, _ in spans):
                continue
            words, _ = _normal(name, n["morph"])
            label = self._crm_label(name) or self._label("ОРГ", " ".join(sorted(words)), name)
            spans.append((start, end, label))
        # Сохраняем номерные товарные позиции читаемыми даже при одноимённом клиенте CRM.
        # Явное представление компании выше уже получило приоритет.
        for m in re.finditer(r"\bопор[а-яё]*\s+\d+\b", text, re.I):
            product_words = re.findall(r"[\w-]+", m.group().lower())
            larger_entity = any(sp.type in ("ORG", "PER") and sp.start < m.end() and m.start() < sp.stop
                                and re.findall(r"[\w-]+", sp.text.lower()) != product_words for sp in doc.spans)
            if not larger_entity and not any(s < m.end() and m.start() < e for s, e, _ in spans):
                spans.append((m.start(), m.end(), m.group()))
        for sp in doc.spans:
            if OWN_COMPANY.search(sp.text) or any(s < sp.stop and sp.start < e for s, e, _ in spans):
                continue
            label = self._crm_label(sp.text)
            if label:
                spans.append((sp.start, sp.stop, label))
                continue
            if _discourse(sp.text):
                continue
            words = re.findall(r"[\w-]+", sp.text.lower())
            if (sp.type == "ORG" and _not_org(sp.text, n["morph"])) or all(w in KEEP_WORDS or w in NOT_ORG for w in words):
                continue
            words, nomn = _normal(sp.text, n["morph"])
            kind = NER_LABELS.get(sp.type, "ОРГ")
            # «Марии Мельниковой» и «Мельникова Мария» — одно лицо; в ответ подставляется именительный падеж
            spans.append((sp.start, sp.stop, self._label(kind, " ".join(sorted(words)), nomn if kind == "ИМЯ" else sp.text)))
        for m in re.finditer(r"[\w-]+", text):  # название клиента из CRM, которое natasha не узнала
            w = m.group().lower()
            if any(s < m.end() and m.start() < e for s, e, _ in spans):
                continue
            label = self._crm_label(m.group(), canonical=False)
            if label:
                spans.append((m.start(), m.end(), label))
        # второй проход: слова с заглавной буквы, которые natasha пропустила (часто в начале предложения или искажённые
        # распознаванием): имя/фамилия/отчество/город/организация по словарю или вовсе незнакомое словарю слово
        for m in re.finditer(r"\b[A-ZА-ЯЁ][\w-]{2,}", text):
            w = m.group()
            if any(s < m.end() and m.start() < e for s, e, _ in spans) or OWN_COMPANY.search(w) \
                    or w.lower() in NOT_ORG or w.lower() in KEEP_WORDS or _discourse(w) or w.isupper():
                continue
            p = n["morph"].parse(w)[0]
            g = set(str(p.tag).replace(" ", ",").split(","))
            if g & {"Name", "Surn", "Patr"}:
                words, nomn = _normal(w, n["morph"])
                spans.append((m.start(), m.end(), self._label("ИМЯ", words[0], nomn)))
            elif "Geox" in g:
                spans.append((m.start(), m.end(), self._label("МЕСТО", p.normal_form, w)))
            elif "Orgn" in g or (not p.is_known and len(w) >= 5):
                spans.append((m.start(), m.end(), self._label("ОРГ", p.normal_form, w)))
        out, pos = [], 0
        for s, e, lab in sorted(spans):
            if s < pos:
                continue
            out.append(text[pos:s])
            out.append(lab)
            pos = e
        out.append(text[pos:])
        # «[ОРГ_1] [ОРГ_1]» (название из нескольких слов) → одна метка
        return re.sub(r"(\[[A-ZА-ЯЁ]+_\d+\])(?:\s+\1)+", r"\1", "".join(out))

    def restore(self, value):
        return restore(value, self.mapping)


def restore(value, mapping):
    """Подставить исходные значения вместо меток (рекурсивно по спискам и словарям). Без таблицы замен (удалена
    по сроку хранения) метки остаются как есть."""
    if isinstance(value, str):
        return re.sub(r"\[[A-ZА-ЯЁ]+_\d+\]", lambda m: (mapping or {}).get(m.group(), m.group()), value)
    if isinstance(value, list):
        return [restore(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: restore(v, mapping) for k, v in value.items()}
    return value
