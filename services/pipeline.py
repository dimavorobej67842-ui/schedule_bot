"""Общий конвейер: файл расписания -> парсинг -> ДЗ -> текст сообщения."""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

import docx

from config import settings
from services import eljur as eljur_mod
from services.bells import Bells
from services.formatter import format_schedule
from services.models import DaySchedule
from services.parser import collect_groups, parse_docx
from services.state import State
from services.vk_source import VkSource

log = logging.getLogger(__name__)

bells = Bells(settings.bells_file)

_groups_cache: tuple[int, list[str]] | None = None


class NoFileError(RuntimeError):
    pass


def make_vk() -> VkSource:
    return VkSource(settings.vk_token, settings.vk_chat_peer_id, settings.download_dir)


def load_schedule_from_file(path, group: str | None = None) -> DaySchedule:
    return parse_docx(path, group or settings.group)


def enrich_with_homework(schedule: DaySchedule) -> None:
    if not settings.eljur_url:
        return
    try:
        hw_by_subject = eljur_mod.fetch_homework(settings.eljur_url, settings.eljur_login,
                                                 settings.eljur_password, schedule.date)
    except Exception:
        log.exception("ЭлЖур: не удалось получить ДЗ, шлём расписание без ДЗ")
        return
    if not hw_by_subject:
        return
    for lesson in schedule.lessons:
        if lesson.is_free:
            continue
        lesson.homework = eljur_mod.match_homework(lesson.subject, hw_by_subject)


def file_groups(path) -> list[str]:
    """Список групп из одного файла расписания."""
    return sorted(collect_groups(docx.Document(str(path))))


def note_groups(groups: Iterable[str]) -> None:
    """Запомнить группы в state: меню переживает частичные/новые файлы."""
    st = State.load(settings.state_file)
    merged = sorted(set(st.known_groups) | set(groups))
    if merged != st.known_groups:
        st.known_groups = merged
        st.save(settings.state_file)


def list_groups() -> list[str]:
    """Все группы: union из последних файлов, накопленного списка и выбора пользователей."""
    global _groups_cache
    vk = make_vk()
    docs = vk.find_recent_docx(limit=5)
    if not docs:
        st = State.load(settings.state_file)
        known = sorted(set(st.known_groups) | set(st.users.values()))
        if known:
            return known
        raise NoFileError("В беседе VK нет ни одного файла .docx")
    newest_id = docs[0].message_id
    if _groups_cache and _groups_cache[0] == newest_id:
        return _groups_cache[1]
    groups: set[str] = set()
    for doc in docs:
        try:
            groups |= set(file_groups(vk.save(doc)))
        except Exception:
            log.exception("Не удалось разобрать %r для списка групп", doc.title)
    note_groups(groups)
    st = State.load(settings.state_file)
    groups |= set(st.known_groups) | set(st.users.values())
    result = sorted(groups)
    _groups_cache = (newest_id, result)
    return result


def _render(schedule: DaySchedule, with_homework: bool, heading: str) -> str:
    if with_homework:
        enrich_with_homework(schedule)
    return format_schedule(schedule, bells, heading=heading)


def render_for_date(target: date, group: str | None = None,
                    with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Текст сообщения с расписанием на target. Берёт самый свежий файл из беседы VK."""
    vk = make_vk()
    doc = vk.find_latest_docx(min_date=0)
    if doc is None:
        raise NoFileError("В беседе VK нет ни одного файла .docx")
    path = vk.save(doc)
    schedule = load_schedule_from_file(path, group)
    if schedule.date != target:
        raise NoFileError(
            f"Файл с расписанием есть, но он на {schedule.date:%d.%m}, а нужен {target:%d.%m}. "
            "Похоже, файл на нужный день ещё не кидали."
        )
    return _render(schedule, with_homework, heading)


def render_latest(group: str | None = None,
                  with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Расписание из самого свежего файла — независимо от того, на какой он день."""
    vk = make_vk()
    doc = vk.find_latest_docx(min_date=0)
    if doc is None:
        raise NoFileError("В беседе VK нет ни одного файла .docx")
    path = vk.save(doc)
    schedule = load_schedule_from_file(path, group)
    return _render(schedule, with_homework, heading)
