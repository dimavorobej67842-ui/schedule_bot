"""Сайт КНТ (knt-oil.ru): файл «Изменения к основному расписанию».

Файл лежит по постоянному URL и перезаписывается при каждом обновлении —
новизна определяется по хешу содержимого, не по имени. Пока файл не менялся,
сервер отвечает 304 по If-None-Match — тело не качается.
"""
from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

CHANGES_URL = "https://knt-oil.ru/wp-content/uploads/2023/03/izmenenie.pdf"
_TIMEOUT = 30


def fetch_changes(etag: str = "") -> tuple[bytes, str, str] | None:
    """(содержимое PDF, sha256, etag) или None: нет изменений / сайт недоступен.

    С переданным etag уходит условный запрос: пока файл тот же, сервер отдаёт
    304 без тела — экономия трафика с обеих сторон.
    """
    try:
        headers = {"User-Agent": "schedule_bot/1.0"}
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(CHANGES_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
            new_etag = resp.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        log.exception("Сайт: ошибка %s при загрузке %s", exc.code, CHANGES_URL)
        return None
    except Exception:
        log.exception("Сайт: не удалось скачать %s", CHANGES_URL)
        return None
    if not data.startswith(b"%PDF"):
        log.warning("Сайт: ответ не PDF (%d байт)", len(data))
        return None
    return data, hashlib.sha256(data).hexdigest(), new_etag
