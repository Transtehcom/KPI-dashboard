"""Обезличивание расшифровок перед передачей в Claude (псевдонимизация по 152-ФЗ).

Имена, организации, места — natasha (текст GigaAM v3 с заглавными буквами) и названия клиента из CRM;
телефоны, почта, сайты, ИНН — шаблонами. Каждое найденное заменяется меткой [ИМЯ_1], [ОРГ_2], [МЕСТО_1], [ТЕЛЕФОН_1]…;
одно и то же лицо в разных падежах получает одну метку. Таблица замен остаётся на этом компьютере (call_analysis.pii),
ответ Claude возвращается с подстановкой исходных значений (restore). Числа (цены, сроки, объёмы) не трогаются.
Название нашей компании оставляется — по нему Claude понимает, кто менеджер."""
import re
import threading

# своя компания и её искажения при распознавании — не обезличиваются
OWN_COMPANY = re.compile(r"транс\s*-?\s*те[хк]?\s*ком|транстелеком|транстеком|\bттк\b", re.I)
# слова из названия клиента, по которым его не ищем: организационные формы и общие слова
GENERIC = {"ооо", "оао", "зао", "пао", "ао", "ип", "фгуп", "муп", "гуп", "компания", "группа", "завод", "филиал",
           "общество", "ограниченной", "ответственностью", "акционерное", "публичное", "холдинг", "предприятие",
           "производственная", "производственное", "торговый", "дом", "центр", "управление", "служба", "входящий",
           "исходящий", "звонок", "лид", "сделка", "контакт", "новый"}
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
           "служба", "скайп", "skype", "zoom", "зум", "телемост", "teams", "whatsapp", "ватсап", "телеграм", "telegram"}


def _not_org(text, morph):
    """Фрагмент — не название организации: только термины из NOT_ORG или в нём есть глагол («Вышлите КП»)."""
    words = re.findall(r"[\w-]+", text.lower())
    return all(w in NOT_ORG for w in words) or any(morph.parse(w)[0].tag.POS in ("VERB", "INFN") for w in words)


class Pseudonymizer:
    """Метки одного звонка: одна и та же сущность получает одну метку во всех предложениях."""

    def __init__(self, crm_names=()):
        self.mapping = {}  # метка → исходное значение (первое встретившееся написание)
        self._keys = {}  # нормализованный ключ → метка
        self._count = {}
        # отличительные слова названия клиента / контакта из CRM: ищутся по началу слова (падежи)
        self.crm = {}  # основа слова → номер названия
        for i, name in enumerate(crm_names):
            for w in re.findall(r"[\w-]+", name or ""):
                if len(w) >= 5 and w.lower() not in GENERIC and not OWN_COMPANY.search(w):
                    self.crm.setdefault(w.lower()[:max(5, len(w) - 2)], i)
        self.crm_stems = sorted(self.crm, key=len, reverse=True)

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
        for sp in doc.spans:
            if OWN_COMPANY.search(sp.text) or any(s < sp.stop and sp.start < e for s, e, _ in spans):
                continue
            if sp.type == "ORG" and _not_org(sp.text, n["morph"]):
                continue
            stem = next((st for st in self.crm_stems for w in sp.text.lower().split() if w.startswith(st)), None)
            if stem:  # клиент из CRM — организация, даже если natasha сочла его именем или местом
                spans.append((sp.start, sp.stop, self._label("ОРГ", f"crm:{self.crm[stem]}", sp.text)))
                continue
            words, nomn = _normal(sp.text, n["morph"])
            kind = NER_LABELS.get(sp.type, "ОРГ")
            # «Марии Мельниковой» и «Мельникова Мария» — одно лицо; в ответ подставляется именительный падеж
            spans.append((sp.start, sp.stop, self._label(kind, " ".join(sorted(words)), nomn if kind == "ИМЯ" else sp.text)))
        for m in re.finditer(r"[\w-]+", text):  # название клиента из CRM, которое natasha не узнала
            w = m.group().lower()
            if any(s < m.end() and m.start() < e for s, e, _ in spans):
                continue
            stem = next((st for st in self.crm_stems if w.startswith(st)), None)
            if stem:
                spans.append((m.start(), m.end(), self._label("ОРГ", f"crm:{self.crm[stem]}", m.group())))
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
        """Подставить исходные значения вместо меток в строках ответа (рекурсивно по спискам и словарям)."""
        if isinstance(value, str):
            return re.sub(r"\[[A-ZА-ЯЁ]+_\d+\]", lambda m: self.mapping.get(m.group(), m.group()), value)
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        return value
