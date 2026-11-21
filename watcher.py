"""Слежение за беседой VK: новый файл расписания -> сообщение в Telegram."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.handlers import _schedule_keyboard
from config import settings
from services import pdf_bridge, pipeline, web_source
from services.dedup import schedule_fingerprint
from services.formatter import format_schedule
from services.state import State, left_reason, mark_sent, note_left, note_return
from services.vk_source import VkDocument, VkError, VkSource, is_auth_error

log = logging.getLogger(__name__)

_TOKEN_ALERT = (
    "⚠️ VK-токен перестал работать (протух или отозван).\n"
    "Обнови VK_TOKEN: локально — в .env, для GitHub Actions — в секретах репозитория "
    "(Settings → Secrets and variables → Actions). Как получить новый — в README (раздел «VK-токен»)."
)

_SEND_PAUSE = 0.05  # сек между сообщениями: ~20/сек, ниже лимита Telegram (30/сек)
_BATCH_SIZE = 20  # после каждой порции — дополнительная пауза
_BATCH_PAUSE = 1.0


def _build_messages(path, doc_title: str, state: State) -> list[tuple[str, str]]:
    """Сообщения (кому, текст) по новому файлу: владельцу и всем, кто выбрал группу.

    Файл разбирается один раз на группу, а не на каждого пользователя:
    60 юзеров — это 60 подписок, но лишь несколько уникальных групп.

    Чужой файл (не расписание / без групп) пропускается молча — никому
    ничего не пишем, только лог.
    """
    try:
        is_schedule = pipeline.looks_like_schedule(path)
    except Exception as exc:
        log.exception("Не удалось разобрать файл %r", doc_title)
        return [(str(settings.chat_id),
                 f"⚠️ Новый файл {escape(doc_title)}, но разобрать его не удалось: {escape(str(exc))}")]
    if not is_schedule:
        log.info("Файл %r не расписание (нет групп с занятиями) — пропускаем", doc_title)
        return []

    owner_id = str(settings.chat_id)
    recipients: list[tuple[str, str, bool]] = [
        (owner_id, state.group_of(owner_id) or settings.group, True)
    ]
    recipients += [(uid, group, False) for uid, group in state.users.items() if uid != owner_id]

    # пары берём изо всех файлов дня (дневной + «Изменения»), а не только из этого файла
    day = pipeline.file_date(path)

    rendered: dict[tuple[str, bool], str] = {}
    failed: dict[tuple[str, bool], Exception] = {}

    messages: list[tuple[str, str]] = []
    for chat_id, group, with_homework in recipients:
        key = (group, with_homework)
        if key not in rendered and key not in failed:
            try:
                schedule = (pipeline.merged_schedule_for(day, group) if day is not None
                            else pipeline.load_schedule_from_file(path, group))
                if schedule is None or not schedule.lessons:
                    raise ValueError(f"в файле нет занятий для группы «{group}»")
                if with_homework:
                    pipeline.enrich_with_homework(schedule)
                rendered[key] = format_schedule(
                    schedule, pipeline.bells, heading="Новое расписание на"
                )
            except Exception as exc:
                failed[key] = exc
                if with_homework:
                    log.exception("Не удалось разобрать файл %r", doc_title)
                else:
                    log.warning("Не удалось собрать расписание для группы %s: %s", group, exc)
        if key in rendered:
            messages.append((chat_id, rendered[key]))
        elif with_homework:
            messages.append(
                (chat_id, f"⚠️ Новый файл {escape(doc_title)}, но разобрать его не удалось: "
                          f"{escape(str(failed[key]))}")
            )
    return messages


async def _send_many(bot: Bot, messages: list[tuple[str, str]]) -> dict[str, dict]:
    """Отправка порциями: один отказ (заблокированный бот и т.п.) не роняет остальную рассылку.

    Возвращает {uid: {"ok": bool, "reason": str | None}} — для отчёта «кто получил».
    """
    results: dict[str, dict] = {}
    for i, (chat_id, text) in enumerate(messages, start=1):
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML",
                                   reply_markup=_schedule_keyboard())
            note_return(settings.state_file, chat_id)
            results[str(chat_id)] = {"ok": True, "reason": None}
        except TelegramAPIError as exc:
            log.warning("Не доставлено %s: %s", chat_id, exc)
            reason = left_reason(exc)
            if reason:
                note_left(settings.state_file, chat_id, reason)
            results[str(chat_id)] = {"ok": False, "reason": reason or str(exc)}
        if i % _BATCH_SIZE == 0:
            await asyncio.sleep(_BATCH_PAUSE)
        else:
            await asyncio.sleep(_SEND_PAUSE)
    return results


def _process_file(path, doc_title: str, state: State, source: str, *,
                  force: bool = False, owner_only: bool = False) -> list[tuple[str, str]]:
    """Дедуп между источниками + сборка сообщений по файлу.

    Одно и то же расписание, пришедшее сначала в VK, а потом на сайт (или
    наоборот), второй раз никому не рассылается. Отличающаяся версия на ту же
    дату уходит только владельцу — как возможная правка.
    """
    try:
        is_schedule = pipeline.looks_like_schedule(path)
    except Exception as exc:
        log.exception("Не удалось разобрать файл %r", doc_title)
        return [(str(settings.chat_id),
                 f"⚠️ Новый файл {escape(doc_title)}, но разобрать его не удалось: {escape(str(exc))}")]
    if not is_schedule:
        log.info("Файл %r не расписание (нет групп с занятиями) — пропускаем", doc_title)
        return []

    fp = date_str = ""
    try:
        fp, date_str = schedule_fingerprint(path)
    except Exception:
        log.exception("Не удалось посчитать отпечаток %r", doc_title)

    if not force:
        sent = state.sent_schedules
        if fp and any(m.get("fp") == fp for m in sent.values()):
            log.info("%s: %r — уже рассылано (отпечаток %s), дубликат не шлём",
                     source, doc_title, fp)
            return []
        if date_str and date_str in sent:
            prev = sent[date_str]
            log.info("%s: %r — расписание на %s уже рассылано (источник %s), дубликат не шлём",
                     source, doc_title, date_str, prev.get("source"))
            if prev.get("fp") != fp:
                return [(str(settings.chat_id),
                         f"ℹ️ {escape(doc_title)}: расписание на {date_str} уже разослано "
                         f"(источник: {escape(str(prev.get('source', '?')))}), но содержимое "
                         f"отличается. Массово не шлю — проверь, это правка?")]
            return []

    messages = _build_messages(path, doc_title, state)
    if owner_only:
        owner_id = str(settings.chat_id)
        messages = [(uid, "🧪 Тестовая рассылка (сайт knt-oil.ru)\n\n" + text)
                    for uid, text in messages if uid == owner_id]
    elif messages and fp:
        # фиксируем ДО отправки: повторная рассылка всем хуже, чем один пропуск
        mark_sent(settings.state_file, date_str, fp, source, doc_title)
    return messages


def _check_once(state: State) -> list[tuple[str, str]] | None:
    """None — нового файла нет; список (может быть пустым) — файл обработан или пропущен."""
    vk = pipeline.make_vk()
    doc = vk.find_latest_docx(min_date=state.last_doc_date)
    if doc is None:
        return None
    log.info("VK: новый файл %r от %d", doc.title, doc.date)
    state.last_doc_date = doc.date
    state.last_doc_title = doc.title

    try:
        path = vk.save(doc)
    except Exception:
        log.exception("Не удалось сохранить файл %r", doc.title)
        return [(str(settings.chat_id), f"⚠️ Новый файл {escape(doc.title)}, но сохранить его не удалось.")]
    return _process_file(path, doc.title, state, source="vk")


def _mark_doc_processed(state: State) -> None:
    """Зафиксировать файл обработанным, не затирая регистрации во время рассылки."""
    def _apply(s: State) -> None:
        s.last_doc_date = state.last_doc_date
        s.last_doc_title = state.last_doc_title
    State.update(settings.state_file, _apply)


def _check_web_once(state: State, *, force: bool = False,
                    owner_only: bool = False) -> list[tuple[str, str]] | None:
    """None — на сайте ничего нового; список (может быть пустым) — файл обработан."""
    got = web_source.fetch_changes("" if force else state.web_pdf_etag)
    if got is None:
        return None
    data, pdf_hash, etag = got
    if not force and pdf_hash == state.web_pdf_hash:
        if etag and etag != state.web_pdf_etag:
            State.update(settings.state_file, lambda s: setattr(s, "web_pdf_etag", etag))
        return None
    log.info("Сайт: новый PDF (%d байт, sha %s)", len(data), pdf_hash[:12])
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = settings.download_dir / "web_izmenenie.pdf"
    pdf_path.write_bytes(data)
    try:
        docx_path = settings.download_dir / f"web_izmenenie_{pdf_hash[:8]}.docx"
        pdf_bridge.pdf_to_docx(pdf_path, docx_path)
    except Exception:
        # хеш не фиксируем — при следующем цикле попробуем снова
        log.exception("Сайт: не удалось сконвертировать PDF")
        return None
    def _apply(s: State) -> None:
        s.web_pdf_hash = pdf_hash
        if etag:
            s.web_pdf_etag = etag
    State.update(settings.state_file, _apply)
    return _process_file(docx_path, "izmenenie.pdf (сайт)", state, source="сайт",
                         force=force, owner_only=owner_only)


async def watch_web(bot: Bot, *, once: bool = False, force: bool = False,
                    owner_only: bool = False) -> None:
    """Сайт КНТ: «Изменения к основному расписанию» — запасной источник рядом с VK.

    once/force/owner_only — для тестового прогона: один цикл, без учёта дедупа,
    сообщения только владельцу.
    """
    while True:
        try:
            # state перечитывается каждый цикл: handlers пишут в тот же файл
            state = State.load(settings.state_file)
            messages = await asyncio.to_thread(_check_web_once, state,
                                               force=force, owner_only=owner_only)
            if messages:
                await _send_many(bot, messages)
        except TelegramAPIError:
            log.exception("Telegram: не удалось отправить сообщение (сайт)")
        except Exception:
            log.exception("watcher-сайт: ошибка цикла проверки")
        if once:
            return
        await asyncio.sleep(settings.web_check_interval)


async def watch_vk(bot: Bot) -> None:
    token_alerted = False
    while True:
        try:
            # state перечитывается каждый цикл: handlers пишут новых пользователей в тот же файл
            state = State.load(settings.state_file)
            messages = await asyncio.to_thread(_check_once, state)
            token_alerted = False
            if messages is not None:
                # файл помечаем обработанным ДО рассылки: повторные сообщения всем
                # хуже, чем один пропуск (догонит через /today). Пропущенные файлы
                # (не расписание) тоже фиксируем — иначе они будут разбираться каждый цикл.
                _mark_doc_processed(state)
                if messages:
                    await _send_many(bot, messages)
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
                text: str | None
                try:
                    if not await asyncio.to_thread(pipeline.looks_like_schedule, path):
                        log.info("VK-избранное: %r — не расписание, пропускаем", doc.title)
                        text = None
                    else:
                        schedule = await asyncio.to_thread(pipeline.load_schedule_from_file,
                                                           path, settings.group)
                        if not schedule.lessons:
                            raise ValueError(f"в файле нет занятий для группы «{settings.group}»")
                        await asyncio.to_thread(pipeline.enrich_with_homework, schedule)
                        text = format_schedule(schedule, pipeline.bells,
                                               heading="Тест из Избранного — расписание на")
                except ValueError as exc:
                    text = f"⚠️ {escape(doc.title)}: {escape(str(exc))}"
                except Exception:
                    log.exception("Избранное: не удалось разобрать %r", doc.title)
                    text = f"⚠️ Файл {escape(doc.title)} разобрать не удалось — подробности в логах."
                if text is not None:
                    await bot.send_message(settings.chat_id, text, parse_mode="HTML",
                                           reply_markup=_schedule_keyboard())
                # сохранить только поля избранного, чтобы не затереть остальное;
                # пропускаемые файлы тоже фиксируем — иначе они крутятся каждый цикл
                State.update(
                    settings.state_file,
                    lambda s: (setattr(s, "fav_doc_date", doc.date),
                               setattr(s, "fav_doc_title", doc.title)),
                )
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
