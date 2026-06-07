from __future__ import annotations

import uuid

from app.core.database import db_session
from app.models.persistence import OCRReviewRecord
from app.schemas.statement import MaterializeOCRReviewRequest, OCRReviewPayload, ParsedStatement
from app.services.ocr_mapping_template_service import create_ocr_mapping_template
from app.services.ocr_service import OCRProcessingError, build_statement_from_review_mapping, extract_ocr_review


def create_ocr_review_session(filename: str, content: bytes) -> OCRReviewPayload:
    payload = extract_ocr_review(filename, content)
    return save_ocr_review_payload(payload)


def save_ocr_review_payload(payload: OCRReviewPayload | dict) -> OCRReviewPayload:
    """Persist a pre-built OCR review payload (e.g. from the scanned pipeline) and
    return it with a freshly assigned review_id. Avoids re-running OCR."""
    review = payload if isinstance(payload, OCRReviewPayload) else OCRReviewPayload.model_validate(payload)
    review_id = uuid.uuid4().hex
    review = review.model_copy(update={"review_id": review_id})
    with db_session() as session:
        session.add(
            OCRReviewRecord(
                review_id=review_id,
                source_filename=review.source_filename,
                payload=review.model_dump(mode="json"),
            )
        )
    return review


def load_ocr_review(review_id: str) -> OCRReviewPayload:
    with db_session() as session:
        record = session.get(OCRReviewRecord, review_id)
        if record is None:
            raise FileNotFoundError("OCR review session was not found.")
        return OCRReviewPayload.model_validate(record.payload)


def materialize_ocr_review(
    review_id: str,
    request: MaterializeOCRReviewRequest,
) -> ParsedStatement:
    review = load_ocr_review(review_id)
    try:
        return build_statement_from_review_mapping(
            review.source_filename,
            [table.rows for table in review.tables],
            table_index=request.table_index,
            header_row_index=request.header_row_index,
            column_mapping=request.column_mapping,
            title=request.title,
        )
    except OCRProcessingError as exc:
        raise ValueError(str(exc)) from exc


def save_mapping_template_from_review(
    review_id: str,
    request: MaterializeOCRReviewRequest,
) -> None:
    review = load_ocr_review(review_id)
    if request.table_index < 0 or request.table_index >= len(review.tables):
        raise ValueError("Selected OCR table was not found.")
    table = review.tables[request.table_index]
    if request.header_row_index < 0 or request.header_row_index >= len(table.rows):
        raise ValueError("Selected OCR header row was not found.")

    create_ocr_mapping_template(
        name=request.mapping_template_name or request.title or "OCR Mapping Template",
        source_filename=review.source_filename,
        header_row=table.rows[request.header_row_index],
        column_mapping=request.column_mapping,
    )
