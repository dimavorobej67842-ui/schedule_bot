"""Тестовый прогон источника «сайт»: один цикл, сообщение только владельцу.

Дедуп обходится (force), чтобы показать само сообщение; отметка «разослано»
при этом не ставится — боевой watcher продолжит считать день неразосланным.
"""
from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "/opt/schedule_bot")

from main import make_bot  # noqa: E402
import watcher  # noqa: E402


async def run() -> None:
    bot = make_bot()
    try:
        await watcher.watch_web(bot, once=True, force=True, owner_only=True)
    finally:
        await bot.session.close()
    print("OK: тестовая рассылка завершена")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
