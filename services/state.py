"""Простое JSON-состояние: что уже обработано из беседы VK и выбор групп пользователей."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class State:
    last_doc_date: int = 0
    last_doc_title: str = ""
    tg_offset: int = 0  # последний обработанный update_id Telegram
    users: dict[str, str] = field(default_factory=dict)  # telegram id -> группа
    vk_alerted: bool = False  # предупреждение о мёртвом VK-токене уже отправлено

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
        return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def group_of(self, user_id: int | str) -> str | None:
        return self.users.get(str(user_id))
