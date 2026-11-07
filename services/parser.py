"""Парсер файлов расписания (.docx с таблицами Группа|Пара|Дисциплина|Преподаватель|Аудитория)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import docx

from .models import DaySchedule, Lesson

HEADERS = {"группа", "пара", "дисциплина", "преподаватель", "аудитория"}
NO_LESSON = {"нет урока", "нет", "—", "-", ""}

_PAIR_RE = re.compile(r"(\d+)\s*[-–—]?\s*(\d+)?\s*п\.?", re.IGNORECASE)
_DATE_IN_NAME_RE = re.compile(r"(\d{1,2})[.\-_ ](\d{1,2})(?:[.\-_ ](\d{2,4}))?")
_DAY_IN_NAME_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")


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
    """'1 п.' -> (1, 1); '3-4 п.' -> (3, 4); пусто/мусор -> (None, None)."""
    m = _PAIR_RE.search(label or "")
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


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


def _iter_table_rows(table):
    for row in table.rows:
        cells = ([_clean(c.text) for c in row.cells] + [""] * 5)[:5]
        if not any(cells):
            continue
        if cells[0].lower() in HEADERS or cells[1].lower() == "пара":
            continue
        yield cells


def collect_groups(doc: docx.Document) -> dict[str, list[Lesson]]:
    """Разбирает все таблицы документа: группа -> список занятий."""
    groups: dict[str, list[Lesson]] = {}
    for table in doc.tables:
        current: str | None = None
        for cells in _iter_table_rows(table):
            col_group, col_pair, col_subject, col_teacher, col_room = (cells + [""] * 5)[:5]
            if col_group:
                current = normalize_group(col_group)
                groups.setdefault(current, [])
            if current is None:
                continue

            subject = _clean_subject(col_subject)
            subject_l = subject.lower()
            teacher = "" if col_teacher in {"—", "-", "–"} else _clean(col_teacher)
            room = "" if col_room in {"—", "-", "–"} else _clean(col_room)

            if not col_pair:
                # без номера пары — только «Разговоры о важном», остальное мусор
                if subject_l.startswith("разговоры"):
                    groups[current].append(
                        Lesson(pair_label=subject, subject=subject, teacher=teacher, room=room)
                    )
                continue

            start, end = parse_pair_label(col_pair)
            if not subject or subject_l in NO_LESSON:
                groups[current].append(
                    Lesson(pair_label=_clean(col_pair), subject="Нет урока",
                           pair_start=start, pair_end=end, is_free=True)
                )
                continue

            groups[current].append(
                Lesson(pair_label=_clean(col_pair), subject=subject,
                       teacher=teacher, room=room, pair_start=start, pair_end=end)
            )
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
