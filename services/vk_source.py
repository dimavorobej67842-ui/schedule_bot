"""Забор файлов расписания из беседы VK (юзер-токен)."""

from __future__ import annotations

import logging
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

    def _call(self, method: str, **params) -> dict:
        params.update(access_token=self.token, v=API_VERSION)
        resp = requests.post(API_URL.format(method=method), data=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise VkError(
                f"VK API {method}: [{err.get('error_code')}] {err.get('error_msg')}",
                error_code=err.get("error_code"),
            )
        return data["response"]

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
                content = self._download(doc["url"])
                return VkDocument(
                    title=title,
                    content=content,
                    date=msg_date,
                    message_id=msg.get("id", 0),
                )
        return None

    def _download(self, url: str) -> bytes:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def save(self, doc: VkDocument) -> Path:
        assert self.download_dir is not None
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / Path(doc.title).name
        path.write_bytes(doc.content)
        log.info("VK: файл сохранён %s (%d байт)", path, len(doc.content))
        return path
