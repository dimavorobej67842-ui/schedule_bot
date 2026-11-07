"""Проверка связи с Telegram через прокси из .env (TG_PROXY).

Запуск: python tools/tg_check.py
Печатает getMe; с флагом --send шлёт тестовое сообщение в CHAT_ID.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from main import make_bot  # noqa: E402


async def run(send: bool) -> None:
    bot = make_bot()
    me = await bot.get_me()
    print(f"OK: бот @{me.username} ({me.first_name})")
    if send:
        await bot.send_message(settings.chat_id, "✅ schedule-bot: связь через прокси работает")
        print(f"Тестовое сообщение отправлено в chat_id={settings.chat_id}")
    await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run(send="--send" in sys.argv))
    except Exception as exc:
        sys.exit(f"FAIL: {type(exc).__name__}: {exc}")
