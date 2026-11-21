"""PDF «Изменения к основному расписанию» с сайта КНТ -> .docx с колонтитулом-штампом.

PDF — это таблица Word, выгруженная в PDF: строка данных = один блок группы
(слитая ячейка), текст разложен по 5 визуальным колонкам. Границы колонок —
из ячеек шапки, блоки групп — из bbox строк данных.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pdfplumber
from docx import Document
from docx.shared import Pt

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})")
WEEK_RE = re.compile(r"(\d+)\s*НЕДЕЛЯ", re.IGNORECASE)
WEEKDAY_RE = re.compile(
    r"(ПОНЕДЕЛЬНИК|ВТОРНИК|СРЕДА|ЧЕТВЕРГ|ПЯТНИЦА|СУББОТА|ВОСКРЕСЕНЬЕ)", re.IGNORECASE
)

GROUP_RE = re.compile(r"^\d{2,4}-[А-ЯЁ]{2,5}(?:-\d{1,3}Д?)?$", re.IGNORECASE)


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    """Слова -> визуальные строки по y (допуск 3pt)."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[words[0]]]
    for w in words[1:]:
        if abs(w["top"] - lines[-1][0]["top"]) <= 3.0:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def extract(pdf_path: str | Path) -> tuple[list[dict], dict]:
    """(группы с занятиями, штамп) из PDF. Группа -> [{'pair','subject','teacher','room'}]."""
    groups: list[dict] = []
    stamp: dict = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            if not tables:
                continue
            t = tables[0]
            x0t, top_t, x1t, bot_t = t.bbox

            above = [w for w in page.extract_words() if w["top"] < top_t - 2]
            for ln in _cluster_lines(above):
                text = " ".join(w["text"] for w in ln)
                m = DATE_RE.search(text)
                if m and "date" not in stamp:
                    try:
                        stamp["date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                    except ValueError:
                        pass
                m = WEEK_RE.search(text)
                if m and "week" not in stamp:
                    stamp["week"] = int(m.group(1))
                m = WEEKDAY_RE.search(text)
                if m and "weekday" not in stamp:
                    stamp["weekday"] = m.group(1).upper()

            rows = t.rows
            if not rows:
                continue
            header_cells = [c for c in rows[0].cells if c]
            if len(header_cells) != 5:
                log.warning("PDF: страница %s — %d ячеек в шапке вместо 5",
                            page.page_number, len(header_cells))
                continue
            bounds = [(c[0], c[2]) for c in header_cells]

            def col_of(x0: float) -> int:
                for i, (lo, hi) in enumerate(bounds):
                    if lo - 1.5 <= x0 < hi - 0.5:
                        return i
                best, bestd = 0, 1e9
                for i, (lo, hi) in enumerate(bounds):
                    d = abs(x0 - lo)
                    if d < bestd:
                        best, bestd = i, d
                return best

            all_words = page.extract_words()
            for row in rows[1:]:
                cell = row.cells[0]
                if not cell:
                    continue
                cx0, ctop, cx1, cbot = cell
                is_last = row is rows[-1]
                words = []
                for w in all_words:
                    cy = (w["top"] + w["bottom"]) / 2.0
                    if not (cx0 - 1.5 <= w["x0"] <= cx1 + 1.5):
                        continue
                    ok = ctop <= cy <= cbot if is_last else ctop <= cy < cbot
                    if ok:
                        words.append(w)
                if not words:
                    continue

                group_parts: list[str] = []
                lessons: list[dict] = []
                cur: dict | None = None
                for ln in _cluster_lines(words):
                    cols = ["", "", "", "", ""]
                    for w in ln:
                        cols[col_of(w["x0"])] += (" " if cols[col_of(w["x0"])] else "") + w["text"]
                    cols = [c.strip() for c in cols]
                    g, pair, subj, teach, room = cols

                    if g:
                        group_parts.append(g)
                    if pair:
                        cur = {"pair": pair, "subject": subj, "teacher": teach, "room": room}
                        lessons.append(cur)
                    elif cur is not None and (subj or teach or room):
                        if subj:
                            cur["subject"] = (cur["subject"] + " " + subj).strip()
                        if teach:
                            cur["teacher"] = (cur["teacher"] + " " + teach).strip()
                        if room:
                            cur["room"] = (cur["room"] + " " + room).strip()
                    elif subj or teach or room:
                        # занятие без номера пары («Уч.практика» на весь день)
                        cur = {"pair": "", "subject": subj, "teacher": teach, "room": room}
                        lessons.append(cur)

                group = "".join(group_parts).replace(" ", "")
                if not group:
                    # строка-продолжение разбитой строки таблицы — дописываем к предыдущей группе
                    if groups and lessons:
                        log.warning("PDF: страница %s — строка без имени группы, дописываем к %s (%d уроков)",
                                    page.page_number, groups[-1]["group"], len(lessons))
                        groups[-1]["lessons"].extend(lessons)
                    else:
                        log.warning("PDF: страница %s — строка без имени группы: %s",
                                    page.page_number, lessons)
                    continue
                if not GROUP_RE.match(group):
                    log.warning("PDF: страница %s — подозрительное имя группы %r (частей %s)",
                                page.page_number, group, group_parts)
                groups.append({"group": group, "lessons": lessons})

    return groups, stamp


def build_docx(groups: list[dict], stamp: dict, out_path: str | Path) -> None:
    """.docx, который parse_docx разбирает без изменений: штамп в колонтитуле + таблица 5 колонок."""
    doc = Document()
    hdr = doc.sections[0].header
    p = hdr.paragraphs[0]
    p.text = "РАСПИСАНИЕ ЗАНЯТИЙ"
    p2 = hdr.add_paragraph(f"{stamp.get('weekday', '')} {stamp['date']:%d.%m.%Y} г.".strip())
    p3 = hdr.add_paragraph(f"{stamp.get('week', '')} НЕДЕЛЯ".strip())
    for par in (p, p2, p3):
        for run in par.runs:
            run.font.size = Pt(11)

    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    hdr_cells = table.rows[0].cells
    for i, name in enumerate(["Группа", "Пара", "Дисциплина", "Преподаватель", "Аудитория"]):
        hdr_cells[i].text = name
        for par in hdr_cells[i].paragraphs:
            for run in par.runs:
                run.bold = True

    for g in groups:
        for i, les in enumerate(g["lessons"]):
            row = table.add_row().cells
            row[0].text = g["group"] if i == 0 else ""
            row[1].text = les["pair"]
            row[2].text = les["subject"]
            row[3].text = les["teacher"]
            row[4].text = les["room"]

    doc.save(str(out_path))


def pdf_to_docx(pdf_path: str | Path, out_path: str | Path) -> dict:
    """Конвертация целиком; возвращает штамп {'date','week','weekday'}."""
    groups, stamp = extract(pdf_path)
    if not groups:
        raise ValueError("в PDF не нашлось ни одной группы")
    if "date" not in stamp:
        raise ValueError("в PDF нет даты (колонтитул «ЧЧ.ММ.ГГГГ»)")
    build_docx(groups, stamp, out_path)
    log.info("PDF->docx: %d групп, %d занятий, %s", len(groups),
             sum(len(g["lessons"]) for g in groups), stamp.get("date"))
    return stamp
