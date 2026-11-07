from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Lesson:
    pair_label: str
    subject: str
    teacher: str = ""
    room: str = ""
    pair_start: int | None = None
    pair_end: int | None = None
    is_free: bool = False
    homework: str = ""

    @property
    def time_range(self) -> str | None:
        if self.pair_start is None:
            return None
        return f"{self.pair_start}" if self.pair_start == self.pair_end else f"{self.pair_start}-{self.pair_end}"


@dataclass
class DaySchedule:
    date: date
    group: str
    lessons: list[Lesson] = field(default_factory=list)
    source_filename: str = ""
