"""Запуск: Telegram-бот (команды) + слежение за беседой VK."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonDefault

from bot.handlers import router
from config import settings
from watcher import watch_vk, watch_vk_favorites, watch_web

log = logging.getLogger(__name__)

_WORKER_RESTART_DELAY = 30  # секунд перед перезапуском упавшего воркера
_WATCHDOG_BEAT = 60  # секунд между heartbeat для systemd WatchdogSec


def make_bot() -> Bot:
    session = AiohttpSession(proxy=settings.tg_proxy) if settings.tg_proxy else None
    return Bot(settings.bot_token, session=session)


def _sd_notify(state: str) -> None:
    """Сигнал в systemd (sd_notify без зависимостей): WATCHDOG=1 держит процесс живым."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(state.encode())
        finally:
            sock.close()
    except OSError:
        log.debug("sd_notify: не удалось отправить %s", state)


async def _heartbeat() -> None:
    """Если процесс завис — systemd не получит WATCHDOG и убьёт его, Restart=always поднимет."""
    while True:
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(_WATCHDOG_BEAT)


async def _supervised(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Воркер не должен умереть молча: любой сбой — лог и перезапуск."""
    while True:
        try:
            await factory()
            log.warning("воркер %s завершился сам — перезапуск через %d сек", name, _WORKER_RESTART_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("воркер %s упал — перезапуск через %d сек", name, _WORKER_RESTART_DELAY)
        await asyncio.sleep(_WORKER_RESTART_DELAY)


async def set_commands_menu(bot: Bot) -> None:
    """Меню команд рядом с полем ввода (кнопка ☰).

    /visits и /users показываются только в чате владельца (scope=Chat) —
    остальные их не видят, а хендлер молча игнорирует чужие запросы.
    """
    common = [
        BotCommand(command="start", description="Старт и выбор группы"),
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
        BotCommand(command="group", description="Выбрать или сменить группу"),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        common + [
            BotCommand(command="visits", description="Кто заходил (только тебе)"),
            BotCommand(command="users", description="Пользователи и группы (только тебе)"),
            BotCommand(command="push", description="Расслать расписание (всем)"),
            BotCommand(command="push_test", description="Расслать расписание (тест)"),
        ],
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
    asyncio.create_task(_heartbeat())
    asyncio.create_task(_supervised("watch_vk", lambda: watch_vk(bot)))
    asyncio.create_task(_supervised("watch_vk_favorites", lambda: watch_vk_favorites(bot)))
    asyncio.create_task(_supervised("watch_web", lambda: watch_web(bot)))
    log.info("Бот запущен, группа %s, беседа VK %s, прокси %s",
             settings.group, settings.vk_chat_peer_id, settings.tg_proxy or "нет")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
