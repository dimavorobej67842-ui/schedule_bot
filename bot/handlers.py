"""Команды Telegram-бота: /start /group /today /tomorrow + кнопки + выбор группы."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from html import escape

import docx
from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services import pdf_bridge, pipeline, visits
from services.formatter import format_schedule
from services.models import DaySchedule
from services.parser import collect_groups, extract_header_stamp, normalize_group
from services.state import State, left_reason, note_left, note_return
from services.vk_source import VkError, is_auth_error

log = logging.getLogger(__name__)
router = Router()

_MOSCOW = ZoneInfo("Europe/Moscow")


def _today() -> date:
    """«Сегодня» по Москве: бот живёт на VPS и не должен зависеть от его TZ."""
    return datetime.now(_MOSCOW).date()

_CB_PREFIX = "group:"
_ACT_PREFIX = "act:"

_BTN_TODAY = "📅 Сегодня"
_BTN_TOMORROW = "📅 Завтра"
_BTN_GROUP = "👥 Группа"
_BTN_HELP = "ℹ️ Помощь"

_HELP = (
    "Присылаю расписание группы {group}.\n\n"
    "Кнопки внизу:\n"
    "— 📅 Сегодня — расписание на сегодня\n"
    "— 📅 Завтра — расписание на завтра\n"
    "— 👥 Группа — выбрать или сменить группу\n"
    "— ℹ️ Помощь — это сообщение\n\n"
    "Команды /today, /tomorrow, /group работают так же.\n\n"
    "Как в беседе VK появляется новый файл — присылаю расписание сам.\n\n"
    "Вопросы и предложения: @CTPECCOCTb"
)


class VisitMiddleware(BaseMiddleware):
    """Лог каждого действия пользователя в visits.jsonl."""

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        try:
            visits.record(event, settings.visits_file)
        except Exception:
            log.exception("Не удалось записать визит в %s", settings.visits_file)
        user = getattr(event, "from_user", None)
        if user is not None:
            # пользователь снова активен — снять отметку об уходе
            await asyncio.to_thread(note_return, settings.state_file, user.id)
        return await handler(event, data)


router.message.outer_middleware(VisitMiddleware())
router.callback_query.outer_middleware(VisitMiddleware())


def _reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянные кнопки под полем ввода — вместо набора команд."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=_BTN_TODAY), KeyboardButton(text=_BTN_TOMORROW)],
            [KeyboardButton(text=_BTN_GROUP), KeyboardButton(text=_BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def _schedule_keyboard() -> InlineKeyboardMarkup:
    """Кнопки под присланным расписанием: повторить/сменить без ввода команд."""
    kb = InlineKeyboardBuilder()
    kb.button(text=_BTN_TODAY, callback_data=f"{_ACT_PREFIX}today")
    kb.button(text=_BTN_TOMORROW, callback_data=f"{_ACT_PREFIX}tomorrow")
    kb.button(text=_BTN_GROUP, callback_data=f"{_ACT_PREFIX}group")
    kb.adjust(2, 1)
    return kb.as_markup()


def _fmt_visits(rows: list[dict], total_actions: int) -> str:
    if not rows:
        return "Пока никто не заходил."

    def _plural(n: int) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return f"{n} заход"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return f"{n} захода"
        return f"{n} заходов"

    def _times(u: dict) -> str:
        if not u["last_ts"]:
            return "заходов не залогировано"
        return f"первый {u['first_ts']} · последний {u['last_ts']} · {_plural(u['count'])}"

    lines = [f"Заходили в бота: {len(rows)} чел., всего {total_actions} действий.", ""]
    shown = rows[:30]
    for u in shown:
        parts = []
        if u["name"]:
            parts.append(escape(u["name"]))
        if u["username"]:
            parts.append(f"(@{escape(u['username'])})")
        who = " ".join(parts) or str(u["user_id"])
        group = escape(u["group"]) if u["group"] else "—"
        lines.append(f"{who} — {group}")
        lines.append(f"    {_times(u)}")
    if len(rows) > len(shown):
        lines.append(f"\n…и ещё {len(rows) - len(shown)}")
    return "\n".join(lines)


def _groups_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for name in groups:
        kb.button(text=name, callback_data=f"{_CB_PREFIX}{name}")
    kb.adjust(2)
    return kb.as_markup()


async def _send_menu(bot, chat_id: int) -> None:
    try:
        groups = await asyncio.to_thread(pipeline.list_groups)
    except pipeline.NoFileError:
        await bot.send_message(
            chat_id,
            "В беседе VK пока нет файла расписания — меню групп появится позже.",
            reply_markup=_reply_keyboard(),
        )
        return
    except VkError as exc:
        if is_auth_error(exc):
            await bot.send_message(
                chat_id,
                "⚠️ VK-токен перестал работать (протух или отозван). "
                "Обнови VK_TOKEN в .env — как, в README.",
                reply_markup=_reply_keyboard(),
            )
        else:
            log.exception("VK: не удалось получить список групп")
            await bot.send_message(chat_id, f"VK недоступен: {exc}", reply_markup=_reply_keyboard())
        return
    except Exception:
        log.exception("Не удалось получить список групп")
        await bot.send_message(
            chat_id,
            "Не удалось получить список групп — подробности в логах.",
            reply_markup=_reply_keyboard(),
        )
        return
    await bot.send_message(chat_id, "Выбери свою группу:", reply_markup=_groups_keyboard(groups))


async def _send_schedule(bot, chat_id: int, user_id: int, target: date) -> None:
    state = State.load(settings.state_file)
    group = state.group_of(user_id)
    if not group:
        await _send_menu(bot, chat_id)
        return
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    with_homework = chat_id == settings.chat_id
    try:
        text = await asyncio.to_thread(pipeline.render_for_date, target, group, with_homework)
    except pipeline.NoFileError as exc:
        await bot.send_message(chat_id, str(exc), reply_markup=_reply_keyboard())
    except VkError as exc:
        if is_auth_error(exc):
            await bot.send_message(
                chat_id,
                "⚠️ VK-токен перестал работать (протух или отозван). "
                "Обнови VK_TOKEN в .env — как, в README.",
                reply_markup=_reply_keyboard(),
            )
        else:
            log.exception("VK: ошибка при сборке расписания")
            await bot.send_message(chat_id, f"VK недоступен: {exc}", reply_markup=_reply_keyboard())
    except Exception:
        log.exception("Не удалось собрать расписание на %s", target)
        await bot.send_message(
            chat_id,
            "Не удалось собрать расписание — подробности в логах.",
            reply_markup=_reply_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=_schedule_keyboard(),
        )


async def _send_help(bot, chat_id: int, user_id: int) -> None:
    state = State.load(settings.state_file)
    group = state.group_of(user_id) or "—"
    await bot.send_message(
        chat_id,
        _HELP.format(group=escape(group)),
        parse_mode="HTML",
        reply_markup=_reply_keyboard(),
    )


def _display_name(user) -> str:
    """Имя пользователя так, как оно написано в профиле (нике), без изменений."""
    if user.first_name:
        return user.first_name
    if user.username:
        return f"@{user.username}"
    return "друг"


async def _notify_owner_join(message: Message, is_new: bool) -> None:
    """Владельцу автоматически: кто зашёл в бота (ник + юзернейм). О себе — не шлём."""
    user = message.from_user
    if user is None or user.id == settings.chat_id:
        return
    name = escape(f"{user.first_name or ''} {user.last_name or ''}".strip() or "—")
    uname = f"@{escape(user.username)}" if user.username else "без юзернейма"
    mark = "новый пользователь" if is_new else "заходил ранее"
    try:
        await message.bot.send_message(
            settings.chat_id,
            f"👤 Зашёл в бота: <b>{name}</b>\n"
            f"{uname} · id <code>{user.id}</code>\n"
            f"{mark}",
            parse_mode="HTML",
        )
    except Exception:
        log.exception("Не удалось уведомить владельца о входе %s", user.id)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = str(message.from_user.id)
    state = State.load(settings.state_file)
    is_new = uid not in state.greeted
    await _notify_owner_join(message, is_new)
    if is_new:
        State.update(settings.state_file, lambda s: s.greeted.append(uid))
        name = escape(_display_name(message.from_user))
        await message.answer(
            f"Привет, {name}! 👋\n"
            "Я присылаю расписание КНТ. Как только в беседе VK появляется новый файл — "
            "пришлю расписание твоей группы сам.\n\n"
            "Сначала выбери группу кнопками ниже:",
            parse_mode="HTML",
            reply_markup=_reply_keyboard(),
        )
    else:
        await _send_help(message.bot, message.chat.id, message.from_user.id)
    await _send_menu(message.bot, message.chat.id)


@router.message(Command("group"))
async def cmd_group(message: Message) -> None:
    await _send_menu(message.bot, message.chat.id)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await _send_schedule(message.bot, message.chat.id, message.from_user.id, _today())


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message) -> None:
    await _send_schedule(
        message.bot, message.chat.id, message.from_user.id, _today() + timedelta(days=1)
    )


@router.message(F.text == _BTN_TODAY)
async def btn_today(message: Message) -> None:
    await _send_schedule(message.bot, message.chat.id, message.from_user.id, _today())


@router.message(F.text == _BTN_TOMORROW)
async def btn_tomorrow(message: Message) -> None:
    await _send_schedule(
        message.bot, message.chat.id, message.from_user.id, _today() + timedelta(days=1)
    )


@router.message(F.text == _BTN_GROUP)
async def btn_group(message: Message) -> None:
    await _send_menu(message.bot, message.chat.id)


@router.message(F.text == _BTN_HELP)
async def btn_help(message: Message) -> None:
    await _send_help(message.bot, message.chat.id, message.from_user.id)


async def _fill_names(bot, rows: list[dict]) -> None:
    """Подтянуть имя/юзернейм из Telegram для тех, кого ещё нет в логе визитов."""
    for u in rows:
        if u["name"] and u["username"]:
            continue
        try:
            chat = await bot.get_chat(u["user_id"])
        except TelegramAPIError as exc:
            log.warning("Не удалось получить профиль %s: %s", u["user_id"], exc)
            reason = left_reason(exc)
            if reason:
                note_left(settings.state_file, u["user_id"], reason)
            continue
        if not u["name"]:
            u["name"] = " ".join(filter(None, [chat.first_name or "", chat.last_name or ""])).strip()
        if not u["username"]:
            u["username"] = chat.username or ""


@router.message(Command("visits"))
async def cmd_visits(message: Message) -> None:
    """Кто заходил в бота — только для владельца, остальным молча игнорируем."""
    if message.from_user.id != settings.chat_id:
        return
    state = State.load(settings.state_file)
    rows, total_actions = visits.summary(state, settings.visits_file)
    await _fill_names(message.bot, rows)
    await message.answer(_fmt_visits(rows, total_actions))


def _fmt_users(rows: list[dict], left: dict[str, str]) -> str:
    """Таблица пользователей: юзернейм | имя | группа; в конце счётчики и кто вышел."""
    if not rows and not left:
        return "Пока никто не заходил."

    def _line(u: dict) -> str:
        username = f"@{u['username']}" if u["username"] else "—"
        name = u["name"] or "—"
        group = u["group"] or "—"
        return f"{username} | {name} | {group}"

    by_id = {str(u["user_id"]): u for u in rows}
    active = [u for u in rows if str(u["user_id"]) not in left]
    ordered = sorted(
        active,
        key=lambda u: (u["group"] or "яяя", (u["name"] or "").lower(), u["username"] or ""),
    )
    lines = ["Юзернейм | Имя | Группа", ""]
    lines += [_line(u) for u in ordered] or ["—"]

    by_group: dict[str, int] = {}
    for u in active:
        g = u["group"] or "без группы"
        by_group[g] = by_group.get(g, 0) + 1
    lines += ["", "Пользователей по группам:"]
    lines += [f"{g} — {n}" for g, n in sorted(by_group.items(), key=lambda kv: (-kv[1], kv[0]))]
    lines.append(f"Всего пользователей: {len(active)}")

    if left:
        gone = sorted(left.items(), key=lambda kv: kv[1])
        lines += ["", f"Вышли из бота ({len(left)}):"]
        for uid, stamp in gone:
            u = by_id.get(uid, {})
            username = f"@{u['username']}" if u.get("username") else "—"
            name = u.get("name") or "—"
            group = u.get("group") or "—"
            lines.append(f"{username} | {name} | {group} | {stamp}")
    return "\n".join(lines)


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    """Все пользователи с группами — только для владельца, остальным молча игнорируем."""
    if message.from_user.id != settings.chat_id:
        return
    state = State.load(settings.state_file)
    rows, _total_actions = visits.summary(state, settings.visits_file)
    await _fill_names(message.bot, rows)
    text = _fmt_users(rows, state.left)
    # Telegram режет сообщения длиннее 4096 символов — шлём кусками по границе строк
    while len(text) > 4000:
        cut = text.rfind("\n", 0, 4000)
        await message.answer(text[:cut])
        text = text[cut + 1:]
    await message.answer(text)


@router.callback_query(F.data.startswith(_CB_PREFIX))
async def cb_pick_group(callback: CallbackQuery) -> None:
    group = callback.data[len(_CB_PREFIX):]
    uid = str(callback.from_user.id)
    State.update(settings.state_file, lambda s: s.users.__setitem__(uid, group))
    await callback.answer(f"Группа {group} сохранена")
    await callback.message.answer(
        _HELP.format(group=escape(group)),
        parse_mode="HTML",
        reply_markup=_reply_keyboard(),
    )
    try:
        with_homework = callback.message.chat.id == settings.chat_id
        text = await asyncio.to_thread(pipeline.render_latest, group, with_homework)
    except pipeline.NoFileError as exc:
        await callback.message.answer(str(exc))
    except Exception:
        log.exception("Не удалось прислать расписание после выбора группы %s", group)
        await callback.message.answer("Не удалось собрать расписание — подробности в логах.")
    else:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_schedule_keyboard(),
        )


@router.callback_query(F.data == f"{_ACT_PREFIX}today")
async def cb_today(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_schedule(
        callback.bot, callback.message.chat.id, callback.from_user.id, _today()
    )


@router.callback_query(F.data == f"{_ACT_PREFIX}tomorrow")
async def cb_tomorrow(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_schedule(
        callback.bot,
        callback.message.chat.id,
        callback.from_user.id,
        _today() + timedelta(days=1),
    )


@router.callback_query(F.data == f"{_ACT_PREFIX}group")
async def cb_group(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_menu(callback.bot, callback.message.chat.id)


# ---------- Рассылка файла владельцем: /push, /push_test, кнопки после файла ----------

_PUSH_PREFIX = "push:"
_BTN_PUSH_ALL = "📤 Отправить расписание (всем)"
_BTN_PUSH_TEST = "🧪 Отправить расписание (тест)"
_BTN_RESEND = "🔁 Дослать"

# одна рассылка за раз: повторное нажатие кнопки не должно дублировать 340 сообщений
_push_lock = asyncio.Lock()


def _push_keyboard(push_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=_BTN_PUSH_ALL, callback_data=f"{_PUSH_PREFIX}all:{push_id}")
    kb.button(text=_BTN_PUSH_TEST, callback_data=f"{_PUSH_PREFIX}test:{push_id}")
    kb.adjust(1)
    return kb.as_markup()


def _resend_keyboard(n: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_BTN_RESEND} ({n})", callback_data=f"{_PUSH_PREFIX}resend")
    return kb.as_markup()


def _no_lessons_message(group: str, day: date, week: int | None, title: str) -> str:
    """Тот же заголовок/стиль, что и обычное расписание, но без пар."""
    schedule = DaySchedule(date=day, week=week, group=group, lessons=[],
                           source_filename=title)
    text = format_schedule(schedule, pipeline.bells, heading="Новое расписание на")
    return text.replace("В файле нет занятий для этой группы.", "▫️ Занятий нет")


def _push_meta(path: Path) -> dict:
    """Штамп и группы файла; ValueError — файл не похож на расписание."""
    doc = docx.Document(str(path))
    day, week = extract_header_stamp(doc)
    groups = collect_groups(doc)
    if not groups:
        raise ValueError("в файле нет ни одной группы")
    if not any(not les.is_free for lessons in groups.values() for les in lessons):
        raise ValueError("в файле нет ни одного занятия (только «Нет урока»)")
    return {"date": day, "week": week, "groups": groups}


async def _download_push_file(message: Message) -> Path:
    """Скачать документ владельца в downloads/; PDF сконвертировать в .docx."""
    doc = message.document
    name = Path(doc.file_name or "push.docx").name
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("нужен файл .pdf или .docx")
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem or "push"
    if suffix == ".pdf":
        pdf_path = settings.download_dir / f"{stem}.pdf"
        await message.bot.download(doc, destination=pdf_path)
        out = settings.download_dir / f"{stem}.docx"
        pdf_bridge.pdf_to_docx(pdf_path, out)
        return out
    out = settings.download_dir / f"{stem}.docx"
    await message.bot.download(doc, destination=out)
    return out


def _build_full_messages(path: Path, title: str, state: State, *,
                         mark: bool) -> list[tuple[str, str]]:
    """Сообщения по файлу для всех (владелец включён) + «Занятий нет» отсутствующим.

    mark=True — через watcher._process_file: дедуп записывается ДО отправки, чтобы
    VK/сайт не разослали этот же файл повторно. mark=False — только сборка (тест,
    дослать): через _process_file дедуп бы «съел» последующую настоящую рассылку.
    """
    import watcher  # noqa: PLC0415 — локально, иначе цикл handlers <-> watcher

    if mark:
        messages = watcher._process_file(path, title, state,
                                         source="файл в чат", force=True)
    else:
        messages = watcher._build_messages(path, title, state)

    meta = _push_meta(path)
    groups_in_file = {normalize_group(g) for g in meta["groups"]}
    day, week = meta["date"], meta["week"]

    owner_id = str(settings.chat_id)
    recipients: list[tuple[str, str]] = [
        (owner_id, state.group_of(owner_id) or settings.group),
    ]
    recipients += [(uid, group) for uid, group in state.users.items() if uid != owner_id]

    sent_uids = {uid for uid, _ in messages}
    extra: list[tuple[str, str]] = []
    for uid, group in recipients:
        if uid in sent_uids or normalize_group(group or "") in groups_in_file:
            continue
        extra.append((uid, _no_lessons_message(group or "—", day, week, title)))
    return messages + extra


def _fmt_push_report(title: str, meta: dict, results: dict[str, dict],
                     state: State, *, test: bool) -> str:
    ok_n = sum(1 for r in results.values() if r["ok"])
    bad = [(uid, r.get("reason") or "ошибка") for uid, r in results.items() if not r["ok"]]
    week = meta["week"]
    week_s = f" · {week} неделя" if week is not None else ""
    head = "🧪 Тестовая рассылка (только вам)" if test else "📋 Рассылка"
    lines = [
        f"{head}: {title} ({meta['date']:%d.%m.%Y}{week_s})",
        f"👥 Получателей: {len(results)}",
        "",
        f"✅ Доставлено: {ok_n}",
    ]
    if not test:
        lines.append(f"❌ Не доставлено: {len(bad)}")
        shown = bad[:30]
        for uid, reason in shown:
            group = state.group_of(uid) or "—"
            lines.append(f"  {uid} · {escape(group)} · {escape(reason)}")
        if len(bad) > len(shown):
            lines.append(f"  …и ещё {len(bad) - len(shown)}")
    return "\n".join(lines)


async def _run_push(message: Message, mode: str, push_id: str = "") -> None:
    """mode: all | test | resend. Рассылка, отчёт «кто получил», кнопка «Дослать».

    push_id привязывает кнопку к конкретному скинутому файлу: кнопка от старого
    файла не должна разослать более новый, скинутый следом.
    """
    import watcher  # noqa: PLC0415 — локально, иначе цикл handlers <-> watcher

    async with _push_lock:
        state = State.load(settings.state_file)
        if mode == "resend":
            pending = state.pending_resend
            if not pending or not pending.get("uids"):
                await message.answer("Некого досылать — все получили.")
                return
        else:
            pending = state.pending_push
            if not pending:
                await message.answer(
                    "Сначала скинь файл расписания (.pdf или .docx), потом жми кнопку."
                )
                return
            if push_id and str(pending.get("push_id") or "") != push_id:
                await message.answer(
                    "Эта кнопка от более старого файла — скинь файл заново и нажми под свежим."
                )
                return
            if mode == "all" and pending.get("sent"):
                await message.answer("Этот файл уже разослан всем. Скинь файл заново.")
                return

        path = Path(pending["path"])
        title = str(pending.get("title") or path.name)
        if not path.exists():
            await message.answer(f"⚠️ Файл {escape(title)} не найден на сервере — скинь заново.")
            return

        try:
            meta = _push_meta(path)
        except Exception as exc:
            await message.answer(f"⚠️ Файл не разобрать: {escape(str(exc))}")
            return

        if mode == "test":
            owner_id = str(settings.chat_id)
            messages = [(uid, text) for uid, text
                        in _build_full_messages(path, title, state, mark=False)
                        if uid == owner_id]
        elif mode == "resend":
            want = set(pending["uids"])
            messages = [(uid, text) for uid, text
                        in _build_full_messages(path, title, state, mark=False)
                        if uid in want]
        else:
            # фиксируем до отправки: повторное нажатие не дублирует рассылку
            State.update(settings.state_file,
                         lambda s: s.pending_push.update({"sent": True}))
            messages = _build_full_messages(path, title, state, mark=True)

        if not messages:
            await message.answer("0 сообщений — рассылка не запущена.")
            return

        results = await watcher._send_many(message.bot, messages)
        bad_uids = {uid: (r.get("reason") or "ошибка")
                    for uid, r in results.items() if not r["ok"]}

        if mode in {"all", "resend"}:
            State.update(settings.state_file, lambda s: setattr(s, "pending_resend", {
                "path": str(path), "title": title,
                "date": meta["date"].isoformat(),
                "uids": bad_uids,
                "ts": f"{datetime.now():%d.%m.%Y %H:%M}",
            }))

        report = _fmt_push_report(title, meta, results, state, test=(mode == "test"))
        if bad_uids:
            await message.answer(report, reply_markup=_resend_keyboard(len(bad_uids)))
        else:
            await message.answer(report)

        if mode != "resend":
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:
                pass


@router.message(F.document)
async def owner_push_file(message: Message) -> None:
    """Владелец скидывает файл расписания — готовим рассылку, режим выбирает кнопками."""
    if message.from_user.id != settings.chat_id:
        return
    try:
        path = await _download_push_file(message)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось принять файл: {escape(str(exc))}")
        return
    try:
        meta = _push_meta(path)
    except Exception as exc:
        await message.answer(f"⚠️ Это не похоже на расписание: {escape(str(exc))}")
        return

    state = State.load(settings.state_file)
    recipients_n = 1 + sum(1 for uid in state.users if uid != str(settings.chat_id))
    groups_n = len(meta["groups"])
    week = meta["week"]
    week_s = f" · {week} неделя" if week is not None else ""
    push_id = f"{datetime.now():%Y%m%d%H%M%S}"
    State.update(settings.state_file, lambda s: setattr(s, "pending_push", {
        "path": str(path), "title": path.name,
        "date": meta["date"].isoformat(), "week": week,
        "push_id": push_id,
        "sent": False, "ts": f"{datetime.now():%d.%m.%Y %H:%M}",
    }))
    await message.answer(
        f"📄 Файл принят: <b>{escape(path.name)}</b>\n"
        f"📅 {meta['date']:%d.%m.%Y}{week_s}\n"
        f"👥 Групп в файле: {groups_n} · получателей: {recipients_n}\n\n"
        "Выбери режим рассылки:",
        parse_mode="HTML",
        reply_markup=_push_keyboard(push_id),
    )


@router.callback_query(F.data.startswith(_PUSH_PREFIX))
async def cb_push(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.chat_id:
        await callback.answer("Рассылкой управляет владелец бота.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    mode = parts[1] if len(parts) > 1 else ""
    push_id = parts[2] if len(parts) > 2 else ""
    if mode not in {"all", "test", "resend"}:
        await callback.answer()
        return
    await callback.answer("Рассылка…")
    await _run_push(callback.message, mode=mode, push_id=push_id)


@router.message(Command("push"))
async def cmd_push(message: Message) -> None:
    """Расслать расписание из последнего файла всем (только владелец)."""
    if message.from_user.id != settings.chat_id:
        return
    await _run_push(message, mode="all")


@router.message(Command("push_test"))
async def cmd_push_test(message: Message) -> None:
    """Расслать расписание из последнего файла только владельцу (тест)."""
    if message.from_user.id != settings.chat_id:
        return
    await _run_push(message, mode="test")
