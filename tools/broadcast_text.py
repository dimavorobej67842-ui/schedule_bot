"""Рассылка текстового сообщения всем пользователям бота.

Запуск: python tools/broadcast_text.py "текст сообщения"
Кому: все из state.json (включая владельца). Заблокировавших бота помечает в state.left.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.exceptions import TelegramAPIError  # noqa: E402

from config import settings  # noqa: E402
from main import make_bot  # noqa: E402
from services.state import State, left_reason, note_left, note_return  # noqa: E402

_SEND_PAUSE = 0.05
_BATCH_SIZE = 20
_BATCH_PAUSE = 1.0


async def run(text: str) -> None:
    state = State.load(settings.state_file)
    uids = sorted(state.users)
    if not uids:
        sys.exit("FAIL: в state.json нет пользователей")
    bot = make_bot()
    sent = failed = 0
    try:
        for i, uid in enumerate(uids, start=1):
            try:
                await bot.send_message(uid, text)
                note_return(settings.state_file, uid)
                sent += 1
            except TelegramAPIError as exc:
                failed += 1
                print(f"не доставлено {uid}: {exc}")
                reason = left_reason(exc)
                if reason:
                    note_left(settings.state_file, uid, reason)
            if i % _BATCH_SIZE == 0:
                await asyncio.sleep(_BATCH_PAUSE)
            else:
                await asyncio.sleep(_SEND_PAUSE)
    finally:
        await bot.session.close()
    print(f"OK: отправлено {sent}, не доставлено {failed}, всего {len(uids)}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python tools/broadcast_text.py \"текст сообщения\"")
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"FAIL: {type(exc).__name__}: {exc}")
