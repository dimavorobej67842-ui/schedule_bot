"""Простое JSON-состояние: что уже обработано из беседы VK и выбор групп пользователей."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

# Все записи сериализуются: параллельный read-modify-write (handlers + watcher)
# не должен терять регистрации пользователей и не должен откатывать last_doc,
# иначе у всех уйдёт повторная рассылка.
_LOCK = threading.Lock()


@dataclass
class State:
    last_doc_date: int = 0
    last_doc_title: str = ""
    fav_doc_date: int = 0  # последний обработанный файл из «Избранного» VK
    fav_doc_title: str = ""
    tg_offset: int = 0  # последний обработанный update_id Telegram
    users: dict[str, str] = field(default_factory=dict)  # telegram id -> группа
    known_groups: list[str] = field(default_factory=list)  # все группы, виденные в файлах
    left: dict[str, str] = field(default_factory=dict)  # telegram id -> "дд.мм.гггг чч:мм · причина"
    vk_alerted: bool = False  # предупреждение о мёртвом VK-токене уже отправлено
    greeted: list[str] = field(default_factory=list)  # кому уже присылали приветствие
    # дата из штампа ("ГГГГ-ММ-ДД") -> {fp, source, title, ts}: что уже разослали.
    # Один и тот же день из VK и с сайта не должен дать вторую рассылку.
    sent_schedules: dict[str, dict] = field(default_factory=dict)
    web_pdf_hash: str = ""  # sha256 последнего обработанного PDF с сайта КНТ
    web_pdf_etag: str = ""  # ETag файла с сайта — условный запрос, чтобы не качать тело зря
    # последний файл, скинутый владельцем в чат: {path, title, date, week, push_id, sent, ts}
    pending_push: dict = field(default_factory=dict)
    # недоставленные после рассылки — кнопка «Дослать»: {path, title, date, uids: {uid: причина}, ts}
    pending_resend: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
        return cls()

    def save(self, path: Path) -> None:
        with _LOCK:
            self._write(path)

    def _write(self, path: Path) -> None:
        """Атомарная запись: при падении посередине остаётся старый целый файл."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def update(cls, path: Path, mutate) -> "State":
        """Атомарный read-modify-write: mutate(state) применяется под замком."""
        with _LOCK:
            st = cls.load(path)
            mutate(st)
            st._write(path)
            return st

    def group_of(self, user_id: int | str) -> str | None:
        return self.users.get(str(user_id))


def left_reason(exc: Exception) -> str | None:
    """Причина «пользователь вышел из бота» по тексту ошибки Telegram; None — не про то.

    Блок бота («bot was blocked by the user») и удалённый/отключённый аккаунт
    («user is deactivated» / «chat not found») — единственные стабильные сигналы ухода.
    """
    text = str(exc).lower()
    if "bot was blocked" in text:
        return "заблокировал бота"
    if "user is deactivated" in text or "chat not found" in text:
        return "удалил аккаунт"
    return None


def note_left(path: Path, user_id: int | str, reason: str) -> None:
    """Запомнить, что пользователь вышел из бота (когда и почему)."""
    stamp = f"{datetime.now():%d.%m.%Y %H:%M} · {reason}"
    State.update(path, lambda s: s.left.__setitem__(str(user_id), stamp))


def note_return(path: Path, user_id: int | str) -> None:
    """Пользователь снова активен — снять отметку об уходе (если была)."""
    uid = str(user_id)
    if uid in State.load(path).left:
        State.update(path, lambda s: s.left.pop(uid, None))


def mark_sent(path: Path, date_str: str, fp: str, source: str, title: str) -> None:
    """Зафиксировать рассылку расписания на дату — защита от дублей между источниками."""
    stamp = f"{datetime.now():%d.%m.%Y %H:%M}"
    State.update(
        path,
        lambda s: s.sent_schedules.__setitem__(date_str, {
            "fp": fp, "source": source, "title": title, "ts": stamp,
        }),
    )
