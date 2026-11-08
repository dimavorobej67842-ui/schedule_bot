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
from services.vk_source import VkDocument, VkError, VkSource, is_auth_error

log = logging.getLogger(__name__)

_TOKEN_ALERT = (
    "⚠️ VK-токен перестал работать (протух или отозван).\n"
    "Обнови VK_TOKEN: локально — в .env, для GitHub Actions — в секретах репозитория "
    "(Settings → Secrets and variables → Actions). Как получить новый — в README (раздел «VK-токен»)."
)


def _build_messages(path, doc_title: str, state: State) -> list[tuple[str, str]]:
    """Сообщения (кому, текст) по новому файлу: владельцу и всем, кто выбрал группу."""
    owner_id = str(settings.chat_id)
    recipients: list[tuple[str, str, bool]] = [
        (owner_id, state.group_of(owner_id) or settings.group, True)
    ]
    recipients += [(uid, group, False) for uid, group in state.users.items() if uid != owner_id]

    messages: list[tuple[str, str]] = []
    for chat_id, group, with_homework in recipients:
        try:
            schedule = pipeline.load_schedule_from_file(path, group)
            if with_homework:
                pipeline.enrich_with_homework(schedule)
            text = format_schedule(schedule, pipeline.bells, heading="Новое расписание на")
        except Exception as exc:
            if with_homework:
                log.exception("Не удалось разобрать файл %r", doc_title)
                messages.append(
                    (chat_id, f"⚠️ Новый файл {escape(doc_title)}, но разобрать его не удалось: {escape(str(exc))}")
                )
            else:
                log.warning("Не удалось собрать расписание для %s (группа %s): %s", chat_id, group, exc)
            continue
        messages.append((chat_id, text))
    return messages


def _check_once(state: State) -> list[tuple[str, str]]:
    """Сообщения (кому, текст), если в беседе появился новый .docx (иначе пусто)."""
    vk = pipeline.make_vk()
    doc = vk.find_latest_docx(min_date=state.last_doc_date)
    if doc is None:
        return []
    log.info("VK: новый файл %r от %d", doc.title, doc.date)
    state.last_doc_date = doc.date
    state.last_doc_title = doc.title

    try:
        path = vk.save(doc)
    except Exception:
        log.exception("Не удалось сохранить файл %r", doc.title)
        return [(str(settings.chat_id), f"⚠️ Новый файл {escape(doc.title)}, но сохранить его не удалось.")]
    return _build_messages(path, doc.title, state)


async def watch_vk(bot: Bot) -> None:
    token_alerted = False
    while True:
        try:
            # state перечитывается каждый цикл: handlers пишут новых пользователей в тот же файл
            state = State.load(settings.state_file)
            messages = await asyncio.to_thread(_check_once, state)
            token_alerted = False
            if messages:
                for chat_id, text in messages:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                # сохранить только поля файла, чтобы не затереть регистрации во время рассылки
                fresh = State.load(settings.state_file)
                fresh.last_doc_date = state.last_doc_date
                fresh.last_doc_title = state.last_doc_title
                fresh.save(settings.state_file)
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


async def watch_vk_favorites(bot: Bot) -> None:
    """«Избранное» VK: новый .docx -> расписание ТОЛЬКО владельцу (канал для проверок)."""
    token_alerted = False
    while True:
        try:
            # state перечитывается каждый цикл: не затирать регистрации и поля основного watcher
            state = State.load(settings.state_file)
            found: list[tuple[VkSource, VkDocument]] = []
            for peer_id in settings.fav_peer_ids:
                vk = VkSource(settings.vk_token, peer_id, settings.download_dir)
                found += [(vk, d) for d in await asyncio.to_thread(vk.find_new_docx, state.fav_doc_date)]
            found.sort(key=lambda t: t[1].date)
            token_alerted = False
            for vk, doc in found:
                log.info("VK-избранное: новый файл %r от %d", doc.title, doc.date)
                path = await asyncio.to_thread(vk.save, doc)
                try:
                    schedule = await asyncio.to_thread(pipeline.load_schedule_from_file,
                                                       path, settings.group)
                    await asyncio.to_thread(pipeline.enrich_with_homework, schedule)
                    text = format_schedule(schedule, pipeline.bells,
                                           heading="Тест из Избранного — расписание на")
                except ValueError as exc:
                    text = f"⚠️ {escape(doc.title)}: {escape(str(exc))}"
                except Exception:
                    log.exception("Избранное: не удалось разобрать %r", doc.title)
                    text = f"⚠️ Файл {escape(doc.title)} разобрать не удалось — подробности в логах."
                await bot.send_message(settings.chat_id, text, parse_mode="HTML")
                # сохранить только поля избранного, чтобы не затереть остальное
                fresh = State.load(settings.state_file)
                fresh.fav_doc_date = doc.date
                fresh.fav_doc_title = doc.title
                fresh.save(settings.state_file)
        except VkError as exc:
            if is_auth_error(exc):
                if not token_alerted:
                    await bot.send_message(settings.chat_id, _TOKEN_ALERT)
                    token_alerted = True
            log.error("VK-избранное: %s", exc)
        except TelegramAPIError:
            log.exception("Telegram: не удалось отправить сообщение (избранное)")
        except Exception:
            log.exception("watcher-избранное: ошибка цикла проверки")
        await asyncio.sleep(60)  # Избранное проверяем чаще: это канал для быстрых проверок
