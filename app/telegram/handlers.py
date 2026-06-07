"""aiogram handlers: commands, document/photo uploads, and inline callbacks.

Blocking analyzer work runs via ``asyncio.to_thread`` so the bot's event loop
(shared with the FastAPI web server) stays responsive.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PhotoSize

from app.core.config import settings
from app.telegram import presenter, service

log = logging.getLogger(__name__)

router = Router(name="statement-analyzer")


def _is_allowed(user_id: int | None) -> bool:
    allowed = settings.telegram_allowed_user_ids
    if not allowed:  # empty = open to everyone
        return True
    return user_id in allowed


async def _deny(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("⛔ Доступ запрещён.", show_alert=True)
    else:
        await event.answer("⛔ Доступ запрещён.")


def _uid(event: Message | CallbackQuery) -> int | None:
    return event.from_user.id if event.from_user else None


# ── Commands ─────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(_uid(message)):
        await _deny(message)
        return
    await message.answer(presenter.welcome_text(), reply_markup=presenter.start_keyboard())


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not _is_allowed(_uid(message)):
        await _deny(message)
        return
    sessions = await asyncio.to_thread(service.recent_sessions, 12)
    await message.answer(
        presenter.history_text(sessions),
        reply_markup=presenter.history_keyboard(sessions) if sessions else None,
    )


# ── Uploads ──────────────────────────────────────────────────────────────────────

@router.message(F.document)
async def on_document(message: Message) -> None:
    if not _is_allowed(_uid(message)):
        await _deny(message)
        return
    doc = message.document
    filename = doc.file_name or "statement.pdf"
    ext = Path(filename).suffix.lower()
    if ext not in service.ALLOWED_EXTENSIONS:
        await message.answer(
            f"⚠️ Формат «{ext or '—'}» не поддерживается.\nПоддерживаю: {presenter.SUPPORTED_FORMATS}"
        )
        return
    if doc.file_size and doc.file_size > service.MAX_FILE_BYTES:
        await message.answer("⚠️ Файл слишком большой (максимум 25 МБ).")
        return
    await _process_upload(message, filename, doc)


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    if not _is_allowed(_uid(message)):
        await _deny(message)
        return
    photo: PhotoSize = message.photo[-1]  # largest size
    await _process_upload(message, "photo.jpg", photo)


@router.message()
async def on_other(message: Message) -> None:
    if not _is_allowed(_uid(message)):
        return
    await message.answer(
        "Пришлите файл выписки — PDF, Excel или фото.\n/history — последние выписки, /help — помощь."
    )


async def _process_upload(message: Message, filename: str, file_obj) -> None:
    status = await message.answer("⏳ Разбираю выписку…")
    try:
        buffer = await message.bot.download(file_obj)
        content = buffer.read() if buffer else b""
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram: download failed: %s", exc)
        await status.edit_text("⚠️ Не удалось скачать файл. Попробуйте ещё раз.")
        return

    if not content:
        await status.edit_text("⚠️ Файл пустой.")
        return

    try:
        result = await asyncio.to_thread(service.analyze_document, filename, content)
    except Exception as exc:  # noqa: BLE001
        log.exception("telegram: analyze failed")
        await status.edit_text(f"⚠️ Ошибка обработки: {exc}")
        return

    if result.needs_manual_review or not result.statement or not result.session_id:
        await status.edit_text(presenter.manual_review_text(result.error))
        return

    data = await asyncio.to_thread(service.prepare_summary, result.statement)
    await status.edit_text(
        presenter.summary_text(data.statement, data.quality),
        reply_markup=presenter.summary_keyboard(result.session_id, data.default_index),
    )


# ── Callbacks ────────────────────────────────────────────────────────────────────

async def _load(callback: CallbackQuery, session_id: str) -> service.SummaryData | None:
    data = await asyncio.to_thread(service.summarize_session, session_id)
    if data is None:
        await callback.answer("Сессия не найдена. Пришлите файл заново.", show_alert=True)
    return data


@router.callback_query(F.data.startswith("x:"))
async def cb_export(callback: CallbackQuery) -> None:
    try:
        _, fmt, session_id, index = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    await callback.answer("Готовлю файл…")
    try:
        result = await asyncio.to_thread(
            service.export_variant, session_id, int(index), csv=(fmt == "c")
        )
    except FileNotFoundError:
        await callback.message.answer("Сессия не найдена. Пришлите файл заново.")
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram: export failed: %s", exc)
        await callback.message.answer(f"⚠️ Ошибка экспорта: {exc}")
        return
    await callback.message.answer_document(
        BufferedInputFile(result.data, filename=result.filename)
    )


@router.callback_query(F.data.startswith("v:"))
async def cb_choose_variant(callback: CallbackQuery) -> None:
    session_id = callback.data.split(":", 1)[1]
    data = await _load(callback, session_id)
    if data is None:
        return
    await callback.message.edit_reply_markup(
        reply_markup=presenter.variant_choice_keyboard(session_id, data.variants)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vp:"))
async def cb_pick_variant(callback: CallbackQuery) -> None:
    try:
        _, session_id, index = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    data = await _load(callback, session_id)
    if data is None:
        return
    idx = int(index)
    name = data.variants[idx].name if 0 <= idx < len(data.variants) else "?"
    await callback.message.edit_reply_markup(
        reply_markup=presenter.variant_export_keyboard(session_id, idx)
    )
    await callback.answer(f"Вариант: {name}")


@router.callback_query(F.data.startswith("q:"))
async def cb_quality(callback: CallbackQuery) -> None:
    session_id = callback.data.split(":", 1)[1]
    data = await _load(callback, session_id)
    if data is None:
        return
    await callback.message.edit_text(
        presenter.quality_text(data.quality, data.diagnostics),
        reply_markup=presenter.back_keyboard(session_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("b:"))
async def cb_back(callback: CallbackQuery) -> None:
    session_id = callback.data.split(":", 1)[1]
    data = await _load(callback, session_id)
    if data is None:
        return
    await callback.message.edit_text(
        presenter.summary_text(data.statement, data.quality),
        reply_markup=presenter.summary_keyboard(session_id, data.default_index),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_open_session(callback: CallbackQuery) -> None:
    session_id = callback.data.split(":", 1)[1]
    data = await _load(callback, session_id)
    if data is None:
        return
    await callback.message.answer(
        presenter.summary_text(data.statement, data.quality),
        reply_markup=presenter.summary_keyboard(session_id, data.default_index),
    )
    await callback.answer()


@router.callback_query(F.data == "hist")
async def cb_history(callback: CallbackQuery) -> None:
    sessions = await asyncio.to_thread(service.recent_sessions, 12)
    await callback.message.answer(
        presenter.history_text(sessions),
        reply_markup=presenter.history_keyboard(sessions) if sessions else None,
    )
    await callback.answer()


@router.callback_query(F.data == "fmt")
async def cb_formats(callback: CallbackQuery) -> None:
    await callback.message.answer(presenter.formats_text())
    await callback.answer()
