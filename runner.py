"""Один прогон бота (для GitHub Actions по крону): команды Telegram + проверка беседы VK.

Вместо бесконечного поллинга — разовый цикл: забрать апдейты, ответить, проверить VK,
сохранить состояние. Следующий запуск — следующий тик крона.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramConflictError

from bot.handlers import router
from config import settings
from main import make_bot, set_commands_menu
from services.state import State
from services.vk_source import VkError, is_auth_error
from watcher import _TOKEN_ALERT, _check_once

log = logging.getLogger(__name__)


async def run() -> None:
    bot = make_bot()
    dp = Dispatcher()
    dp.include_router(router)

    state = State.load(settings.state_file)
    try:
        await set_commands_menu(bot)
    except TelegramAPIError as exc:
        log.warning("Не удалось обновить меню команд: %s", exc)

    try:
        updates = await bot.get_updates(
            offset=state.tg_offset or None,
            timeout=0,
            limit=100,
            allowed_updates=["message", "callback_query"],
        )
    except TelegramConflictError:
        log.error("Telegram: Conflict — кто-то ещё поллит этого бота (запущен локальный бот?)")
        return

    for update in updates:
        try:
            await dp.feed_update(bot, update)
        except Exception:
            log.exception("Ошибка обработки update %s", update.update_id)

    state = State.load(settings.state_file)  # хендлеры могли записать пользователей
    if updates:
        state.tg_offset = updates[-1].update_id + 1

    try:
        messages = await asyncio.to_thread(_check_once, state)
    except VkError as exc:
        log.error("VK: %s", exc)
        if is_auth_error(exc) and not state.vk_alerted:
            await bot.send_message(settings.chat_id, _TOKEN_ALERT)
            state.vk_alerted = True
        state.save(settings.state_file)
        await bot.session.close()
        return
    state.vk_alerted = False

    for chat_id, text in messages:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    state.save(settings.state_file)
    await bot.session.close()
    log.info("Прогон готов: апдейтов %s, файл VK %s", len(updates), state.last_doc_title or "нет нового")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except Exception as exc:
        log.exception("Прогон упал")
        first = str(exc).splitlines()[0][:150] if str(exc) else ""
        print(f"::error::{type(exc).__name__}: {first}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
