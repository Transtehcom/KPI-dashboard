"""Local aliases and scoped document privacy for Codex context."""
import json
import re
from contextlib import contextmanager

from .anonymize import Pseudonymizer, restore

LABEL = re.compile(r"(\[[A-ZА-ЯЁ_]+(?:_\d+)?\])")

DOCUMENT_TITLE = re.compile(r"(?<![а-яёa-z0-9])(?:договор[а-яё]*|дог\.|сч[её]т(?:а|у|ом|е|ы|ов)?|коммерческ[а-яё]*[ _-]+предложени[а-яё]*|кп|накладн[а-яё]*|паспорт[а-яё]*|водительск[а-яё]*[ _-]+удостоверени[а-яё]*|в[/_ -]у|военн[а-яё]*[ _-]+билет[а-яё]*|снилс|инн)(?![а-яёa-z0-9])", re.I)


class PrivacyContext:
    def __init__(self, state=None, documents_only=False):
        self.documents_only = documents_only
        self._document_depth = 0
        state = state or {}
        self.values = dict(state.get("values", {}))
        self.sources = dict(state.get("sources", {}))
        self.aliases = dict(state.get("aliases", {}))
        self.secret_values = set()
        self._reverse = {(k.rsplit("_",1)[0][1:], v.casefold()): k for k,v in self.values.items()}
        self._counts = {}
        for k in self.values:
            kind=k.rsplit("_",1)[0][1:]
            self._counts[kind]=self._counts.get(kind,0)+1
        self._pattern = None
        self._lookup = {}
        self.ps = Pseudonymizer()
        ner = state.get("ner", {})
        self.ps.mapping = dict(ner.get("mapping", {}))
        self.ps._keys = {tuple(key): value for key, value in ner.get("keys", [])}
        self.ps._count = dict(ner.get("count", {}))

    @contextmanager
    def document(self, title, force=False):
        protected = force or bool(DOCUMENT_TITLE.search(str(title)))
        self._document_depth += int(protected)
        try:
            yield
        finally:
            self._document_depth -= int(protected)

    def register(self, kind, value):
        value = str(value or "").strip()
        if not value:
            return ""
        if LABEL.fullmatch(value):
            return value
        if self.documents_only and not self._document_depth:
            return self.redact_secrets(value)
        existing = self._reverse.get((kind,value.casefold()))
        if existing:
            return existing
        self._counts[kind] = self._counts.get(kind,0)+1
        label = f"[{kind}_{self._counts[kind]}]"
        self.values[label] = value
        self._reverse[(kind,value.casefold())] = label
        self._pattern = None
        return label

    def alias(self, label, value):
        if value:
            self.aliases[str(value).casefold()] = label
            self._pattern = None

    def add_secret(self, value):
        if isinstance(value, str) and value:
            self.secret_values.add(value)

    def config_secrets(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"password|passwd|secret|token|webhook|api_key", key, re.I):
                    self.add_secret(item)
                else:
                    self.config_secrets(item)
        elif isinstance(value, list):
            for item in value:
                self.config_secrets(item)

    def redact_secrets(self, text):
        text = str(text or "")
        for secret in sorted(self.secret_values, key=len, reverse=True):
            text = text.replace(secret, "[СКРЫТО]")
        text = re.sub(r"https?://[^\s\"<>]+/rest/\d+/[^\s\"<>]+", "[СКРЫТО]", text, flags=re.I)
        text = re.sub(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|Bearer\s+\S+)", "[СКРЫТО]", text)
        text = re.sub(r"(?i)((?:пароль|password|api_key|token)\s*[:=]\s*)\S+", r"\1[СКРЫТО]", text)
        return text

    def mask(self, text):
        text = self.redact_secrets(str(text or ""))
        if self.documents_only and not self._document_depth:
            # A pasted document may follow an introductory sentence.
            for line in text.splitlines():
                heading = re.sub(r"^[\s#>*_`•\d.)-]+", "", line)
                if DOCUMENT_TITLE.match(heading):
                    with self.document(heading):
                        return self.mask(text)
            return text
        literals = {}
        def keep_literal(value):
            key = "[РАЗРЕШЕННОЕЗНАЧЕНИЕ_" + str(len(literals) + 1) + "]"
            literals[key] = value
            return key
        text = re.sub(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}[.-]\d{2}[.-]\d{4})\b", lambda m: keep_literal(m[0]), text)
        amount = r"[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?"
        text = re.sub(r"(?i)(?<!\w)" + amount + r"(?=\s*(?:руб(?:лей|ля|ль|\.)?\b|₽|USD\b|EUR\b|доллар[а-яё]*\b|евро\b))", lambda m: keep_literal(m[0]), text)
        text = re.sub(r"(?i)(\b(?:сумма|стоимость|цена)\s*[:=]?\s*)(" + amount + r")(?![\d.,])", lambda m: m[1] + keep_literal(m[2]), text)
        digit_separator = r"[ \t\u00a0\u202f\u2009‐‑–—-]*"
        # Passport series and number can appear as a pair without the word passport.
        passport_pair = (r"(?i)(\bсерия\s*[:№=-]?\s*)(\d(?:" + digit_separator + r"\d){3})"
                         r"([\s,;]*(?:и\s+)?номер\s*[:№=-]?\s*)(\d(?:" + digit_separator + r"\d){5})"
                         r"(?!" + digit_separator + r"\d)")
        text = re.sub(passport_pair, lambda m: m[1] + self.register("РЕКВИЗИТ", m[2]) + m[3] + self.register("РЕКВИЗИТ", m[4]), text)
        bare_passport = r"(?<!\w)(\d{2}[ \t\u00a0\u202f]*\d{2}[\s‐‑–—-]+\d{6})(?!\w)"
        text = re.sub(bare_passport, lambda m: self.register("РЕКВИЗИТ", m[1]), text)
        for prefix, lengths in ((r"БИК|КПП", (9,)), (r"ИНН", (12,10)), (r"ОГРНИП", (15,)), (r"ОГРН", (13,)),
                                (r"р/с|к/с|расч[её]тный\s+сч[её]т|корреспондентский\s+сч[её]т", (20,)),
                                (r"СНИЛС", (11,)),
                                (r"(?:номер|серия)\s+паспорта|паспорт(?:а)?(?:\s+РФ)?", (10,6,4)),
                                (r"серия", (4,)), (r"номер", (6,)),
                                (r"водительск(?:ое|ого)\s+удостоверени(?:е|я)(?:\s+номер)?", (10,))):
            for length in lengths:
                pattern = r"(?i)(\b(?:" + prefix + r"),?\s*[\"']?\s*[:№=,;|\-]?\s*[\"']?\s*)(\d(?:" + digit_separator + r"\d){" + str(length-1) + r"})(?!" + digit_separator + r"\d)"
                text = re.sub(pattern,lambda m:m[1]+self.register("РЕКВИЗИТ",m[2]),text)
        if self.values:
            if self._pattern is None:
                self._lookup = {v.casefold(): k for k,v in self.values.items() if not v.isdigit() and not LABEL.fullmatch(v)}
                self._lookup.update({v:k for v,k in self.aliases.items() if not v.isdigit()})
                alternatives = "|".join(re.escape(v) for v in sorted(self._lookup,key=len,reverse=True))
                self._pattern = re.compile(r"(?<![\w])(?:" + alternatives + r")(?![\w])",re.I) if alternatives else re.compile(r"(?!)")
            text = self._pattern.sub(lambda match:self._lookup[match[0].casefold()],text)
        text = re.sub(r"https?://[^\s\"<>]+", lambda m: self.register("ССЫЛКА", m[0]), text, flags=re.I)
        def document(match):
            number = match[2]
            suffix = text[match.end():].lstrip().casefold()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}",number) or suffix.startswith(("руб", "₽", "доллар", "евро")):
                return match[0]
            return match[1] + self.register("ДОКУМЕНТ",number)
        text = re.sub(r"(?i)(\b(?:заказ(?:а|у|е|ом)?|договор(?:а|у|е|ом)?|сч[её]т(?:а|у|е|ом)?)\s*(?:№\s*)?)(\d[0-9А-Яа-яA-Za-z/_-]{2,})(?![\w,])",document,text)
        text = re.sub(r"\b\d{2}[А-Яа-яA-Za-z]{1,8}-\d{3,}\b", lambda m: self.register("ДОКУМЕНТ", m[0]), text)
        text = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", lambda m: self.register("ОБЪЕКТ", m[0]), text, flags=re.I)
        text = re.sub(r"\b(?:call|co|ld|d|l)\d+\b", lambda m: self.register("ОБЪЕКТ", m[0]), text)
        masked = "".join(part if LABEL.fullmatch(part) else self.ps.mask(part) for part in LABEL.split(text) if part)
        known={v.casefold():k for k,v in self.values.items()}
        known.update(self.aliases)
        for label,original in self.ps.mapping.items():
            canonical=known.get(original.casefold())
            if canonical:
                masked=masked.replace(label,canonical)
        for key, value in literals.items():
            masked = masked.replace(key, value)
        return masked

    def sanitize(self, value):
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items() if key not in ("url", "pii", "links", "file", "record_url")}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item) for item in value]
        return self.mask(value) if isinstance(value, str) else value

    def restore(self, text):
        return restore(text, {**self.ps.mapping, **self.values})

    def source(self, key, title, url=None, system="Дашборд"):
        for sid, row in self.sources.items():
            if row["key"] == key:
                return sid
        sid = "source_" + str(len(self.sources) + 1)
        self.sources[sid] = {"key": key, "title": title, "url": url, "system": system}
        return sid

    def dump(self):
        return {"values": self.values, "sources": self.sources, "aliases": self.aliases,
                "ner": {"mapping": self.ps.mapping, "keys": [[list(k), v] for k, v in self.ps._keys.items()], "count": self.ps._count}}
