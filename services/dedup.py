"""Отпечаток расписания: одна и та же таблица из VK и с сайта даёт один отпечаток.

Сравнивается смысловое содержимое (дата-неделя + все занятия нормализованным
текстом), а не байты файла: docx школы и docx, восстановленный из PDF сайта,
различаются на диске, но несут одно расписание.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import docx

from services.parser import collect_groups, extract_header_stamp


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def schedule_fingerprint(path: str | Path) -> tuple[str, str]:
    """(отпечаток, 'ГГГГ-ММ-ДД') по содержимому файла.

    Отпечаток не включает дату: смысловая идентичность таблицы проверяется
    отдельно от того, на какой день она составлена.
    """
    d = docx.Document(str(path))
    groups = collect_groups(d)
    day, week = extract_header_stamp(d)
    rows: list[str] = []
    for group in sorted(groups):
        for les in groups[group]:
            rows.append("|".join((
                _norm(group), _norm(les.pair_label), _norm(les.subject),
                _norm(les.teacher), _norm(les.room),
            )))
    canonical = f"week={week}\n" + "\n".join(rows)
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return fp, day.isoformat()
