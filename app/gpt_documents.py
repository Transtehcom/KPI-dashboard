"""Chat-scoped document reading and bounded, format-preserving edits."""
import base64
import csv
import datetime as dt
import io
import math
import re
import uuid
import zipfile
from pathlib import PurePath

from .common import db_lock
from . import gpt_images
from .gpt_privacy import DOCUMENT_TITLE

TOOLS = ["document_list", "document_read", "document_edit"]
MAX_BYTES = 3 * 1024 * 1024
FORMATS = {".docx", ".xlsx", ".txt", ".md", ".csv", ".pdf"}


def extension(name):
    return PurePath(name).suffix.lower()


def field_kind(text):
    text = str(text or "")
    if re.search(r"парол|password|passwd|секрет|secret|token|api.?key|ключ.*доступ", text, re.I):
        return "secret"
    if re.fullmatch(r"\s*(?:серия|номер)(?:\s+паспорта)?\s*[:№=]?\s*", text, re.I):
        return "РЕКВИЗИТ"
    if re.search(r"\b(?:инн|кпп|бик|снилс|огрн|р/с|к/с)\b|расч[её]тный сч[её]т|(?:номер|серия) паспорта|телефон|почт|email|адрес", text, re.I):
        return "РЕКВИЗИТ"
    if re.search(r"\bфио\b|фамили|отчеств|имя сотрудника", text, re.I):
        return "ИМЯ"
    if re.search(r"контрагент|компания|организац|поставщик|покупател", text, re.I):
        return "КОМПАНИЯ"
    if re.search(r"сумм|стоим|цена", text, re.I):
        return "amount"
    return ""


def office_archive(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1500 or sum(e.file_size for e in entries) > 30 * 1024 * 1024:
                raise ValueError("Распакованный документ слишком велик")
            if any(e.flag_bits & 1 for e in entries):
                raise ValueError("Зашифрованные документы не поддерживаются")
    except zipfile.BadZipFile:
        raise ValueError("Файл не является корректным DOCX/XLSX") from None


def paragraphs(document):
    from docx.oxml.ns import qn
    parts = [document.part]
    parts += [p for p in document.part.package.parts if str(p.partname).startswith(("/word/header", "/word/footer"))]
    out = []
    for part in parts:
        for paragraph in part.element.iter(qn("w:p")):
            nodes = [node for node in paragraph.iter(qn("w:t"))
                     if next(node.iterancestors(qn("w:p")), None) is paragraph]
            out.append(nodes)
    return out


def load(name, raw):
    """Return editable object and stable text units. PDF is read-only."""
    suffix = extension(name)
    try:
        if suffix == ".docx":
            from docx import Document
            office_archive(raw)
            obj = Document(io.BytesIO(raw))
            units = [{"id": f"p:{i}", "text": "".join(n.text or "" for n in nodes)}
                     for i, nodes in enumerate(paragraphs(obj), 1)]
        elif suffix == ".xlsx":
            from openpyxl import load_workbook
            office_archive(raw)
            obj = load_workbook(io.BytesIO(raw))
            units = []
            for sheet in obj:
                if sheet.max_row > 10000 or sheet.max_column > 200:
                    raise ValueError("Лист превышает лимит 10 000 строк или 200 столбцов")
                column_kinds = {}
                for cell in sorted(sheet._cells.values(), key=lambda c: (c.row, c.column)):
                    if cell.value is not None:
                        own_kind = field_kind(cell.value) if isinstance(cell.value, str) else ""
                        left = sheet._cells.get((cell.row, cell.column - 1))
                        semantic = field_kind(left.value) if left else ""
                        semantic = semantic or column_kinds.get(cell.column, "")
                        if own_kind:
                            column_kinds[cell.column] = own_kind
                            semantic = "secret" if own_kind == "secret" else ""
                        units.append({"id": sheet.title + "!" + cell.coordinate, "text": str(cell.value),
                                      "semantic": semantic, "numeric": isinstance(cell.value, (int, float))})
        elif suffix == ".pdf":
            from pypdf import PdfReader
            obj = PdfReader(io.BytesIO(raw))
            if obj.is_encrypted or len(obj.pages) > 100:
                raise ValueError("Поддерживается PDF без пароля, до 100 страниц")
            units = []
            for i, page in enumerate(obj.pages, 1):
                text = page.extract_text() or ""
                for start in range(0, len(text), 4000):
                    units.append({"id": f"page:{i}:{start // 4000 + 1}", "text": text[start:start + 4000]})
            if not units:
                raise ValueError("В PDF нет текстового слоя. Для скана потребуется распознавание текста")
        else:
            try:
                text = raw.decode("utf-8-sig")
                encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            except UnicodeDecodeError:
                text, encoding = raw.decode("cp1251"), "cp1251"
            if "\x00" in text:
                raise ValueError("Файл не является текстовым")
            obj = (text.splitlines(keepends=True) or [""], encoding)
            units = [{"id": f"line:{i}", "text": line.rstrip("\r\n")} for i, line in enumerate(obj[0], 1)]
            if suffix == ".csv":
                try:
                    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
                records, units, column_kinds, start = [], [], {}, 0
                for index, cells in enumerate(reader, 1):
                    records.append("".join(obj[0][start:reader.line_num]))
                    start = reader.line_num
                    semantics = []
                    for column, cell in enumerate(cells):
                        own_kind = field_kind(cell)
                        semantic = field_kind(cells[column - 1]) if column else ""
                        semantic = semantic or column_kinds.get(column, "")
                        if own_kind:
                            column_kinds[column] = own_kind
                            semantic = "secret" if own_kind == "secret" else ""
                        semantics.append(semantic)
                    units.append({"id": f"line:{index}", "text": records[-1].rstrip("\r\n"),
                                  "cells": cells, "semantics": semantics, "dialect": dialect})
                obj = (records or [""], encoding)
        if len(units) > 8000 or sum(len(u["text"]) for u in units) > 250000:
            raise ValueError("Документ превышает лимит 8 000 блоков или 250 000 символов")
        if any(len(u["text"]) > 20000 for u in units):
            raise ValueError("Один текстовый блок превышает 20 000 символов")
        return obj, units
    except (ImportError, ModuleNotFoundError):
        raise ValueError("На сервере отсутствует библиотека обработки этого формата") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("Не удалось прочитать документ. Проверьте формат и отсутствие пароля") from None


def replace_text(current, old, new):
    if not isinstance(old, str) or not isinstance(new, str) or len(new) > 20000:
        raise ValueError("Для текстовой правки нужны old и new длиной до 20 000 символов")
    if not old:
        if current:
            raise ValueError("Пустой old допустим только для пустого блока")
        return new
    if current.count(old) != 1:
        raise ValueError("Старый фрагмент должен встречаться в блоке ровно один раз. Сначала перечитайте документ")
    return current.replace(old, new, 1)


def edit(name, raw, changes):
    suffix = extension(name)
    if suffix == ".pdf":
        raise ValueError("PDF доступен только для чтения. Для правок загрузите DOCX или текст")
    obj, units = load(name, raw)
    known = {u["id"]: u["text"] for u in units}
    original_paragraphs = paragraphs(obj) if suffix == ".docx" else []
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("unit"), str):
            raise ValueError("Укажите unit, old и new для каждой правки")
        unit, old, new = change["unit"], change.get("old"), change.get("new")
        if unit == "append" and suffix != ".xlsx":
            if old != "" or not isinstance(new, str) or not 1 <= len(new) <= 20000:
                raise ValueError("Для добавления текста передайте unit=append, old пустой и new до 20 000 символов")
            if suffix == ".docx":
                for line in new.splitlines():
                    obj.add_paragraph(line)
            else:
                ending = "\r\n" if any(line.endswith("\r\n") for line in obj[0]) else "\n"
                if obj[0][-1] and not obj[0][-1].endswith(("\r", "\n")):
                    obj[0][-1] += ending
                obj[0].append(new)
            continue
        if suffix == ".xlsx":
            match = re.fullmatch(r"(.+)!([A-Z]{1,3}[1-9]\d{0,3})", unit)
            if not match or match[1] not in obj.sheetnames:
                raise ValueError("Укажите существующий лист и ячейку, например План!A1")
            cell = obj[match[1]][match[2]]
            if cell.column > 200 or str(cell.value if cell.value is not None else "") != str(old):
                raise ValueError("Ячейка или её старое значение не совпадает. Перечитайте документ")
            if not isinstance(new, (str, int, float, bool, type(None))) or (isinstance(new, float) and not math.isfinite(new)):
                raise ValueError("Новое значение ячейки должно быть текстом, числом, булевым значением или null")
            if isinstance(new, str) and len(new) > 20000:
                raise ValueError("Значение ячейки слишком длинное")
            if isinstance(cell.value, (dt.datetime, dt.date)) and isinstance(new, str):
                try:
                    parsed = dt.datetime.fromisoformat(new)
                except ValueError:
                    raise ValueError("Для даты ячейки используйте YYYY-MM-DD или ISO дату со временем") from None
                if parsed.tzinfo is not None:
                    raise ValueError("Дата Excel должна быть без часового пояса")
                new = parsed if isinstance(cell.value, dt.datetime) else parsed.date()
            cell.value = new
            continue
        if unit not in known:
            raise ValueError("Неизвестный блок документа. Сначала прочитайте document_read")
        current = known[unit]
        if suffix == ".csv" and isinstance(old, str) and old and current.count(old) != 1:
            dialect = units[int(unit.split(":")[1]) - 1]["dialect"]
            try:
                expected = list(csv.reader(io.StringIO(old), dialect, strict=True))
                actual = list(csv.reader(io.StringIO(current), dialect, strict=True))
            except csv.Error:
                raise ValueError("Старый фрагмент CSV некорректен. Перечитайте документ") from None
            if len(expected) == 1 and expected == actual:
                old = current
        replacement = replace_text(current, old, new)
        if suffix == ".docx":
            if "\n" in new or "\r" in new:
                raise ValueError("В DOCX редактируйте каждый абзац отдельно без переноса строки в new")
            nodes = original_paragraphs[int(unit.split(":")[1]) - 1]
            if not nodes:
                raise ValueError("Добавление текста в пустой абзац DOCX пока не поддерживается")
            start = current.index(old) if old else 0
            end, offset, inserted = start + len(old), 0, False
            for node in nodes:
                text = node.text or ""
                left, right = offset, offset + len(text)
                if right > start and left < end or not old and not inserted:
                    before = text[:max(0, start - left)]
                    after = text[max(0, end - left):]
                    node.text = before + (new if not inserted else "") + after
                    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    inserted = True
                offset = right
        else:
            index = int(unit.split(":")[1]) - 1
            ending = obj[0][index][len(obj[0][index].rstrip("\r\n")):]
            obj[0][index] = replacement + ending
        known[unit] = replacement
    if suffix in (".docx", ".xlsx"):
        stream = io.BytesIO()
        obj.save(stream)
        return stream.getvalue()
    try:
        return "".join(obj[0]).encode(obj[1])
    except UnicodeEncodeError:
        raise ValueError("Новый текст содержит символы, отсутствующие в кодировке исходного файла") from None


class Documents:
    def __init__(self, con):
        self.con = con

    def listing(self, chat):
        with db_lock():
            rows = self.con.execute("SELECT d.id,d.name,d.created,d.parent_id,d.unit_count,length(d.content) AS size,i.mime,i.width,i.height,i.protected FROM gpt_documents d LEFT JOIN gpt_image_metadata i ON i.document_id=d.id WHERE d.chat_id=? ORDER BY d.created,d.rowid", (chat,)).fetchall()
        return [{**dict(r), "is_image": bool(r["mime"]), "protected": bool(r["protected"] or DOCUMENT_TITLE.search(r["name"])),
                 "protection_forced": bool(DOCUMENT_TITLE.search(r["name"])),
                 "editable": not r["mime"] and extension(r["name"]) != ".pdf"} for r in rows]

    def history(self, chat):
        with db_lock():
            return [dict(r) for r in self.con.execute("SELECT id,name,created,parent_id,size,deleted FROM gpt_document_deletions WHERE chat_id=? ORDER BY deleted DESC", (chat,))]

    def archive_deleted(self, chat, identities):
        marks = ",".join("?" for _ in identities)
        self.con.execute(f"INSERT OR REPLACE INTO gpt_document_deletions SELECT id,chat_id,name,created,parent_id,length(content),? FROM gpt_documents WHERE chat_id=? AND id IN ({marks})", (dt.datetime.now().isoformat(timespec="microseconds"), chat, *identities))

    def get(self, chat, identity):
        with db_lock():
            row = self.con.execute("SELECT * FROM gpt_documents WHERE chat_id=? AND id=?", (chat, identity)).fetchone()
        if row is None:
            raise ValueError("Документ не найден в этом чате")
        return dict(row)

    def save(self, chat, name, raw, parent=None, protected=False, guard=None):
        image = None
        if extension(name) in gpt_images.FORMATS:
            raw, image = gpt_images.normalize(raw)
            units = []
        else:
            if not raw or len(raw) > MAX_BYTES:
                raise ValueError("Размер документа должен быть от 1 байта до 3 МБ")
            _obj, units = load(name, raw)
        identity, created = uuid.uuid4().hex, dt.datetime.now().isoformat(timespec="microseconds")
        with db_lock():
            if guard:
                guard()
            if not self.con.execute("SELECT 1 FROM gpt_chats WHERE id=?", (chat,)).fetchone():
                raise ValueError("Чат не найден")
            count, size = self.con.execute("SELECT count(*),coalesce(sum(length(content)),0) FROM gpt_documents WHERE chat_id=?", (chat,)).fetchone()
            if count >= 30 or size + len(raw) > 30 * 1024 * 1024:
                raise ValueError("В чате достигнут лимит 30 документов/версий или 30 МБ. Создайте новый чат")
            self.con.execute("INSERT INTO gpt_documents VALUES (?,?,?,?,?,?,?)", (identity, chat, name, raw, created, parent, len(units)))
            if image:
                protected = bool(protected or DOCUMENT_TITLE.search(name))
                self.con.execute("INSERT INTO gpt_image_metadata(document_id,mime,width,height,protected) VALUES (?,?,?,?,?)",
                                 (identity, image["mime"], image["width"], image["height"], int(protected)))
            self.con.commit()
        return {"id": identity, "name": name, "created": created, "parent_id": parent, "unit_count": len(units), "size": len(raw),
                "is_image": bool(image), "protected": bool(protected), "protection_forced": bool(DOCUMENT_TITLE.search(name)),
                "editable": not image and extension(name) != ".pdf", **(image or {})}

    def upload(self, chat, name, encoded, protected=False):
        if type(protected) is not bool:
            raise ValueError("Признак «Содержит документ» должен быть true или false")
        if not isinstance(name, str) or not name.strip() or len(name) > 180 or re.search(r'[\\/\x00-\x1f]', name):
            raise ValueError("Укажите имя файла без пути, до 180 символов")
        name = name.strip()
        if extension(name) not in FORMATS | gpt_images.FORMATS:
            raise ValueError("Поддерживаются DOCX, XLSX, TXT, MD, CSV, PDF, PNG, JPEG и WebP")
        limit = gpt_images.MAX_BYTES if extension(name) in gpt_images.FORMATS else MAX_BYTES
        if not isinstance(encoded, str) or len(encoded) > 4 * limit // 3 + 4:
            raise ValueError("Файл превышает 10 МБ для изображения или 3 МБ для документа")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("Некорректные данные файла") from None
        return self.save(chat, name, raw, protected=protected)

    def set_protection(self, chat, identity, protected):
        if type(protected) is not bool:
            raise ValueError("Признак «Содержит документ» должен быть true или false")
        with db_lock():
            row = self.get(chat, identity)
            protected = bool(protected or DOCUMENT_TITLE.search(row["name"]))
            result = self.con.execute("UPDATE gpt_image_metadata SET protected=? WHERE document_id=?", (int(protected), identity))
            if not result.rowcount:
                raise ValueError("Вложение не является изображением")
            self.con.commit()
        return {"id": identity, "protected": protected}

    def image_read(self, chat, identity, privacy):
        row = self.get(chat, identity)
        with db_lock():
            image = self.con.execute("SELECT * FROM gpt_image_metadata WHERE document_id=?", (identity,)).fetchone()
        if image is None:
            raise ValueError("Вложение не является изображением")
        protected = bool(image["protected"] or DOCUMENT_TITLE.search(row["name"]))
        with privacy.document(row["name"], force=protected):
            privacy.source("document:" + row["id"], row["name"], None, "Изображение чата")
            if protected:
                text = privacy.mask(gpt_images.local_ocr(row["content"]))
                masked, pending = [], ""
                for line in text.splitlines():
                    own_kind = field_kind(line)
                    if own_kind == "secret" or pending == "secret":
                        masked.append("[СКРЫТО]")
                    elif pending in ("РЕКВИЗИТ", "ИМЯ", "КОМПАНИЯ"):
                        masked.append(privacy.register(pending, line))
                    else:
                        masked.append(privacy.mask(line))
                    pending = own_kind if len(line) < 90 and not re.search(r"\d", line) else ""
                return [{"type": "inputText", "text": "Локально распознанный документ\n" + "\n".join(masked)}]
            raw, metadata = gpt_images.normalize(row["content"])
            return [{"type": "inputImage", "imageUrl": "data:" + metadata["mime"] + ";base64," + base64.b64encode(raw).decode("ascii")}]

    def delete(self, chat, identity):
        with db_lock():
            self.archive_deleted(chat, [identity])
            result = self.con.execute("DELETE FROM gpt_documents WHERE chat_id=? AND id=?", (chat, identity))
            if not result.rowcount:
                raise ValueError("Документ не найден")
            self.con.execute("DELETE FROM gpt_image_metadata WHERE document_id=?", (identity,))
            self.con.commit()

    def download(self, chat, identity):
        row = self.get(chat, identity)
        return row["name"], row["content"]

    def tool(self, tool, chat, query, privacy, guard=None):
        if tool == "document_list":
            rows = self.listing(chat)
            out = []
            for row in rows:
                with privacy.document(row["name"], force=row.get("protected", False)):
                    out.append({**row, "name": privacy.register("ДОКУМЕНТ", row["name"]), "format": extension(row["name"])})
            return {"documents": out}
        if not isinstance(query, dict):
            raise ValueError("Параметры документа должны быть JSON-объектом")
        row = self.get(chat, str(query.get("id") or ""))
        if extension(row["name"]) in gpt_images.FORMATS:
            if tool == "document_read":
                return {"id": row["id"], "is_image": True, "tool": "image_read", "note": "Для чтения изображения вызовите image_read с этим id"}
            raise ValueError("Правка изображений недоступна")
        with privacy.document(row["name"]):
            if tool == "document_read":
                obj, units = load(row["name"], row["content"])
                if extension(row["name"]) == ".xlsx":
                    for sheet in obj:
                        privacy.register("ЛИСТ", sheet.title)
                masked_units, pending = [], ""
                for unit in units:
                    if "cells" in unit:
                        cells = []
                        for value, semantic in zip(unit["cells"], unit["semantics"]):
                            if semantic == "secret":
                                cells.append("[СКРЫТО]")
                            elif semantic in ("РЕКВИЗИТ", "ИМЯ", "КОМПАНИЯ"):
                                cells.append(privacy.register(semantic, value))
                            elif semantic == "amount" and re.fullmatch(r"[+-]?[\d .,]+", value):
                                cells.append(value)
                            else:
                                cells.append(privacy.mask(value))
                        stream = io.StringIO(newline="")
                        csv.writer(stream, unit["dialect"]).writerow(cells)
                        masked_units.append({"id": privacy.register("БЛОК", unit["id"]), "text": stream.getvalue().rstrip("\r\n")})
                        continue
                    semantic = unit.get("semantic", "")
                    if extension(row["name"]) != ".xlsx":
                        own_kind = field_kind(unit["text"])
                        semantic = "secret" if own_kind == "secret" else pending
                        pending = own_kind if len(unit["text"]) < 90 and not re.search(r"\d", unit["text"]) else ""
                    if semantic == "secret":
                        text = "[СКРЫТО]"
                    elif semantic in ("РЕКВИЗИТ", "ИМЯ", "КОМПАНИЯ"):
                        text = privacy.register(semantic, unit["text"])
                    elif semantic == "amount" and unit.get("numeric"):
                        text = unit["text"]
                    else:
                        text = privacy.mask(unit["text"])
                    masked_units.append({"id": privacy.register("БЛОК", unit["id"]), "text": text})
                offset = query.get("offset", 0)
                if type(offset) is not int or not 0 <= offset <= len(units):
                    raise ValueError("offset должен быть позицией в документе")
                selected, length = [], 0
                for unit in masked_units[offset:offset + 60]:
                    if selected and length + len(unit["text"]) > 20000:
                        break
                    selected.append(unit)
                    length += len(unit["text"])
                next_offset = offset + len(selected)
                sid = privacy.source("document:" + row["id"], row["name"], None, "Документ чата")
                return {"id": row["id"], "name": privacy.register("ДОКУМЕНТ", row["name"]), "units": selected,
                        "total_units": len(units), "next_offset": next_offset if next_offset < len(units) else None,
                        "editable": extension(row["name"]) != ".pdf", "source": sid}
            if tool != "document_edit":
                raise ValueError("Неизвестная операция с документом")
            changes = privacy.restore(query.get("changes"))
            if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
                raise ValueError("Передайте от 1 до 50 адресных правок changes")
            raw = edit(row["name"], row["content"], changes)
            name = PurePath(row["name"]).stem[:140] + " — правки" + extension(row["name"])
            result = self.save(chat, name, raw, row["id"], guard=guard)
            return {"document": {**result, "name": privacy.register("ДОКУМЕНТ", result["name"])}, "changes_applied": len(changes), "note": "Создана новая копия. Оригинал сохранён"}
