"""Thin adapter between the Telegram bot and the existing analyzer services.

All functions here are synchronous and may block (PDF parsing, xlsx building,
DB access) — the bot handlers call them via ``asyncio.to_thread`` so the event
loop (shared with the web server) stays responsive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.statement import (
    ParsedStatement,
    PreviewVariant,
    QualitySummary,
    RowDiagnostic,
    SessionSummary,
)
from app.services.document_service import (
    DocumentParseError,
    parse_statement_with_diagnostics,
)
from app.services.export_service import export_statement, export_statement_csv
from app.services.quality_service import analyze_statement_quality
from app.services.session_service import (
    get_preference,
    list_recent_sessions,
    load_session,
    save_session,
)
from app.services.template_service import list_templates
from app.services.variant_service import apply_template_to_variant, build_variants

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg"}
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB safety cap


@dataclass
class AnalyzeResult:
    session_id: str | None = None
    statement: ParsedStatement | None = None
    needs_manual_review: bool = False
    error: str | None = None


def analyze_document(filename: str, content: bytes) -> AnalyzeResult:
    """Parse an uploaded statement and persist a session.

    On a non-standard / scanned document (DocumentParseError) we flag it for the
    web UI rather than attempting interactive OCR column mapping in the bot.
    """
    try:
        statement, _matches = parse_statement_with_diagnostics(filename, content)
    except DocumentParseError as exc:
        return AnalyzeResult(needs_manual_review=True, error=str(exc))

    session_id = save_session(statement)
    # Reload so the bot summary reflects correction-memory + AI enrichment applied on save.
    stored = load_session(session_id)
    return AnalyzeResult(session_id=session_id, statement=stored)


def build_all_variants(statement: ParsedStatement) -> list[PreviewVariant]:
    """Base variants + saved templates, in the same stable order as the web preview."""
    base_variants = build_variants(statement)
    base_lookup = {variant.key: variant for variant in base_variants}
    templates = list_templates(statement.metadata.parser_key)
    saved_variants = [
        apply_template_to_variant(base_lookup[template.base_variant_key], template)
        for template in templates
        if template.base_variant_key in base_lookup
    ]
    return base_variants + saved_variants


def default_variant_index(statement: ParsedStatement, variants: list[PreviewVariant]) -> int:
    """Pick the index of the preferred variant: preference → default template → first."""
    if not variants:
        return 0
    preference = get_preference(statement.metadata.parser_key)
    if preference and preference.preferred_variant_key:
        for index, variant in enumerate(variants):
            if variant.key == preference.preferred_variant_key:
                return index
    templates = list_templates(statement.metadata.parser_key)
    default_template = next((t for t in templates if t.is_default), None)
    if default_template is not None:
        default_key = f"template::{default_template.template_id}"
        for index, variant in enumerate(variants):
            if variant.key == default_key:
                return index
    return 0


@dataclass
class ExportResult:
    data: bytes
    filename: str


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return (stem or "statement")[:60]


def export_variant(session_id: str, variant_index: int, *, csv: bool) -> ExportResult:
    """Reload the session, resolve the variant by index and render xlsx/csv bytes."""
    statement = load_session(session_id)
    variants = build_all_variants(statement)
    if not variants:
        raise ValueError("Нет доступных вариантов для экспорта.")
    index = variant_index if 0 <= variant_index < len(variants) else 0
    variant = variants[index]

    stem = _safe_stem(statement.metadata.title or statement.metadata.source_filename)
    variant_stem = _safe_stem(variant.name)
    if csv:
        data = export_statement_csv(statement, variant.key)
        return ExportResult(data=data, filename=f"{stem}-{variant_stem}.csv")
    data = export_statement(statement, variant.key)
    return ExportResult(data=data, filename=f"{stem}-{variant_stem}.xlsx")


@dataclass
class SummaryData:
    statement: ParsedStatement
    quality: QualitySummary
    diagnostics: list[RowDiagnostic] = field(default_factory=list)
    variants: list[PreviewVariant] = field(default_factory=list)
    default_index: int = 0


def prepare_summary(statement: ParsedStatement) -> SummaryData:
    """Compute everything the bot needs to render a statement summary."""
    quality, diagnostics = analyze_statement_quality(statement)
    variants = build_all_variants(statement)
    return SummaryData(
        statement=statement,
        quality=quality,
        diagnostics=diagnostics,
        variants=variants,
        default_index=default_variant_index(statement, variants),
    )


def summarize_session(session_id: str) -> SummaryData | None:
    statement = get_session(session_id)
    if statement is None:
        return None
    return prepare_summary(statement)


def get_quality(statement: ParsedStatement) -> tuple[QualitySummary, list[RowDiagnostic]]:
    return analyze_statement_quality(statement)


def get_session(session_id: str) -> ParsedStatement | None:
    try:
        return load_session(session_id)
    except FileNotFoundError:
        return None


def recent_sessions(limit: int = 12) -> list[SessionSummary]:
    return list_recent_sessions(limit)
