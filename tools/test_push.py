"""Тест-рассылка расписания ТОЛЬКО владельцу (CHAT_ID из .env).

Запуск: python tools/test_push.py [путь\к\файлу.docx] [группа]
Без аргументов берёт самый свежий файл из беседы VK. Никому больше не шлёт.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from main import make_bot  # noqa: E402
from services.formatter import format_schedule  # noqa: E402
from services.pipeline import bells, enrich_with_homework, load_schedule_from_file, render_latest  # noqa: E402


async def run(path: Path | None, group: str | None) -> None:
    bot = make_bot()
    try:
        if path is not None:
            schedule = load_schedule_from_file(path, group)
            enrich_with_homework(schedule)
            text = format_schedule(schedule, bells, heading="Тест рассылки — расписание на")
        else:
            text = render_latest(group, with_homework=True,
                                 heading="Тест рассылки — расписание на")
        await bot.send_message(settings.chat_id, text, parse_mode="HTML")
        print(f"OK: тестовое расписание отправлено владельцу chat_id={settings.chat_id}")
    finally:
        await bot.session.close()


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    group = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(run(path, group))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"FAIL: {type(exc).__name__}: {exc}")
