"""Russian-language message builders, inline keyboards and callback_data codec.

callback_data layout (kept well under Telegram's 64-byte limit; session_id is
32 hex chars):
    x:<e|c>:<session_id>:<idx>   export excel/csv of variant #idx
    v:<session_id>               open the variant chooser
    vp:<session_id>:<idx>        pick variant #idx → show its export buttons
    q:<session_id>               show quality report
    s:<session_id>               open a session from history
    b:<session_id>               back to the summary
    hist                         open recent history
"""
from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.schemas.statement import (
    ParsedStatement,
    PreviewVariant,
    QualitySummary,
    RowDiagnostic,
    SessionSummary,
)

SUPPORTED_FORMATS = "PDF, Excel (.xlsx/.xlsm), фото/скан (.png/.jpg/.jpeg)"


# ── Text helpers ────────────────────────────────────────────────────────────────

def _esc(value: str | None) -> str:
    return html.escape(value) if value else ""


def _money(value: float | None) -> str:
    if not value:
        return "0"
    return f"{value:,.2f}".replace(",", " ")


def welcome_text() -> str:
    return (
        "👋 <b>Анализатор банковских выписок</b>\n\n"
        "Пришлите файл выписки — я разберу его и верну таблицу в Excel или CSV.\n\n"
        f"Поддерживаю: {SUPPORTED_FORMATS}\n\n"
        "Команды: /history — последние выписки, /help — помощь."
    )


def formats_text() -> str:
    return (
        "ℹ️ <b>Поддерживаемые форматы</b>\n\n"
        f"{SUPPORTED_FORMATS}\n\n"
        "Просто отправьте файл или фото выписки в этот чат."
    )


def manual_review_text(error: str | None) -> str:
    base = (
        "🧾 Не удалось распознать это как стандартную выписку.\n"
        "Для нестандартных и отсканированных документов используйте веб-приложение "
        "— там можно вручную разметить колонки."
    )
    if error:
        base += f"\n\n<i>{_esc(error[:300])}</i>"
    return base


def summary_text(statement: ParsedStatement, quality: QualitySummary) -> str:
    meta = statement.metadata
    totals = meta.totals
    currency = _esc(meta.currency) or ""
    cur = f" {currency}" if currency else ""

    lines: list[str] = [f"🏦 <b>{_esc(meta.title or meta.source_filename)}</b>"]

    info: list[str] = []
    if meta.account_holder:
        info.append(f"👤 {_esc(meta.account_holder)}")
    card = meta.card_number or meta.account_number
    if card:
        info.append(f"💳 {_esc(card)}")
    if meta.currency:
        info.append(f"💱 {currency}")
    if meta.period_start or meta.period_end:
        info.append(f"📅 {_esc(meta.period_start or '?')} — {_esc(meta.period_end or '?')}")
    info.append(f"🔢 Операций: {meta.transaction_count}")
    lines.append("\n".join(info))

    lines.append(
        "💰 <b>Итоги</b>\n"
        f"  Пополнения: {_money(totals.topup_total)}{cur}\n"
        f"  Списания: {_money(totals.expense_total)}{cur}\n"
        f"  Покупки: {_money(totals.purchase_total)}{cur}\n"
        f"  Переводы: {_money(totals.transfer_total)}{cur}\n"
        f"  Снятия: {_money(totals.cash_withdrawal_total)}{cur}"
    )

    if quality.high_risk_count or quality.review_required_count:
        lines.append(
            f"⚠️ Качество: высокий риск {quality.high_risk_count}, "
            f"на проверку {quality.review_required_count}"
        )
    if quality.totals_mismatch:
        lines.append("⚠️ Возможно несоответствие итогов — проверьте суммы.")

    insights = statement.ai_insights
    if insights and insights.summary:
        lines.append(f"🧠 {_esc(insights.summary[:600])}")

    lines.append("\nВыберите формат выгрузки ниже 👇")
    return "\n\n".join(lines)


def quality_text(quality: QualitySummary, diagnostics: list[RowDiagnostic]) -> str:
    lines = [
        "⚠️ <b>Качество распознавания</b>",
        (
            f"Уверенность: {round(quality.overall_confidence * 100)}%  ·  "
            f"высокий риск: {quality.high_risk_count}  ·  "
            f"на проверку: {quality.review_required_count}  ·  "
            f"исправлено: {quality.corrected_count}"
        ),
    ]
    risky = [d for d in diagnostics if d.flags][:6]
    if risky:
        lines.append("\n<b>Строки на внимание:</b>")
        for d in risky:
            reason = d.flags[0].message if d.flags else ""
            lines.append(
                f"• №{d.row_number} {_esc(d.date)} — {_esc(d.detail[:40])} "
                f"({_money(d.amount)}): {_esc(reason[:60])}"
            )
    if quality.recommendations:
        lines.append("\n<b>Рекомендации:</b>")
        for rec in quality.recommendations[:4]:
            lines.append(f"• {_esc(rec)}")
    if not risky and not quality.recommendations:
        lines.append("\n✅ Существенных проблем не обнаружено.")
    return "\n".join(lines)


# ── Keyboards ───────────────────────────────────────────────────────────────────

def start_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📜 История", callback_data="hist"),
        InlineKeyboardButton(text="ℹ️ Форматы", callback_data="fmt"),
    )
    return kb.as_markup()


def summary_keyboard(session_id: str, default_index: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Excel", callback_data=f"x:e:{session_id}:{default_index}"),
        InlineKeyboardButton(text="📄 CSV", callback_data=f"x:c:{session_id}:{default_index}"),
    )
    kb.row(InlineKeyboardButton(text="🧩 Выбрать вариант", callback_data=f"v:{session_id}"))
    kb.row(InlineKeyboardButton(text="⚠️ Качество", callback_data=f"q:{session_id}"))
    return kb.as_markup()


def variant_choice_keyboard(session_id: str, variants: list[PreviewVariant]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, variant in enumerate(variants[:20]):
        kb.row(InlineKeyboardButton(text=variant.name[:60], callback_data=f"vp:{session_id}:{index}"))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"b:{session_id}"))
    return kb.as_markup()


def variant_export_keyboard(session_id: str, index: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Excel", callback_data=f"x:e:{session_id}:{index}"),
        InlineKeyboardButton(text="📄 CSV", callback_data=f"x:c:{session_id}:{index}"),
    )
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"b:{session_id}"))
    return kb.as_markup()


def back_keyboard(session_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"b:{session_id}"))
    return kb.as_markup()


def history_text(sessions: list[SessionSummary]) -> str:
    if not sessions:
        return "📜 История пуста. Пришлите выписку, чтобы начать."
    return "📜 <b>Последние выписки</b>\nВыберите, чтобы открыть и выгрузить:"


def history_keyboard(sessions: list[SessionSummary]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in sessions[:12]:
        label = f"{s.title or s.source_filename}"[:48]
        if s.transaction_count:
            label = f"{label} · {s.transaction_count} оп."
        kb.row(InlineKeyboardButton(text=label, callback_data=f"s:{s.session_id}"))
    return kb.as_markup()
