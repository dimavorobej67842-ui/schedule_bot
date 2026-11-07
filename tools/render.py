"""Показать, как будет выглядеть сообщение из локального файла расписания.

Запуск: python tools/render.py путь\к\файлу.docx [группа]
Парсит файл, подставляет ДЗ из ЭлЖура (если настроен) и печатает текст сообщения.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from services.pipeline import bells, enrich_with_homework, load_schedule_from_file  # noqa: E402
from services.formatter import format_schedule  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    group = sys.argv[2] if len(sys.argv) > 2 else settings.group
    schedule = load_schedule_from_file(path, group)
    enrich_with_homework(schedule)
    print(f"Дата из имени файла: {schedule.date:%d.%m.%Y}\n")
    print(format_schedule(schedule, bells))


if __name__ == "__main__":
    main()
