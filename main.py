"""Запуск: Telegram-бот (команды) + слежение за беседой VK."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonDefault

from bot.handlers import router
from config import settings
from watcher import watch_vk


def make_bot() -> Bot:
    session = AiohttpSession(proxy=settings.tg_proxy) if settings.tg_proxy else None
    return Bot(settings.bot_token, session=session)


async def set_commands_menu(bot: Bot) -> None:
    """Меню команд рядом с полем ввода (кнопка ☰).

    /visits показывается только в чате владельца (scope=Chat) —
    остальные его не видят, а хендлер молча игнорирует чужие запросы.
    """
    common = [
        BotCommand(command="start", description="Старт и выбор группы"),
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
        BotCommand(command="group", description="Выбрать или сменить группу"),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        common + [BotCommand(command="visits", description="Кто заходил (только тебе)")],
        scope=BotCommandScopeChat(chat_id=settings.chat_id),
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = make_bot()
    await set_commands_menu(bot)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(watch_vk(bot))
    log = logging.getLogger(__name__)
    log.info("Бот запущен, группа %s, беседа VK %s, прокси %s",
             settings.group, settings.vk_chat_peer_id, settings.tg_proxy or "нет")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
