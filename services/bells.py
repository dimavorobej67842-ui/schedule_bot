"""Звонки: три графика (пн / вт-пт / сб) из bells.json, выбор по дню недели."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Bell:
    start: str
    end: str
    title: str = ""

    @property
    def range(self) -> str:
        return f"{self.start}–{self.end}"


@dataclass
class DayBells:
    by_pair: dict[int, Bell]
    talks: Bell | None

    def for_range(self, start: int | None, end: int | None) -> str | None:
        """Время для диапазона пар: начало первой — конец последней."""
        if start is None or end is None:
            return None
        first, last = self.by_pair.get(start), self.by_pair.get(end)
        if not first or not last:
            return None
        return f"{first.start}–{last.end}"

    def table(self, pairs: set[int], with_talks: bool) -> list[str]:
        """Только звонки, нужные студенту: по числу его пар (+ «Разговоры», если есть)."""
        lines = []
        if with_talks and self.talks:
            lines.append(f"{self.talks.title or 'Разговоры о важном'} — {self.talks.range}")
        for num in sorted(pairs):
            bell = self.by_pair.get(num)
            if bell:
                lines.append(f"{num} пара — {bell.range}")
        return lines


class Bells:
    def __init__(self, path: str | Path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self._days: dict[str, DayBells] = {}
        for key in ("monday", "tue_fri", "saturday"):
            by_pair: dict[int, Bell] = {}
            talks: Bell | None = None
            for name, val in (raw.get(key) or {}).items():
                if not isinstance(val, dict):
                    continue
                bell = Bell(start=val["start"], end=val["end"], title=val.get("title", ""))
                if name == "talks":
                    talks = bell
                else:
                    by_pair[int(name)] = bell
            self._days[key] = DayBells(by_pair=by_pair, talks=talks)

    def for_weekday(self, weekday: int) -> DayBells:
        # 0=пн .. 6=вс; занятий в воскресенье нет, но на всякий случай берём «вт-пт»
        if weekday == 0:
            key = "monday"
        elif weekday == 5:
            key = "saturday"
        else:
            key = "tue_fri"
        return self._days[key]
