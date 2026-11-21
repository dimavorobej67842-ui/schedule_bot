"""Забор файлов расписания из беседы VK (юзер-токен)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/{method}"
API_VERSION = "5.199"
ALLOWED_EXT = (".docx",)


class VkError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


AUTH_ERROR_CODES = {5, 1117, 1114}  # невалидный/протухший токен


def is_auth_error(exc: VkError) -> bool:
    return exc.error_code in AUTH_ERROR_CODES


# Ошибка 9 (flood control) — VK просит сбавить темп. Быстрые повторы (2/4/8 сек)
# от каждого ватчера и каждого нажатия кнопки только продлевают штраф: встать в
# общую паузу, во время которой ВСЕ вызовы API отваливаются без сетевых запросов.
# Повторный [9] подряд удваивает паузу — чтобы редкие пробы не продлевали штраф VK.
_FLOOD_PAUSE = 300.0  # первый штраф — 5 минут
_FLOOD_PAUSE_MAX = 7200.0  # потолок — 2 часа
_flood_lock = threading.Lock()
_flood_until = 0.0  # time.monotonic(): до какого момента API не дёргаем
_flood_streak = 0  # сколько [9] подряд без успешного вызова

# Тела файлов беседы неизменны — кэш по message_id, чтобы каждое нажатие кнопки
# не перекачивало один и тот же .docx.
_content_lock = threading.Lock()
_content_cache: dict[int, bytes] = {}


def _check_flood(method: str) -> None:
    """VkError вместо сетевого запроса, если VK недавно просил паузу."""
    with _flood_lock:
        left = _flood_until - time.monotonic()
    if left > 0:
        if left >= 120:
            pause_txt = f"{int(left // 60)} мин"
        else:
            pause_txt = f"{int(left)} сек"
        raise VkError(
            f"VK API {method}: флуд-контроль VK, пауза ещё {pause_txt}",
            error_code=9,
        )


def _enter_flood() -> None:
    global _flood_until, _flood_streak
    with _flood_lock:
        _flood_streak += 1
        pause = min(_FLOOD_PAUSE * (2 ** (_flood_streak - 1)), _FLOOD_PAUSE_MAX)
        _flood_until = time.monotonic() + pause
        streak = _flood_streak
    log.warning("VK: флуд-контроль (серия %d) — все вызовы API на паузе %d сек",
                streak, int(pause))


def _note_ok() -> None:
    global _flood_streak
    with _flood_lock:
        _flood_streak = 0


@dataclass
class VkDocInfo:
    """Метаданные .docx без скачивания тела файла."""

    title: str
    date: int  # unix time сообщения
    message_id: int
    url: str


@dataclass
class VkDocument:
    title: str
    content: bytes
    date: int  # unix time сообщения
    message_id: int


class VkSource:
    def __init__(self, token: str, peer_id: int, download_dir: Path | None = None):
        self.token = token
        self.peer_id = peer_id
        self.download_dir = download_dir

    # 1 «unknown», 6 «too many», 10 «internal» — лечится повтором;
    # 9 «flood» — НЕ повторяем, встаём в общую паузу (_enter_flood);
    # ошибки авторизации (5/1117/1114) тоже не повторяем — по ним нужен новый токен
    _RETRY_CODES = {1, 6, 10}
    _RETRIES = 4

    def _call(self, method: str, **params) -> dict:
        params.update(access_token=self.token, v=API_VERSION)
        url = API_URL.format(method=method)
        last_exc: Exception | None = None
        for attempt in range(1, self._RETRIES + 1):
            _check_flood(method)
            try:
                resp = requests.post(url, data=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt == self._RETRIES:
                    break
                delay = 2**attempt
                log.warning("VK %s: %s — повтор через %d сек", method, exc, delay)
                time.sleep(delay)
                continue
            if "error" in data:
                err = data["error"]
                code = err.get("error_code")
                if code == 9:
                    _enter_flood()
                    raise VkError(
                        f"VK API {method}: [{code}] {err.get('error_msg')}",
                        error_code=code,
                    )
                if code in self._RETRY_CODES and attempt < self._RETRIES:
                    delay = 2**attempt
                    log.warning("VK %s: [%s] %s — повтор через %d сек",
                                method, code, err.get("error_msg"), delay)
                    time.sleep(delay)
                    continue
                raise VkError(
                    f"VK API {method}: [{code}] {err.get('error_msg')}",
                    error_code=code,
                )
            _note_ok()
            return data["response"]
        raise VkError(f"VK API {method}: не удалось после {self._RETRIES} попыток: {last_exc}")

    def list_docx(self, min_date: int = 0) -> list[VkDocInfo]:
        """Метаданные всех .docx строго новее min_date, от старых к новым."""
        response = self._call("messages.getHistory", peer_id=self.peer_id, count=50)
        found: list[VkDocInfo] = []
        for msg in response.get("items", []):
            msg_date = msg.get("date", 0)
            if msg_date <= min_date:
                break
            for att in msg.get("attachments", []):
                if att.get("type") != "doc":
                    continue
                doc = att["doc"]
                title = doc.get("title") or ""
                if not title.lower().endswith(ALLOWED_EXT):
                    continue
                found.append(VkDocInfo(
                    title=title,
                    date=msg_date,
                    message_id=msg.get("id", 0),
                    url=doc["url"],
                ))
                break
        found.sort(key=lambda d: d.date)
        return found

    def fetch(self, info: VkDocInfo) -> VkDocument:
        """Скачать тело файла по метаданным."""
        return VkDocument(
            title=info.title,
            content=self._content(info.message_id, info.url),
            date=info.date,
            message_id=info.message_id,
        )

    def find_latest_docx(self, min_date: int = 0) -> VkDocument | None:
        """Самый свежий .docx в беседе строго новее min_date (unix seconds)."""
        response = self._call("messages.getHistory", peer_id=self.peer_id, count=50)
        for msg in response.get("items", []):
            msg_date = msg.get("date", 0)
            if msg_date <= min_date:
                break
            for att in msg.get("attachments", []):
                if att.get("type") != "doc":
                    continue
                doc = att["doc"]
                title = doc.get("title") or ""
                if not title.lower().endswith(ALLOWED_EXT):
                    continue
                content = self._content(msg.get("id", 0), doc["url"])
                return VkDocument(
                    title=title,
                    content=content,
                    date=msg_date,
                    message_id=msg.get("id", 0),
                )
        return None

    def find_new_docx(self, min_date: int = 0) -> list[VkDocument]:
        """Все .docx строго новее min_date (unix seconds), от старых к новым."""
        response = self._call("messages.getHistory", peer_id=self.peer_id, count=50)
        found: list[VkDocument] = []
        for msg in response.get("items", []):
            msg_date = msg.get("date", 0)
            if msg_date <= min_date:
                break
            for att in msg.get("attachments", []):
                if att.get("type") != "doc":
                    continue
                doc = att["doc"]
                title = doc.get("title") or ""
                if not title.lower().endswith(ALLOWED_EXT):
                    continue
                found.append(VkDocument(
                    title=title,
                    content=self._content(msg.get("id", 0), doc["url"]),
                    date=msg_date,
                    message_id=msg.get("id", 0),
                ))
                break
        found.sort(key=lambda d: d.date)
        return found

    def _content(self, message_id: int, url: str) -> bytes:
        """Тело файла через кэш: файл в беседе неизменен, перекачивать каждый раз незачем."""
        with _content_lock:
            cached = _content_cache.get(message_id)
        if cached is not None:
            return cached
        content = self._download(url)
        with _content_lock:
            _content_cache[message_id] = content
        return content

    def _download(self, url: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(1, self._RETRIES + 1):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                return resp.content
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == self._RETRIES:
                    break
                delay = 2**attempt
                log.warning("VK: не скачался файл (%s) — повтор через %d сек", exc, delay)
                time.sleep(delay)
        raise VkError(f"VK: не удалось скачать файл после {self._RETRIES} попыток: {last_exc}")

    def save(self, doc: VkDocument) -> Path:
        assert self.download_dir is not None
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / Path(doc.title).name
        path.write_bytes(doc.content)
        log.info("VK: файл сохранён %s (%d байт)", path, len(doc.content))
        return path
