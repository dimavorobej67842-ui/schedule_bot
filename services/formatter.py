"""Сборка текста сообщения для Telegram (HTML)."""

from __future__ import annotations

from datetime import date
from html import escape

from .bells import Bells, DayBells
from .models import DaySchedule, Lesson

_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]


def format_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} ({_WEEKDAYS[d.weekday()]})"


def _lesson_line(lesson: Lesson, bells: DayBells) -> str:
    if lesson.is_free:
        return f"▫️ {escape(lesson.pair_label)} — нет урока"

    time = bells.for_range(lesson.pair_start, lesson.pair_end)
    head = f"<b>{escape(lesson.subject)}</b>"
    if lesson.pair_start is not None:
        time_part = f" · {time}" if time else ""
        line = f"▫️ {escape(lesson.pair_label)}{time_part}\n{head}"
    else:
        talks = f" · {bells.talks.range}" if bells.talks else ""
        line = f"▫️ {head}{talks}"

    details = " · ".join(x for x in (escape(lesson.teacher) if lesson.teacher else "",
                                     escape(lesson.room) if lesson.room else "") if x)
    if details:
        line += f"\n{details}"
    if lesson.homework:
        line += f"\n📝 ДЗ: {escape(lesson.homework)}"
    return line


def format_schedule(schedule: DaySchedule, bells: Bells, heading: str = "Расписание на") -> str:
    day = bells.for_weekday(schedule.date.weekday())
    lines = [
        f"🗓 <b>{escape(heading)} {format_date(schedule.date)}</b>",
        f"👥 {escape(schedule.group)}",
        "",
    ]
    used_pairs: set[int] = set()
    with_talks = False
    if not schedule.lessons:
        lines.append("В файле нет занятий для этой группы.")
    for lesson in schedule.lessons:
        lines.append(_lesson_line(lesson, day))
        lines.append("")
        if not lesson.is_free:
            if lesson.pair_start is None:
                with_talks = True
            else:
                used_pairs.update(range(lesson.pair_start, lesson.pair_end + 1))

    bell_lines = day.table(used_pairs, with_talks)
    if bell_lines:
        lines.append("<b>🔔 Звонки</b>")
        lines.extend(bell_lines)
    return "\n".join(lines).rstrip()
