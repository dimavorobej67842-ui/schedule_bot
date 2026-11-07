"""Слежение за беседой VK: новый файл расписания -> сообщение в Telegram."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import settings
from services import pipeline
from services.formatter import format_schedule
from services.state import State
from services.vk_source import VkError, is_auth_error

log = logging.getLogger(__name__)

_TOKEN_ALERT = (
    "⚠️ VK-токен перестал работать (протух или отозван).\n"
    "Обнови VK_TOKEN в .env — как это сделать, в README (раздел «VK-токен»)."
)


def _check_once(state: State) -> str | None:
    """Текст сообщения, если в беседе появился новый .docx (иначе None)."""
    vk = pipeline.make_vk()
    doc = vk.find_latest_docx(min_date=state.last_doc_date)
    if doc is None:
        return None
    log.info("VK: новый файл %r от %d", doc.title, doc.date)
    state.last_doc_date = doc.date
    state.last_doc_title = doc.title

    try:
        path = vk.save(doc)
        schedule = pipeline.load_schedule_from_file(path)
        pipeline.enrich_with_homework(schedule)
        return format_schedule(schedule, pipeline.bells, heading="Новое расписание на")
    except Exception as exc:
        log.exception("Не удалось разобрать файл %r", doc.title)
        return f"⚠️ Новый файл {escape(doc.title)}, но разобрать его не удалось: {escape(str(exc))}"


async def watch_vk(bot: Bot) -> None:
    state = State.load(settings.state_file)
    token_alerted = False
    while True:
        try:
            text = await asyncio.to_thread(_check_once, state)
            token_alerted = False
            if text:
                await bot.send_message(settings.chat_id, text, parse_mode="HTML")
                state.save(settings.state_file)
        except VkError as exc:
            if is_auth_error(exc):
                if not token_alerted:
                    await bot.send_message(settings.chat_id, _TOKEN_ALERT)
                    token_alerted = True
            log.error("VK: %s", exc)
        except TelegramAPIError:
            log.exception("Telegram: не удалось отправить сообщение")
        except Exception:
            log.exception("watcher: ошибка цикла проверки")
        await asyncio.sleep(settings.vk_check_interval)
