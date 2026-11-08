"""Парсер файлов расписания (.docx) — любая схема таблиц и текст без таблиц."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.table import Table

from .models import DaySchedule, Lesson

NO_LESSON = {"нет урока", "нет", "—", "-", ""}

_PAIR_RE = re.compile(r"(\d+)\s*[-–—]?\s*(\d+)?\s*п\.?", re.IGNORECASE)
_PAIR_BARE_RE = re.compile(r"^(\d{1,2})\s*[-–—]\s*(\d{1,2})$|^(\d{1,2})$")
_NO_LETTERS_RE = re.compile(r"[a-zа-яё]", re.IGNORECASE)
_DATE_IN_NAME_RE = re.compile(r"(\d{1,2})[.\-_ ](\d{1,2})(?:[.\-_ ](\d{2,4}))?")
_DAY_IN_NAME_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")

_GROUP_RE = re.compile(r"^\d{2,4}-[А-ЯЁ]{2,5}(?:-\d{1,2}Д?)?$", re.IGNORECASE)
_GROUP_TOKEN_RE = re.compile(r"\d{2,4}-[А-ЯЁ]{2,5}(?:-\d{1,2}Д?)?(?=\s|$)", re.IGNORECASE)
_PAIR_CELL_RE = re.compile(r"^\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s*п\.?$", re.IGNORECASE)
_PAIR_NUM_RE = re.compile(r"^\d{1,2}(?:[-–—]\d{1,2})?$")
_TEACHER_RE = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.[А-ЯЁ]\.")
_ROOM_RE = re.compile(
    r"(?i)^(?:\S*\d+[а-я]?-\d+\S*|дист\.?|сам\.?\s*раб\.?|спортзал\S*)$"
)

# роль -> синонимы шапки (нормализованные: lower, без пробелов и точек)
_HEADER_SYNONYMS = {
    "group": {"группа", "гр", "групп", "группы"},
    "pair": {"пара", "пары", "номер", "номерпары", "№", "№пары", "номерурока", "урок"},
    "subject": {"дисциплина", "предмет", "занятие", "дисц", "наименование", "урок", "название"},
    "teacher": {"преподаватель", "препод", "преп", "учитель", "фио", "педагог"},
    "room": {"аудитория", "ауд", "кабинет", "каб", "аудит"},
}
_ROLES = ("group", "pair", "subject", "teacher", "room")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _clean_subject(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"\s*[—–-]\s*$", "", text)
    return text.strip()


def normalize_group(name: str) -> str:
    name = _clean(name).upper()
    name = re.sub(r"[–—−]", "-", name)
    return name


def parse_pair_label(label: str) -> tuple[int | None, int | None]:
    """'1 п.' -> (1, 1); '3-4 п.' -> (3, 4); '2' -> (2, 2); мусор -> (None, None)."""
    m = _PAIR_RE.search(label or "")
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        return start, end
    s = (label or "").strip()
    m = _PAIR_BARE_RE.match(s)
    if m:
        if m.group(1):
            return int(m.group(1)), int(m.group(2))
        return int(m.group(3)), int(m.group(3))
    # последний шанс: голая цифра без букв («№2»)
    if s and not _NO_LETTERS_RE.search(s):
        m = re.search(r"\d+", s)
        if m:
            v = int(m.group())
            return v, v
    return None, None


def extract_date_from_filename(filename: str, reference: date | None = None) -> date:
    """Дата из имени файла (напр. '09.docx' = день 9). Фолбэк — завтра.

    Из нескольких кандидатов (этот/следующий/предыдущий месяц) берётся ближайшая к завтрашнему дню.
    """
    reference = reference or date.today()
    target = reference + timedelta(days=1)
    stem = Path(filename).stem

    m = _DATE_IN_NAME_RE.search(stem)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            year += 2000 if year < 100 else 0
            try:
                return date(year, month, day)
            except ValueError:
                pass
        candidates = []
        for month_shift in (-1, 0, 1):
            y, mo = reference.year, reference.month + month_shift
            if mo > 12:
                y, mo = y + 1, mo - 12
            elif mo < 1:
                y, mo = y - 1, mo + 12
            try:
                candidates.append(date(y, mo, day))
            except ValueError:
                continue
        if candidates:
            return min(candidates, key=lambda d: abs((d - target).days))

    days = _DAY_IN_NAME_RE.findall(stem)
    if days:
        # последнее число в имени = день («расписание_Чт_10нед_05.docx» -> 5)
        day = int(days[-1])
        if 1 <= day <= 31:
            candidates = []
            for month_shift in (-1, 0, 1):
                y, mo = reference.year, reference.month + month_shift
                if mo > 12:
                    y, mo = y + 1, mo - 12
                elif mo < 1:
                    y, mo = y - 1, mo + 12
                try:
                    candidates.append(date(y, mo, day))
                except ValueError:
                    continue
            if candidates:
                return min(candidates, key=lambda d: abs((d - target).days))

    return target


# ---------- схема колонок ----------

def _norm_header(text: str) -> str:
    return re.sub(r"[\s.]+", "", (text or "").lower().replace("ё", "е"))


def _header_match(text: str) -> tuple[str, int] | None:
    """(роль, качество) для ячейки шапки; качество = длина сопоставленного синонима."""
    t = _norm_header(text)
    if not t:
        return None
    best: tuple[str, int] | None = None
    for role, syns in _HEADER_SYNONYMS.items():
        for s in syns:
            s = s.replace("ё", "е")
            if t == s or (len(min(t, s, key=len)) >= 3 and (t.startswith(s) or s.startswith(t))):
                if best is None or len(s) > best[1]:
                    best = (role, len(s))
    return best


def _map_columns(cells: list[str]) -> dict[str, int] | None:
    """Маппинг роль -> индекс по шапке; None, если строка не похожа на шапку.

    При двух колонках на одну роль («№» и «Пара») побеждает более точный синоним.
    """
    best: dict[str, tuple[int, int]] = {}  # роль -> (качество, индекс)
    for i, c in enumerate(cells):
        m = _header_match(c)
        if not m:
            continue
        role, quality = m
        if role not in best or quality > best[role][0]:
            best[role] = (quality, i)
    mapping = {role: idx for role, (_, idx) in best.items()}
    return mapping if len(mapping) >= 3 else None


def _classify_columns(rows: list[list[str]]) -> dict[str, int]:
    """Эвристика по содержимому, когда шапки нет. Роль -> индекс (-1 = нет)."""
    ncols = max((len(r) for r in rows), default=0)
    samples: list[list[str]] = [[] for _ in range(ncols)]
    for r in rows[:30]:
        for i, c in enumerate(r):
            if c:
                samples[i].append(c)

    def score(i: int, role: str) -> float:
        vals = samples[i] or [""]
        if role == "group":
            hits = sum(1 for v in vals if _GROUP_RE.match(v))
        elif role == "pair":
            strong = sum(1 for v in vals if _PAIR_CELL_RE.match(v))
            weak = sum(1 for v in vals if _PAIR_NUM_RE.match(v) and
                       all(int(x) <= 15 for x in re.findall(r"\d+", v)))
            hits = strong + 0.5 * weak
        elif role == "teacher":
            hits = sum(1 for v in vals if _TEACHER_RE.match(v))
        elif role == "room":
            hits = sum(1 for v in vals if _ROOM_RE.match(v))
        else:  # subject: колонка с самым длинным текстом
            avg = sum(len(v) for v in vals) / len(vals)
            return avg / 50.0
        return hits / len(vals)

    mapping: dict[str, int] = {}
    used: set[int] = set()
    for role in ("group", "teacher", "pair", "room", "subject"):
        best, best_score = -1, 0.0
        for i in range(ncols):
            if i in used:
                continue
            s = score(i, role)
            if s > best_score:
                best, best_score = i, s
        if best >= 0 and (role == "subject" or best_score > 0):
            mapping[role] = best
            used.add(best)
    return {role: mapping.get(role, -1) for role in _ROLES}


def _row_fields(cells: list[str], mapping: dict[str, int]) -> dict[str, str]:
    def field(role: str) -> str:
        i = mapping.get(role, -1)
        return cells[i] if 0 <= i < len(cells) else ""
    return {role: field(role) for role in _ROLES}


# ---------- сбор занятий из строк ----------

def _apply_fields(f: dict[str, str], groups: dict[str, list[Lesson]], current: str | None) -> str | None:
    col_group = _clean(f.get("group", ""))
    col_pair = _clean(f.get("pair", ""))
    col_subject = _clean_subject(f.get("subject", ""))
    col_teacher = _clean(f.get("teacher", ""))
    col_room = _clean(f.get("room", ""))

    if col_group and not (_PAIR_CELL_RE.match(col_group) or _PAIR_NUM_RE.match(col_group)):
        current = normalize_group(col_group)
        groups.setdefault(current, [])
    if current is None:
        return None

    subject_l = col_subject.lower()
    pair_l = col_pair.lower()
    teacher = "" if col_teacher in {"—", "-", "–"} else col_teacher
    room = "" if col_room in {"—", "-", "–"} else col_room

    # «Разговоры о важном» — в любой из колонок, без номера пары
    if subject_l.startswith("разговоры") or pair_l.startswith("разговоры"):
        label = col_pair or col_subject
        groups[current].append(
            Lesson(pair_label=label, subject=col_subject or col_pair, teacher=teacher, room=room)
        )
        return current

    start, end = parse_pair_label(col_pair)
    if start is None:
        # строка без распознанной пары — мусор (сдвинутые колонки и т.п.)
        return current

    if not col_subject or subject_l in NO_LESSON:
        groups[current].append(
            Lesson(pair_label=col_pair, subject="Нет урока",
                   pair_start=start, pair_end=end, is_free=True)
        )
        return current

    groups[current].append(
        Lesson(pair_label=col_pair, subject=col_subject,
               teacher=teacher, room=room, pair_start=start, pair_end=end)
    )
    return current


# ---------- источники строк: таблицы и текст ----------

def _all_tables(doc: docx.Document):
    """Все таблицы документа, включая вложенные и в текстовых рамках (без дублей)."""
    body = doc.element.body
    for tbl in body.iter(qn("w:tbl")):
        parent = tbl.getparent()
        nested = False
        while parent is not None and parent is not body:
            if parent.tag == qn("w:tbl"):
                nested = True
                break
            parent = parent.getparent()
        if not nested:
            yield Table(tbl, doc)


def _field_dicts_from_rows(rows: list[list[str]]):
    """Строки ячеек (любой ширины) -> словари ролей. Шапка ищется в любой строке."""
    rows = [r for r in rows if any(r)]
    if not rows:
        return
    mapping: dict[str, int] | None = None
    for r in rows:
        m = _map_columns(r)
        if m:
            mapping = m
            break
    if mapping is None:
        mapping = _classify_columns(rows)
    for r in rows:
        if _map_columns(r):  # повторяющиеся шапки — пропускаем
            continue
        yield _row_fields(r, mapping)


def _split_loose_line(text: str) -> dict[str, str] | None:
    """Одна строка без таблицы: 'ГРУППА 1 п. Предмет Фамилия И.О. аудитория'."""
    m = _GROUP_TOKEN_RE.match(text)
    if not m:
        return None
    fields = {"group": m.group(0), "pair": "", "subject": "", "teacher": "", "room": ""}
    rest = text[m.end():].strip()
    pm = re.match(r"(\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s*п\.?)\s+", rest, re.IGNORECASE)
    if pm:
        fields["pair"] = pm.group(1)
        rest = rest[pm.end():].strip()
    rm = re.search(r"(\S*(?:\d+[а-я]?-\d+\S*|дист\.?|сам\.?\s*раб\.?|спортзал\S*))\s*$",
                   rest, re.IGNORECASE)
    if rm:
        fields["room"] = rm.group(1)
        rest = rest[:rm.start()].strip()
    tm = _TEACHER_RE.search(rest)
    if tm:
        fields["teacher"] = tm.group(0)
        rest = rest[:tm.start()].strip()
    fields["subject"] = _clean_subject(rest)
    return fields


def _paragraph_field_dicts(doc: docx.Document):
    """Текстовый фолбэк: абзацы с табами/2+ пробелами или свободной строкой."""
    rows: list[list[str]] = []
    loose: list[dict[str, str]] = []
    for p in doc.paragraphs:
        text = (p.text or "").replace("\xa0", " ")
        if not text.strip():
            continue
        if "\t" in text:
            # не strip'ать целиком — иначе срезаются ведущие табы и сдвигается колонка
            cells = [_clean(c) for c in text.split("\t")]
            if len(cells) >= 3 and sum(bool(c) for c in cells) >= 2:
                rows.append(cells)
                continue
        text = text.strip()
        parts = [_clean(c) for c in re.split(r"\s{2,}", text) if _clean(c)]
        if len(parts) >= 3:
            rows.append(parts)
            continue
        f = _split_loose_line(re.sub(r"\s+", " ", text))
        if f:
            loose.append(f)
    yield from _field_dicts_from_rows(rows)
    yield from loose


def collect_groups(doc: docx.Document) -> dict[str, list[Lesson]]:
    """Разбирает документ: группа -> список занятий. Любая схема колонок."""
    groups: dict[str, list[Lesson]] = {}
    for table in _all_tables(doc):
        current = None
        rows = [[_clean(c.text) for c in row.cells] for row in table.rows]
        for fields in _field_dicts_from_rows(rows):
            current = _apply_fields(fields, groups, current)
    if not groups:
        current = None
        for fields in _paragraph_field_dicts(doc):
            current = _apply_fields(fields, groups, current)
    return groups


def parse_docx(path: str | Path, group: str, reference: date | None = None) -> DaySchedule:
    """Достаёт расписание одной группы из файла. Бросает ValueError, если группу не нашли."""
    path = Path(path)
    doc = docx.Document(str(path))
    groups = collect_groups(doc)
    key = normalize_group(group)
    lessons = groups.get(key)
    if lessons is None:
        known = ", ".join(sorted(groups)) or "нет групп в файле"
        raise ValueError(f"Группа «{group}» не найдена в файле {path.name}. Группы в файле: {known}")
    return DaySchedule(
        date=extract_date_from_filename(path.name, reference=reference),
        group=key,
        lessons=lessons,
        source_filename=path.name,
    )
