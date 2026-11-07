"""Команды Telegram-бота: /start /group /today /tomorrow + выбор группы."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services import pipeline
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
