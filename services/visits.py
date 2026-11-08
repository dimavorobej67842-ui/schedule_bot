"""Лог заходов в бота (JSONL) и сводка по пользователям для админ-команды."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aiogram.types import CallbackQuery, Message, User

from services.state import State


def _describe_action(event: Message | CallbackQuery) -> str:
    if isinstance(event, CallbackQuery):
        return "btn:" + (event.data or "")
    return event.text or ""


def record(event: Message | CallbackQuery, visits_file: Path) -> None:
    """Дописать одно действие пользователя в JSONL-лог."""
    user: User | None = event.from_user
    if user is None:
        return
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user_id": user.id,
        "name": " ".join(filter(None, [user.first_name or "", user.last_name or ""])).strip(),
        "username": user.username or "",
        "action": _describe_action(event),
    }
    with visits_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summary(state: State, visits_file: Path) -> tuple[list[dict], int]:
    """Сводка по пользователям (сортировка: последние заходы сверху) + всего действий.

    Участники без записей в логе (регистрация была до внедрения лога) тоже входят.
    """
    users: dict[int, dict] = {}
    total_actions = 0

    if visits_file.exists():
        for line in visits_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                uid = int(e["user_id"])
            except (ValueError, KeyError, TypeError):
                continue
            total_actions += 1
            u = users.setdefault(
                uid,
                {"user_id": uid, "name": "", "username": "", "first_ts": "", "last_ts": "", "count": 0},
            )
            u["count"] += 1
            if e.get("name"):
                u["name"] = e["name"]
            if e.get("username"):
                u["username"] = e["username"]
            ts = e.get("ts", "")
            if ts and (not u["first_ts"] or ts < u["first_ts"]):
                u["first_ts"] = ts
            if ts and ts > u["last_ts"]:
                u["last_ts"] = ts

    for uid_str, group in state.users.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        u = users.setdefault(
            uid,
            {"user_id": uid, "name": "", "username": "", "first_ts": "", "last_ts": "", "count": 0},
        )

    rows = sorted(users.values(), key=lambda u: (u["last_ts"] or "", u["count"]), reverse=True)
    for u in rows:
        u["group"] = state.group_of(u["user_id"]) or ""
    return rows, total_actions
