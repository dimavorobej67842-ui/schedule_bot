"""Общий конвейер: файл расписания -> парсинг -> ДЗ -> текст сообщения."""

from __future__ import annotations

import io
import logging
from datetime import date
from typing import Iterable

import docx

from config import settings
from services import eljur as eljur_mod
from services.bells import Bells
from services.formatter import format_schedule
from services.models import DaySchedule
from services.parser import collect_groups, extract_header_stamp, is_group_code, normalize_group, parse_docx
from services.state import State
from services.vk_source import VkError, VkSource

log = logging.getLogger(__name__)

bells = Bells(settings.bells_file)

_groups_cache: tuple[object, list[str]] | None = None


class NoFileError(RuntimeError):
    pass


def make_vk() -> VkSource:
    return VkSource(settings.vk_token, settings.vk_chat_peer_id, settings.download_dir)


def load_schedule_from_file(path, group: str | None = None) -> DaySchedule:
    return parse_docx(path, group or settings.group)


def looks_like_schedule(path) -> bool:
    """True, если в файле есть группы с настоящими занятиями — это расписание.

    Чужие файлы (ведомости, списки групп) дают либо ноль групп, либо только
    строки «Нет урока» — такое расписанием не считаем. Группы настоящего
    расписания запоминаются в state — чтобы меню переживало частичные файлы.
    """
    groups = collect_groups(docx.Document(str(path)))
    is_schedule = any(not les.is_free for lessons in groups.values() for les in lessons)
    if is_schedule:
        note_groups(groups)
    return is_schedule


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


def _local_files_newest_first() -> list[Path]:
    """Все .docx, накопленные в download_dir (VK-файлы и конверсии с сайта)."""
    if not settings.download_dir.exists():
        return []

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(settings.download_dir.glob("*.docx"), key=mtime, reverse=True)


def _stamp_of(path: Path) -> tuple[date, int | None] | None:
    try:
        return extract_header_stamp(docx.Document(str(path)))
    except Exception:
        return None


def _find_local_for_date(target: date) -> Path | None:
    """Самый свежий локальный .docx с датой target в колонтитуле."""
    for path in _local_files_newest_first():
        stamp = _stamp_of(path)
        if stamp and stamp[0] == target:
            return path
    return None


def _find_local_latest() -> Path | None:
    """Самый свежий локальный .docx с читаемым колонтитулом."""
    for path in _local_files_newest_first():
        if _stamp_of(path):
            return path
    return None


def note_groups(groups: Iterable[str]) -> None:
    """Запомнить группы в state: меню переживает частичные файлы («Изменения» и т.п.)."""
    new = {normalize_group(g) for g in groups if g and is_group_code(g)}
    if not new:
        return
    State.update(settings.state_file,
                 lambda s: setattr(s, "known_groups", sorted(set(s.known_groups) | new)))


def list_groups() -> list[str]:
    """Все группы: union свежих файлов + накопленный список + выбор пользователей.

    Раньше брался один последний файл — и «Изменения к расписанию» с сайта
    (частичный файл) сужал меню до пары групп. Теперь список только растёт.
    """
    global _groups_cache
    files = [p for p in _local_files_newest_first()[:5] if _stamp_of(p)]
    if not files:
        st = State.load(settings.state_file)
        known = sorted(set(st.known_groups) | set(st.users.values()))
        if known:
            return known
        try:
            vk = make_vk()
            doc = vk.find_latest_docx(min_date=0)
        except VkError as exc:
            raise NoFileError("Файл расписания ещё не загружен, и VK сейчас недоступен.") from exc
        if doc is None:
            raise NoFileError("Пока нет ни одного файла расписания.")
        path = vk.save(doc)
        files = [path] if _stamp_of(path) else []
        if not files:
            raise NoFileError("Пока нет ни одного файла расписания.")
    key = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    if _groups_cache and _groups_cache[0] == key:
        return _groups_cache[1]
    groups: set[str] = set()
    for path in files:
        try:
            groups |= set(collect_groups(docx.Document(str(path))))
        except Exception:
            log.exception("Не удалось разобрать %r для списка групп", path.name)
    note_groups(groups)
    st = State.load(settings.state_file)
    groups |= set(st.known_groups) | set(st.users.values())
    result = sorted(groups)
    _groups_cache = (key, result)
    return result


def _render(schedule: DaySchedule, with_homework: bool, heading: str) -> str:
    if with_homework:
        enrich_with_homework(schedule)
    return format_schedule(schedule, bells, heading=heading)


# штампы «дата + неделя» по message_id: файлы в беседе неизменны, перечитывать их смысла нет
_doc_stamps: dict[int, tuple[date, int | None]] = {}


def _load_schedule(path: Path, group: str | None, day: date) -> DaySchedule:
    try:
        return load_schedule_from_file(path, group)
    except ValueError as exc:
        # группы в файле нет — для студента это «занятий нет», а не сбой
        raise NoFileError(
            f"В расписании на {day:%d.%m} нет твоей группы "
            f"«{group or settings.group}» — похоже, занятий нет."
        ) from exc


def render_for_date(target: date, group: str | None = None,
                    with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Текст сообщения с расписанием на target — из файла с датой target в колонтитуле.

    Имя файла датой не является, а школа кидает расписание на следующий день
    (в пятницу — сразу на субботу и на понедельник), поэтому файл ищется по дате
    из колонтитула, а не по имени/новизне. Сначала локальные копии в download_dir
    (работает и без VK), затем беседа VK.
    """
    path = _find_local_for_date(target)
    if path is not None:
        schedule = _load_schedule(path, group, target)
        return _render(schedule, with_homework, heading)

    # локально файла на эту дату нет — ищем в беседе VK
    try:
        vk = make_vk()
        infos = vk.list_docx(min_date=0)
    except VkError as exc:
        raise NoFileError(
            f"Расписание на {target:%d.%m} ещё не загружено, и VK сейчас недоступен. "
            f"Попробуй позже."
        ) from exc
    if not infos:
        raise NoFileError("Пока нет ни одного файла расписания.")
    match = None
    seen: set[date] = set()
    for info in reversed(infos):  # новые первыми: при двух файлах на день берём самый свежий
        stamp = _doc_stamps.get(info.message_id)
        if stamp is None:
            try:
                stamp = extract_header_stamp(docx.Document(io.BytesIO(vk.fetch(info).content)))
            except Exception:
                log.warning("VK: в файле %r нет даты в колонтитуле — пропускаем", info.title)
                continue
            _doc_stamps[info.message_id] = stamp
        seen.add(stamp[0])
        if stamp[0] == target:
            match = info
            break
    if match is None:
        nearest = sorted(seen, key=lambda d: (abs((d - target).days), d))[:6]
        available = ", ".join(f"{d:%d.%m}" for d in sorted(nearest)) or "—"
        raise NoFileError(
            f"Расписание на {target:%d.%m} ещё не кидали. "
            f"Есть файлы на: {available}."
        )
    path = vk.save(vk.fetch(match))
    schedule = _load_schedule(path, group, target)
    return _render(schedule, with_homework, heading)


def render_latest(group: str | None = None,
                  with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Расписание из самого свежего файла — независимо от того, на какой он день."""
    path = _find_local_latest()
    if path is None:
        try:
            vk = make_vk()
            doc = vk.find_latest_docx(min_date=0)
        except VkError as exc:
            raise NoFileError("Файл расписания ещё не загружен, и VK сейчас недоступен.") from exc
        if doc is None:
            raise NoFileError("Пока нет ни одного файла расписания.")
        path = vk.save(doc)
    stamp = _stamp_of(path)
    schedule = _load_schedule(path, group, stamp[0] if stamp else date.today())
    return _render(schedule, with_homework, heading)
