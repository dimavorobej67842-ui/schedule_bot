"""Команды Telegram-бота: /start /group /today /tomorrow + выбор группы."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from html import escape

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services import pipeline, visits
from services.state import State
from services.vk_source import VkError, is_auth_error

log = logging.getLogger(__name__)
router = Router()

_CB_PREFIX = "group:"

_HELP = (
    "Присылаю расписание группы {group}.\n\n"
    "/today — расписание на сегодня\n"
    "/tomorrow — расписание на завтра\n"
    "/group — сменить группу\n\n"
    "Как в беседе VK появляется новый файл — присылаю расписание сам."
)


class VisitMiddleware(BaseMiddleware):
    """Лог каждого действия пользователя в visits.jsonl."""

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        try:
            visits.record(event, settings.visits_file)
        except Exception:
            log.exception("Не удалось записать визит в %s", settings.visits_file)
        return await handler(event, data)


router.message.outer_middleware(VisitMiddleware())
router.callback_query.outer_middleware(VisitMiddleware())


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
        who = escape(u["name"] or str(u["user_id"]))
        if u["username"]:
            who += f" (@{escape(u['username'])})"
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


async def _send_menu(message: Message) -> None:
    try:
        groups = await asyncio.to_thread(pipeline.list_groups)
    except pipeline.NoFileError:
        await message.answer("В беседе VK пока нет файла расписания — меню групп появится позже.")
        return
    except VkError as exc:
        if is_auth_error(exc):
            await message.answer(
                "⚠️ VK-токен перестал работать (протух или отозван). "
                "Обнови VK_TOKEN в .env — как, в README."
            )
        else:
            log.exception("VK: не удалось получить список групп")
            await message.answer(f"VK недоступен: {exc}")
        return
    except Exception:
        log.exception("Не удалось получить список групп")
        await message.answer("Не удалось получить список групп — подробности в логах.")
        return
    await message.answer("Выбери свою группу:", reply_markup=_groups_keyboard(groups))


async def _send_schedule(message: Message, user_id: int, target: date) -> None:
    state = State.load(settings.state_file)
    group = state.group_of(user_id)
    if not group:
        await message.answer("Сначала выбери группу:")
        await _send_menu(message)
        return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    with_homework = message.chat.id == settings.chat_id
    try:
        text = await asyncio.to_thread(pipeline.render_for_date, target, group, with_homework)
    except pipeline.NoFileError as exc:
        await message.answer(str(exc))
    except VkError as exc:
        if is_auth_error(exc):
            await message.answer(
                "⚠️ VK-токен перестал работать (протух или отозван). "
                "Обнови VK_TOKEN в .env — как, в README."
            )
        else:
            log.exception("VK: ошибка при сборке расписания")
            await message.answer(f"VK недоступен: {exc}")
    except Exception:
        log.exception("Не удалось собрать расписание на %s", target)
        await message.answer("Не удалось собрать расписание — подробности в логах.")
    else:
        await message.answer(text, parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _send_menu(message)


@router.message(Command("group"))
async def cmd_group(message: Message) -> None:
    await _send_menu(message)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await _send_schedule(message, message.from_user.id, date.today())


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message) -> None:
    await _send_schedule(message, message.from_user.id, date.today() + timedelta(days=1))


@router.message(Command("visits"))
async def cmd_visits(message: Message) -> None:
    """Кто заходил в бота — только для владельца, остальным молча игнорируем."""
    if message.from_user.id != settings.chat_id:
        return
    state = State.load(settings.state_file)
    rows, total_actions = visits.summary(state, settings.visits_file)
    await message.answer(_fmt_visits(rows, total_actions))


@router.callback_query(F.data.startswith(_CB_PREFIX))
async def cb_pick_group(callback: CallbackQuery) -> None:
    group = callback.data[len(_CB_PREFIX):]
    state = State.load(settings.state_file)
    state.users[str(callback.from_user.id)] = group
    state.save(settings.state_file)
    await callback.answer(f"Группа {group} сохранена")
    await callback.message.answer(_HELP.format(group=escape(group)), parse_mode="HTML")
    try:
        with_homework = callback.message.chat.id == settings.chat_id
        text = await asyncio.to_thread(pipeline.render_latest, group, with_homework)
    except pipeline.NoFileError as exc:
        await callback.message.answer(str(exc))
    except Exception:
        log.exception("Не удалось прислать расписание после выбора группы %s", group)
        await callback.message.answer("Не удалось собрать расписание — подробности в логах.")
    else:
        await callback.message.answer(text, parse_mode="HTML")
