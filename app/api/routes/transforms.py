from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.schemas.statement import (
    AddOnboardingSampleRequest,
    AdvisorColumnRequest,
    AdvisorColumnResponse,
    AnalyzeDiffRequest,
    AnalyzeDiffResponse,
    AppliedRuleInfo,
    ConsistencyReport,
    CorrectionMemoryEntry,
    CreateOnboardingProjectRequest,
    ComputeRequest,
    ComputeResponse,
    CreateTemplateRequest,
    ExportCsvRequest,
    ExportRequest,
    MaterializeOCRReviewRequest,
    OCRMappingTemplate,
    OCRRuleManagerSnapshot,
    OCRRuleVersionDiff,
    OnboardingProject,
    ParserMatch,
    PreferenceRecord,
    PreviewResponse,
    ClarifyRequest,
    ReAnalyzeRequest,
    SavePreferenceRequest,
    SmartRefineRequest,
    SmartRefineResponse,
    TransformationTemplate,
    UpdateOCRRuleRequest,
    UpdateRowRequest,
    UpdateTemplateRequest,
    VisionStatus,
    WebExcelSaveRequest,
)
from app.services.document_service import (
    DocumentParseError,
    list_supported_parsers,
    parse_statement_with_diagnostics,
)
from app.services.export_service import compute_variant, export_statement, export_statement_csv
from app.services.ocr_mapping_template_service import (
    compare_ocr_mapping_template_versions,
    find_best_ocr_mapping_match,
    get_ocr_mapping_template,
    get_ocr_rule_manager_snapshot,
    list_ocr_mapping_templates,
    rollback_ocr_mapping_template,
    set_ocr_mapping_template_status,
)
from app.services.ocr_review_service import (
    create_ocr_review_session,
    materialize_ocr_review,
    save_mapping_template_from_review,
)
from app.services.onboarding_service import (
    add_onboarding_sample,
    create_onboarding_project,
    get_onboarding_project,
    list_onboarding_projects,
)
from app.services.quality_service import analyze_statement_quality
from app.services.session_service import (
    get_preference,
    list_correction_memory,
    list_preferences,
    list_recent_sessions,
    load_session,
    save_preference,
    save_session,
    update_session_transaction,
)
from app.services.template_service import create_template, list_templates, update_template
from app.services.variant_service import apply_template_to_variant, build_template_seed, build_variants
from app.services.vision_service import get_vision_status

router = APIRouter(prefix="/transforms")


@router.post("/preview", response_model=PreviewResponse)
def preview_transform(file: UploadFile = File(...)) -> PreviewResponse:
    # Sync `def` endpoint: Starlette runs it in a threadpool, so the heavy
    # (and blocking) PDF parsing / Azure OCR below never stalls the event loop
    # — health checks and the parser/history polls stay responsive.
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Файл пустой.")

    try:
        statement, parser_matches = parse_statement_with_diagnostics(
            file.filename or "uploaded-document",
            content,
        )
    except DocumentParseError as exc:
        review_response = _build_ocr_review_preview(file.filename or "uploaded-document", content)
        if review_response is not None:
            return review_response
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session_id = save_session(statement)
    preference = get_preference(statement.metadata.parser_key)
    return _build_preview_response(session_id, statement, preference, parser_matches)


@router.get("/sessions/{session_id}", response_model=PreviewResponse)
def get_session_preview(session_id: str) -> PreviewResponse:
    try:
        statement = load_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    preference = get_preference(statement.metadata.parser_key)
    parser_label = next(
        (
            parser.label
            for parser in list_supported_parsers()
            if parser.key == statement.metadata.parser_key
        ),
        statement.metadata.parser_key,
    )
    return _build_preview_response(
        session_id,
        statement,
        preference,
        [ParserMatch(key=statement.metadata.parser_key, label=parser_label, score=1.0, matched=True)],
    )


@router.post("/export")
def export_transform(request: ExportRequest) -> StreamingResponse:
    try:
        statement = load_session(request.session_id)
        workbook_bytes = export_statement(
            statement,
            request.variant_key,
            excluded_rows=request.excluded_rows or [],
            custom_columns=request.custom_columns,
            custom_rows=request.custom_rows,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"{request.variant_key}-{request.session_id}.xlsx"
    return StreamingResponse(
        BytesIO(workbook_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/compute", response_model=ComputeResponse)
def compute_preview(request: ComputeRequest) -> ComputeResponse:
    """Live recompute of formula columns for the in-browser editor (Web-Excel & demo)."""
    try:
        statement = load_session(request.session_id)
        variant = compute_variant(
            statement,
            request.variant_key,
            excluded_rows=request.excluded_rows or [],
            custom_columns=request.custom_columns,
            custom_rows=request.custom_rows,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ComputeResponse(columns=list(variant.columns), rows=list(variant.rows))


@router.post("/export/csv")
def export_transform_csv(request: ExportCsvRequest) -> StreamingResponse:
    try:
        statement = load_session(request.session_id)
        csv_bytes = export_statement_csv(statement, request.variant_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"{request.variant_key}-{request.session_id}.csv"
    return StreamingResponse(
        BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/ocr-reviews/{review_id}/materialize", response_model=PreviewResponse)
def materialize_review_to_preview(
    review_id: str,
    request: MaterializeOCRReviewRequest,
) -> PreviewResponse:
    try:
        statement = materialize_ocr_review(review_id, request)
        if request.save_mapping_template:
            save_mapping_template_from_review(review_id, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session_id = save_session(statement)
    preference = get_preference(statement.metadata.parser_key)
    parser_label = next(
        (
            parser.label
            for parser in list_supported_parsers()
            if parser.key == statement.metadata.parser_key
        ),
        statement.metadata.parser_key,
    )
    return _build_preview_response(
        session_id,
        statement,
        preference,
        [ParserMatch(key=statement.metadata.parser_key, label=parser_label, score=1.0, matched=True)],
    )


@router.patch("/sessions/{session_id}/rows/{row_number}", response_model=PreviewResponse)
def patch_session_row(
    session_id: str,
    row_number: int,
    request: UpdateRowRequest,
) -> PreviewResponse:
    try:
        statement = update_session_transaction(
            session_id,
            row_number,
            date=request.date,
            amount=request.amount,
            operation=request.operation,
            detail=request.detail,
            direction=request.direction,
            note=request.note,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    preference = get_preference(statement.metadata.parser_key)
    parser_label = next(
        (
            parser.label
            for parser in list_supported_parsers()
            if parser.key == statement.metadata.parser_key
        ),
        statement.metadata.parser_key,
    )
    return _build_preview_response(
        session_id,
        statement,
        preference,
        [ParserMatch(key=statement.metadata.parser_key, label=parser_label, score=1.0, matched=True)],
    )


@router.post("/preferences")
async def upsert_preference(request: SavePreferenceRequest) -> PreferenceRecord:
    preference = PreferenceRecord(
        parser_key=request.parser_key,
        preferred_variant_key=request.preferred_variant_key,
        always_show_alternatives=request.always_show_alternatives,
        updated_at=datetime.now(UTC),
    )
    return save_preference(preference)


@router.get("/preferences")
async def get_preferences() -> list[PreferenceRecord]:
    return list_preferences()


@router.get("/correction-memory", response_model=list[CorrectionMemoryEntry])
async def get_correction_memory(parser_key: str | None = None) -> list[CorrectionMemoryEntry]:
    return [CorrectionMemoryEntry.model_validate(item) for item in list_correction_memory(parser_key)]


@router.get("/history")
def get_history():
    return list_recent_sessions()


@router.get("/parsers")
def get_parsers():
    return list_supported_parsers()


@router.get("/vision-status", response_model=VisionStatus)
async def get_vision_runtime_status() -> VisionStatus:
    return get_vision_status()


@router.get("/fx-rates")
def get_fx_rates() -> dict:
    """Live currency rates from NB RK (cached daily), with source + date."""
    from app.services.fx_rates_service import get_all_rates
    return get_all_rates()


@router.get("/templates")
async def get_templates(parser_key: str | None = None) -> list[TransformationTemplate]:
    return list_templates(parser_key)


@router.post("/templates")
async def post_template(request: CreateTemplateRequest) -> TransformationTemplate:
    return create_template(request)


@router.post("/web-excel/save", response_model=TransformationTemplate)
def save_web_excel_image(request: WebExcelSaveRequest) -> TransformationTemplate:
    """Infer reusable {field} rules from a Web-Excel grid and save them as a
    template (with presentation layout) so the same statement is reproduced next time."""
    from app.services.web_excel_formula_translator import infer_column_rules
    from app.schemas.statement import TemplateColumnConfig

    try:
        statement = load_session(request.session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    base = next(
        (v for v in build_variants(statement) if v.key == request.base_variant_key),
        None,
    )
    if base is None:
        raise HTTPException(status_code=404, detail="Базовый вариант не найден.")

    base_keys = {c.key for c in base.columns}
    # The grid rows already carry the base fields (detail/operation/amount/…), so we
    # infer from them directly — order-independent (robust to sorting in the grid).
    source_rows = [dict(r) for r in (request.rows or base.rows)]
    rules = infer_column_rules(request.columns, request.rows, source_rows, request.a1_formulas)

    columns = [
        TemplateColumnConfig(
            key=col["key"],
            label=col.get("label", col["key"]),
            kind=col.get("kind", "text"),
            enabled=True,
            # Base columns map straight through by key; only user-added columns get
            # an inferred reusable formula.
            formula=None if col["key"] in base_keys else rules.get(col["key"]),
        )
        for col in request.columns
    ]

    return create_template(CreateTemplateRequest(
        parser_key=statement.metadata.parser_key,
        name=request.name,
        description="Web-Excel",
        base_variant_key=request.base_variant_key,
        columns=columns,
        is_default=request.is_default,
        session_id=request.session_id,
        layout=request.layout,
    ))


@router.patch("/templates/{template_id}")
async def patch_template(template_id: str, request: UpdateTemplateRequest) -> TransformationTemplate:
    try:
        return update_template(template_id, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/ocr-mapping-templates", response_model=list[OCRMappingTemplate])
async def get_ocr_mapping_templates() -> list[OCRMappingTemplate]:
    return list_ocr_mapping_templates()


@router.get("/ocr-rule-manager", response_model=OCRRuleManagerSnapshot)
async def get_ocr_rule_manager() -> OCRRuleManagerSnapshot:
    return get_ocr_rule_manager_snapshot()


@router.patch("/ocr-mapping-templates/{template_id}/status", response_model=OCRMappingTemplate)
async def patch_ocr_mapping_template_status(
    template_id: str,
    request: UpdateOCRRuleRequest,
) -> OCRMappingTemplate:
    try:
        return set_ocr_mapping_template_status(template_id, is_active=request.is_active)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ocr-mapping-templates/{template_id}/rollback", response_model=OCRMappingTemplate)
async def post_ocr_mapping_template_rollback(template_id: str) -> OCRMappingTemplate:
    try:
        return rollback_ocr_mapping_template(template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ocr-mapping-templates/{template_id}/compare", response_model=OCRRuleVersionDiff)
async def get_ocr_mapping_template_compare(template_id: str) -> OCRRuleVersionDiff:
    try:
        return compare_ocr_mapping_template_versions(template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/template-seed/{session_id}/{variant_key}")
async def get_template_seed(session_id: str, variant_key: str):
    try:
        statement = load_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    base_variant = next((variant for variant in build_variants(statement) if variant.key == variant_key), None)
    if base_variant is None:
        raise HTTPException(status_code=404, detail="Базовый вариант не найден.")

    return {
        "session_id": session_id,
        "variant_key": variant_key,
        "columns": build_template_seed(base_variant),
    }


@router.get("/onboarding/projects", response_model=list[OnboardingProject])
async def get_onboarding_projects() -> list[OnboardingProject]:
    return list_onboarding_projects()


@router.post("/onboarding/projects", response_model=OnboardingProject)
async def post_onboarding_project(request: CreateOnboardingProjectRequest) -> OnboardingProject:
    return create_onboarding_project(request)


@router.get("/onboarding/projects/{project_id}", response_model=OnboardingProject)
async def get_onboarding_project_details(project_id: str) -> OnboardingProject:
    try:
        return get_onboarding_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/onboarding/projects/{project_id}/samples", response_model=OnboardingProject)
async def post_onboarding_sample(
    project_id: str,
    request: AddOnboardingSampleRequest,
) -> OnboardingProject:
    try:
        add_onboarding_sample(project_id, request)
        return get_onboarding_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _build_preview_response(
    session_id: str,
    statement,
    preference: PreferenceRecord | None,
    parser_matches: list[ParserMatch],
    applied_rule: AppliedRuleInfo | None = None,
) -> PreviewResponse:
    from app.services.signature_util import jaccard, signature_from_columns, MATCH_THRESHOLD

    quality_summary, row_diagnostics = analyze_statement_quality(statement)
    base_variants = build_variants(statement)
    templates = list_templates(statement.metadata.parser_key)
    base_lookup = {variant.key: variant for variant in base_variants}
    saved_variants = [
        apply_template_to_variant(base_lookup[template.base_variant_key], template)
        for template in templates
        if template.base_variant_key in base_lookup
    ]

    # Default selection: an explicitly-default template always wins (manual override);
    # otherwise auto-match by column-structure signature (Jaccard) above threshold.
    default_template = next((template for template in templates if template.is_default), None)
    if default_template is None:
        incoming_sig = {key: signature_from_columns(variant.columns) for key, variant in base_lookup.items()}
        best_score = MATCH_THRESHOLD
        for template in templates:
            if template.base_variant_key not in base_lookup or not template.source_signature:
                continue
            score = jaccard(template.source_signature, incoming_sig[template.base_variant_key])
            if score >= best_score:
                best_score = score
                default_template = template
    default_variant_key = f"template::{default_template.template_id}" if default_template else None

    return PreviewResponse(
        session_id=session_id,
        document=statement.metadata,
        parser_matches=parser_matches,
        applied_rule=applied_rule,
        quality_summary=quality_summary,
        row_diagnostics=row_diagnostics,
        ocr_review=None,
        variants=base_variants,
        saved_variants=saved_variants,
        templates=templates,
        preference=preference,
        default_variant_key=default_variant_key,
        ai_insights=statement.ai_insights,
    )


def _build_ocr_review_preview(filename: str, content: bytes) -> PreviewResponse | None:
    try:
        review = create_ocr_review_session(filename, content)
    except Exception:
        return None

    best_match = find_best_ocr_mapping_match(review)
    if best_match is not None:
        template = get_ocr_mapping_template(best_match.template_id)
        if template is not None:
            try:
                statement = materialize_ocr_review(
                    review.review_id,
                    MaterializeOCRReviewRequest(
                        table_index=best_match.table_index,
                        header_row_index=best_match.header_row_index,
                        title=review.source_filename,
                        column_mapping=template.column_mapping,
                    ),
                )
                session_id = save_session(statement)
                preference = get_preference(statement.metadata.parser_key)
                return _build_preview_response(
                    session_id,
                    statement,
                    preference,
                    [
                        ParserMatch(
                            key=statement.metadata.parser_key,
                            label=f"OCR Auto Mapping В· {template.name}",
                            score=best_match.score,
                            matched=True,
                        )
                    ],
                    AppliedRuleInfo(
                        rule_type="ocr_mapping_template",
                        template_id=template.template_id,
                        name=template.name,
                        version=template.version,
                        score=best_match.score,
                        reason=_build_rule_reason(best_match),
                        header_score=best_match.header_score,
                        title_score=best_match.title_score,
                        matched_header_signature=best_match.matched_header_signature,
                        matched_title_keywords=best_match.matched_title_keywords,
                    ),
                )
            except Exception:
                pass

    return PreviewResponse(
        session_id="",
        document={
            "source_filename": filename,
            "title": filename,
            "parser_key": "ocr_raw_review",
            "account_holder": None,
            "card_number": None,
            "account_number": None,
            "currency": None,
            "period_start": None,
            "period_end": None,
            "opening_balance": None,
            "closing_balance": None,
            "transaction_count": 0,
            "totals": {
                "income_total": 0.0,
                "expense_total": 0.0,
                "purchase_total": 0.0,
                "transfer_total": 0.0,
                "topup_total": 0.0,
                "cash_withdrawal_total": 0.0,
            },
        },
        parser_matches=[],
        applied_rule=None,
        quality_summary={"overall_confidence": 0.0},
        row_diagnostics=[],
        ocr_review=review,
        variants=[],
        saved_variants=[],
        templates=[],
        preference=None,
        default_variant_key=None,
    )


# ── Smart Brain endpoints ──────────────────────────────────────────────────────

@router.post("/advisor/column", response_model=AdvisorColumnResponse)
def advisor_column(request: AdvisorColumnRequest) -> AdvisorColumnResponse:
    """
    Recommend formulas for a column based on its name and optional sample values.
    Combines lexical analysis + statistical pattern detection.
    """
    from app.services.column_advisor import advise
    return advise(request)


@router.post("/analyze-diff", response_model=AnalyzeDiffResponse)
def analyze_diff(request: AnalyzeDiffRequest) -> AnalyzeDiffResponse:
    """
    Reverse-engineer what changes a user made to a variant.
    Detects formulas, filters, renames from the diff between original and edited rows.
    """
    from app.services.session_service import load_session
    from app.services.variant_service import build_variants
    from app.services.diff_analyzer import analyze_diff as _analyze

    statement = load_session(request.session_id)
    variants = {v.key: v for v in build_variants(statement)}

    orig_variant = variants.get(request.original_variant_key)
    if orig_variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")

    return _analyze(
        orig_columns=[c.model_dump() for c in orig_variant.columns],
        orig_rows=list(orig_variant.rows),
        edit_columns=request.edited_columns,
        edit_rows=request.edited_rows,
        user_hint=None,
    )


@router.post("/re-analyze", response_model=AnalyzeDiffResponse)
def re_analyze(request: ReAnalyzeRequest) -> AnalyzeDiffResponse:
    """
    Re-analyze diff with a user-provided Russian hint.
    Routes through Smart NLP engine first; falls back to keyword matching if confidence is low.
    """
    from app.services.session_service import load_session
    from app.services.variant_service import build_variants
    from app.services.diff_analyzer import analyze_diff as _analyze
    from app.core.config import settings

    statement = load_session(request.session_id)
    variants = {v.key: v for v in build_variants(statement)}

    orig_variant = variants.get(request.original_variant_key)
    if orig_variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")

    base_result = _analyze(
        orig_columns=[c.model_dump() for c in orig_variant.columns],
        orig_rows=list(orig_variant.rows),
        edit_columns=request.edited_columns,
        edit_rows=request.edited_rows,
        user_hint=None,
    )

    if not settings.smart_nlp_enabled:
        # Use legacy keyword hint path
        from app.services.diff_analyzer import apply_hint
        return base_result.model_copy(
            update={"findings": apply_hint(request.user_hint, base_result.findings)}
        )

    try:
        from app.services import smart_correction_service
        from app.services.nlp.types import SmartContext

        context = SmartContext(
            columns=request.edited_columns,
            recent_rows=request.edited_rows[:20],
            parser_key=statement.metadata.parser_key,
            target_column_key=request.target_column_key,
        )
        refine_result = smart_correction_service.refine(
            findings=base_result.findings,
            user_hint=request.user_hint,
            context=context,
        )
        return AnalyzeDiffResponse(
            findings=refine_result.findings,
            summary_ru=refine_result.narrative_ru,
        )
    except Exception:
        from app.services.diff_analyzer import apply_hint
        return base_result.model_copy(
            update={"findings": apply_hint(request.user_hint, base_result.findings)}
        )


@router.post("/smart-refine", response_model=SmartRefineResponse)
def smart_refine(request: SmartRefineRequest) -> SmartRefineResponse:
    """
    Full smart NLP refinement — returns narrative, clarification chips, and confidence.
    """
    from app.services.session_service import load_session
    from app.services.variant_service import build_variants
    from app.services.diff_analyzer import analyze_diff as _analyze
    from app.services import smart_correction_service
    from app.services.nlp.types import SmartContext

    statement = load_session(request.session_id)
    variants = {v.key: v for v in build_variants(statement)}

    orig_variant = variants.get(request.original_variant_key)
    if orig_variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")

    base_findings = request.existing_findings
    if base_findings is None:
        base_result = _analyze(
            orig_columns=[c.model_dump() for c in orig_variant.columns],
            orig_rows=list(orig_variant.rows),
            edit_columns=request.edited_columns,
            edit_rows=request.edited_rows,
            user_hint=None,
        )
        base_findings = base_result.findings

    context = SmartContext(
        columns=request.edited_columns,
        recent_rows=request.edited_rows[:20],
        parser_key=statement.metadata.parser_key,
        target_column_key=request.target_column_key,
    )
    result = smart_correction_service.refine(
        findings=base_findings,
        user_hint=request.user_hint,
        context=context,
    )
    return SmartRefineResponse(
        findings=result.findings,
        narrative_ru=result.narrative_ru,
        clarifications=result.clarifications,
        confidence=result.confidence,
        summary_ru=result.narrative_ru,
    )


@router.post("/clarify", response_model=SmartRefineResponse)
def clarify_intent(request: ClarifyRequest) -> SmartRefineResponse:
    """
    User picks one of the clarification choices — re-run pipeline with explicit choice.
    """
    from app.services.session_service import load_session
    from app.services.variant_service import build_variants
    from app.services.diff_analyzer import analyze_diff as _analyze
    from app.services import smart_correction_service
    from app.services.nlp.types import SmartContext
    from app.schemas.statement import DiffFinding

    statement = load_session(request.session_id)
    variants = {v.key: v for v in build_variants(statement)}

    orig_variant = variants.get(request.original_variant_key)
    if orig_variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")

    base_findings = request.existing_findings or []
    if not base_findings:
        base_result = _analyze(
            orig_columns=[c.model_dump() for c in orig_variant.columns],
            orig_rows=list(orig_variant.rows),
            edit_columns=request.edited_columns,
            edit_rows=request.edited_rows,
            user_hint=None,
        )
        base_findings = base_result.findings

    # Apply chosen formula directly (user resolved ambiguity)
    chosen_hint = f"{request.question_ru} выбор {request.choice_index}"
    context = SmartContext(
        columns=request.edited_columns,
        recent_rows=request.edited_rows[:20],
        parser_key=statement.metadata.parser_key,
        target_column_key=request.target_column_key,
    )
    result = smart_correction_service.refine(
        findings=base_findings,
        user_hint=chosen_hint,
        context=context,
    )
    return SmartRefineResponse(
        findings=result.findings,
        narrative_ru=result.narrative_ru,
        clarifications=[],
        confidence=result.confidence,
        summary_ru=result.narrative_ru,
    )


@router.get("/consistency/{session_id}/{variant_key:path}", response_model=ConsistencyReport)
def check_consistency(session_id: str, variant_key: str) -> ConsistencyReport:
    """
    Run consistency checks on a variant's rows.
    Detects balance mismatches, duplicates, date gaps, anomalies.
    """
    from app.services.session_service import load_session
    from app.services.variant_service import build_variants
    from app.services.consistency_checker import check_rows

    statement = load_session(session_id)
    variants = {v.key: v for v in build_variants(statement)}
    variant = variants.get(variant_key)
    if variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")

    meta = statement.metadata
    return check_rows(
        rows=list(variant.rows),
        reported_income=getattr(meta, "total_topup", None),
        reported_expense=getattr(meta, "total_purchase", None),
    )


@router.post("/validate-formula")
def validate_formula(body: dict) -> dict:
    """Quick formula syntax check. Returns {valid: bool, error: str|null}."""
    from app.services.formula_engine import validate_formula as _validate
    formula = body.get("formula", "")
    ok, err = _validate(formula)
    return {"valid": ok, "error": err}


def _build_rule_reason(best_match: Any) -> str:
    reasons: list[str] = []
    if getattr(best_match, "matched_header_signature", None):
        reasons.append(f"header matched: {', '.join(best_match.matched_header_signature[:4])}")
    if getattr(best_match, "matched_title_keywords", None):
        reasons.append(f"title matched: {', '.join(best_match.matched_title_keywords[:4])}")
    if not reasons:
        reasons.append("best available OCR template match")
    return " | ".join(reasons)
