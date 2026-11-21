"""Общий конвейер: файл расписания -> парсинг -> ДЗ -> текст сообщения."""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import docx

from config import settings
from services import eljur as eljur_mod
from services.bells import Bells
from services.formatter import format_date, format_schedule
from services.models import DaySchedule, Lesson
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


def file_date(path) -> date | None:
    """Дата файла из колонтитула; None — колонтитул не разобран."""
    stamp = _stamp_of(Path(path))
    return stamp[0] if stamp else None


def _files_for_date(target: date) -> list[Path]:
    """Все локальные .docx с датой target в колонтитуле, от новых к старым.

    На день может быть несколько файлов (дневное расписание + «Изменения» с сайта) —
    для полной картины нужны все, иначе группы из второго файла «пропадают».
    """
    out: list[Path] = []
    for path in _local_files_newest_first():
        stamp = _stamp_of(path)
        if stamp and stamp[0] == target:
            out.append(path)
    return out


def _find_local_latest() -> Path | None:
    """Самый свежий локальный .docx с читаемым колонтитулом."""
    for path in _local_files_newest_first():
        if _stamp_of(path):
            return path
    return None


def merged_schedule_for(day: date, group: str) -> DaySchedule | None:
    """Расписание группы на день из ВСЕХ файлов этого дня; None — файлов нет.

    Дубли пар (дневной файл + «Изменения») схлопываются: свежий файл главнее.
    """
    files = _files_for_date(day)
    if not files:
        return None
    key = normalize_group(group or settings.group)
    merged: dict[tuple, Lesson] = {}
    week: int | None = None
    for path in reversed(files):  # старые первыми: новое перезаписывает
        try:
            groups = collect_groups(docx.Document(str(path)))
        except Exception:
            log.exception("Не удалось разобрать %r", path.name)
            continue
        if week is None:
            stamp = _stamp_of(path)
            if stamp:
                week = stamp[1]
        for les in groups.get(key, []):
            slot = ((les.pair_start, les.pair_end) if les.pair_start is not None
                    else ("label", les.pair_label))
            merged[slot] = les
    lessons = sorted(merged.values(),
                     key=lambda l: (l.pair_start is not None, l.pair_start or 0, l.pair_end or 0))
    return DaySchedule(date=day, group=key, week=week, lessons=lessons,
                       source_filename=files[0].name)


def _next_classes(target: date, group: str, lookahead: int = 8) -> tuple[date, DaySchedule] | None:
    """Ближайший день после target с занятиями группы (по локальным файлам)."""
    for shift in range(1, lookahead + 1):
        day = date.fromordinal(target.toordinal() + shift)
        sched = merged_schedule_for(day, group)
        if sched and any(not les.is_free for les in sched.lessons):
            return day, sched
    return None


def _render_rest_day(target: date, group: str, with_homework: bool) -> str:
    """День без пар: «отдыхай» + расписание ближайшего дня с парами (на каждую неделю)."""
    today = date.today()
    if target == today:
        when = "Сегодня"
    elif target == today + timedelta(days=1):
        when = "Завтра"
    else:
        when = f"На {target:%d.%m}"
    found = _next_classes(target, group)
    if found is None:
        raise NoFileError(
            f"{when} — {format_date(target)}: занятий нет, "
            f"и расписание на ближайшие дни пока не выложили."
        )
    day, sched = found
    if with_homework:
        enrich_with_homework(sched)
    head = f"{when} — {format_date(target)}: занятий нет, отдыхай 😊"
    nxt = f"Пары будут {format_date(day)}:"
    return f"{head}\n{nxt}\n\n" + format_schedule(sched, bells, heading="Расписание на")


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
            groups |= {g for g in collect_groups(docx.Document(str(path))) if is_group_code(g)}
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


def _fetch_vk_for_date(target: date) -> list[Path]:
    """Файлы беседы VK с датой target -> сохранены в download_dir, от новых к старым.

    NoFileError: в VK ничего на эту дату (или VK недоступен и локально пусто).
    """
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
    saved: list[Path] = []
    seen: set[date] = set()
    for info in reversed(infos):  # новые первыми
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
            saved.append(vk.save(vk.fetch(info)))
    if not saved:
        nearest = sorted(seen, key=lambda d: (abs((d - target).days), d))[:6]
        available = ", ".join(f"{d:%d.%m}" for d in sorted(nearest)) or "—"
        raise NoFileError(
            f"Расписание на {target:%d.%m} ещё не кидали. "
            f"Есть файлы на: {available}."
        )
    return saved


def render_for_date(target: date, group: str | None = None,
                    with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Текст сообщения с расписанием на target — из всех файлов с датой target.

    Имя файла датой не является, школа кидает расписание на следующий день
    (в пятницу — сразу на субботу и на понедельник), поэтому файлы ищутся по дате
    из колонтитула. На день бывает несколько файлов (дневной + «Изменения») —
    пары сливаются. Если занятий нет (воскресенье, каникулы, группы не в файле) —
    показываем ближайший день с парами и «отдыхай»: так на каждую неделю.
    """
    group = group or settings.group
    files = _files_for_date(target)
    if not files:
        try:
            files = _fetch_vk_for_date(target)
        except NoFileError:
            files = []
    if files:
        schedule = merged_schedule_for(target, group)
        if schedule and any(not les.is_free for les in schedule.lessons):
            return _render(schedule, with_homework, heading)
    return _render_rest_day(target, group, with_homework)


def render_latest(group: str | None = None,
                  with_homework: bool = False, heading: str = "Расписание на") -> str:
    """Расписание на самый свежий день с файлами — независимо от даты."""
    group = group or settings.group
    latest = None
    for path in _local_files_newest_first():
        stamp = _stamp_of(path)
        if stamp and (latest is None or stamp[0] > latest):
            latest = stamp[0]
    if latest is not None:
        schedule = merged_schedule_for(latest, group)
        if schedule is not None:
            return _render(schedule, with_homework, heading)
    try:
        vk = make_vk()
        doc = vk.find_latest_docx(min_date=0)
    except VkError as exc:
        raise NoFileError("Файл расписания ещё не загружен, и VK сейчас недоступен.") from exc
    if doc is None:
        raise NoFileError("Пока нет ни одного файла расписания.")
    path = vk.save(doc)
    schedule = _load_schedule(path, group, _stamp_of(path)[0] if _stamp_of(path) else date.today())
    return _render(schedule, with_homework, heading)
