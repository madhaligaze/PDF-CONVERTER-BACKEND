"""
Scanned document OCR routes.

POST /transforms/scan                  → upload → ScanResponse
GET  /transforms/scan/{scan_id}/docx   → download .docx
POST /transforms/scan/{scan_id}/to-review → push into OCR review flow
"""
from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from io import BytesIO

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.schemas.scanned import PreviewTableRow, ScanResponse, ScanResultMeta

log = logging.getLogger(__name__)

router = APIRouter(tags=["scanned"])

# Bounded in-memory LRU scan store. Each entry holds a full scanned document
# (tables + cell data), so an unbounded dict would leak RAM and eventually OOM
# the container. Keep only the most recent scans; older ones are evicted.
_SCAN_STORE_MAX = 32
_scan_store: "OrderedDict[str, dict]" = OrderedDict()


def _store_scan(scan_id: str, entry: dict) -> None:
    _scan_store[scan_id] = entry
    _scan_store.move_to_end(scan_id)
    while len(_scan_store) > _SCAN_STORE_MAX:
        _scan_store.popitem(last=False)


def _get_scan(scan_id: str) -> dict | None:
    entry = _scan_store.get(scan_id)
    if entry is not None:
        _scan_store.move_to_end(scan_id)
    return entry


@router.post("/transforms/scan", response_model=ScanResponse)
def scan_document(file: UploadFile = File(...)) -> ScanResponse:
    """
    Upload a scanned PDF or image.
    Returns scan_id + table preview for display in the UI.

    Sync `def`: the heavy OCR pipeline (rapidocr / onnxruntime) runs in a
    threadpool so the single-worker event loop stays responsive.
    """
    try:
        from app.services.scanned.structured_builder import build_scanned_document
    except ImportError:
        raise HTTPException(status_code=503, detail="Scanned OCR pipeline not available")

    content = file.file.read()
    filename = file.filename or "scan.pdf"

    document = build_scanned_document(filename, content)
    scan_id = str(uuid.uuid4()).replace("-", "")

    # Build preview tables
    preview: list[PreviewTableRow] = []
    for page in document.pages:
        for table in page.tables:
            cells = table.cells
            if not cells:
                continue
            rows_count = max(c.row for c in cells) + 1
            cols_count = max(c.col for c in cells) + 1
            grid: list[list[str]] = [[""] * cols_count for _ in range(rows_count)]
            for c in cells:
                grid[c.row][c.col] = c.text

            headers = grid[table.header_row_index] if rows_count > 0 else []
            data_rows = [r for i, r in enumerate(grid) if i != table.header_row_index]
            avg_conf = sum(c.confidence for c in cells) / max(len(cells), 1)

            preview.append(PreviewTableRow(
                page=page.page_index + 1,
                headers=headers,
                rows=data_rows[:10],  # preview: first 10 data rows
                confidence=round(avg_conf, 3),
            ))

    # Persist for downstream endpoints (bounded LRU — see _store_scan)
    _store_scan(scan_id, {"document": document, "filename": filename})

    rotation_angles = [p.rotation_angle for p in document.pages]
    warnings = list(document.warnings)
    for page in document.pages:
        warnings.extend(page.warnings)

    meta = ScanResultMeta(
        scan_id=scan_id,
        source_filename=filename,
        page_count=len(document.pages),
        avg_confidence=round(document.avg_confidence, 3),
        rotation_angles=rotation_angles,
        warnings=list(dict.fromkeys(warnings)),  # deduplicate
        tables_found=sum(len(p.tables) for p in document.pages),
    )

    return ScanResponse(scan_id=scan_id, meta=meta, preview_tables=preview)


@router.get("/transforms/scan/{scan_id}/docx")
def download_scan_docx(scan_id: str) -> StreamingResponse:
    """Download the structured .docx built from a previously uploaded scan."""
    entry = _get_scan(scan_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Скан не найден")

    try:
        from app.services.scanned.docx_writer import write_docx
    except ImportError:
        raise HTTPException(status_code=503, detail="python-docx not available")

    docx_bytes = write_docx(entry["document"])
    if not docx_bytes:
        raise HTTPException(status_code=503, detail="Не удалось создать Word-документ")

    filename = entry["filename"].rsplit(".", 1)[0] + "_ocr.docx"
    return StreamingResponse(
        BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/transforms/scan/{scan_id}/to-review")
def scan_to_review(scan_id: str):
    """
    Push a completed scan into the existing OCR review flow.
    Returns the new review_id so the UI can open the OCR review.
    """
    entry = _get_scan(scan_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Скан не найден")

    try:
        from app.services.scanned.review_adapter import to_ocr_review_payload
        from app.services.ocr_review_service import save_ocr_review_payload
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"OCR review pipeline not available: {exc}")

    document = entry["document"]
    payload = to_ocr_review_payload(document)

    # Persist the already-OCR'd payload directly (no second OCR pass).
    review = save_ocr_review_payload(payload)

    return {
        "review_id": review.review_id,
        "message": "Скан передан в OCR-проверку",
        "source_filename": document.source_filename,
        "tables_found": sum(len(p.tables) for p in document.pages),
    }
