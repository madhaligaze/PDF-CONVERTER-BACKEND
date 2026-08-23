"""Live refresh: the backend polls Google, browsers poll the backend.

Why this shape: polling from the browser would multiply Google API calls by the
number of open tabs. Here one background loop reads Sheets on a fixed interval and
keeps an in-memory snapshot; clients only ask for a `revision` integer, which is
served from memory without touching Google at all.

    Google Sheets ──full read──▶ background loop (15 s)
                                   │ content hash moved? → rebuild, revision++
                                   ▼
                            in-memory snapshot + revision
                                   ▲
         browser ──GET /bbc/revision (5 s, ~100 bytes)──┘

Worst case latency is one backend interval plus one client interval, ≈20 s.

**Why a full read instead of a cheap Drive `modifiedTime` probe:** measured against
the live sheet, `modifiedTime` did not move for over 75 seconds after an edit that
the Sheets API already returned within 3 seconds. Drive updates that field lazily
for Sheets, so gating on it silently stretched the promised 10–20 s into minutes.
The read itself costs one API call and under a second, which at a 15 s interval is
4 calls/min against a 60/min per-user quota — cheap enough that correctness wins.

A snapshot is persisted to `bbc.sheet_snapshots` only when the content hash
actually changes, so the change history costs almost nothing.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.bbc import sheets
from app.bbc.dataset import (
    ContractRow,
    collect_dimensions,
    content_hash,
    coverage,
    parse_dataset,
)
from app.bbc.recognition import annotate_all
from app.bbc.sheets import BbcError
from app.bbc.validators import analyze, summarize

log = logging.getLogger(__name__)


@dataclass
class SourceState:
    """What we know about one spreadsheet source."""

    source: str
    content_hash: str = ""
    spreadsheet_id: str = ""
    fetched_at: str | None = None
    row_count: int = 0
    error: str | None = None


@dataclass
class Snapshot:
    """The dataset every request is served from."""

    revision: int = 0
    changed_at: str | None = None
    rows: list[ContractRow] = field(default_factory=list)
    dimensions: dict[str, list[str]] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    warnings_summary: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, SourceState] = field(default_factory=dict)
    #: Раскладка мастер-листа: какие колонки нашлись и какие уехали. Уходит в
    #: интерфейс, чтобы «книгу поправили» было видно раньше, чем поедут цифры.
    layout: dict[str, Any] = field(default_factory=dict)


_lock = threading.Lock()
_snapshot = Snapshot()

# Held for the duration of a read, so only one goes to Google at a time.
_read_lock = threading.Lock()
# Outcome of the read that is currently finishing — handed to whoever waited.
_last_outcome = False


def get_snapshot() -> Snapshot:
    with _lock:
        return _snapshot


def revision_payload() -> dict[str, Any]:
    """Tiny response the browser polls — served from memory, no Google call."""
    snapshot = get_snapshot()
    return {
        "revision": snapshot.revision,
        "changed_at": snapshot.changed_at,
        "rows": len(snapshot.rows),
        "layout_shifted": bool(snapshot.layout.get("shifted")),
        "sources": {
            name: {
                "fetched_at": state.fetched_at,
                "row_count": state.row_count,
                "error": state.error,
            }
            for name, state in snapshot.sources.items()
        },
    }


def _persist(source: str, digest: str, revision: int, rows: list[ContractRow]) -> None:
    """Store one version of a source. Best-effort: history must never block reads."""
    try:
        from app.bbc.db import bbc_session
        from app.bbc.models import BbcSheetSnapshot

        spreadsheet_id, worksheet = sheets.source_ref(source)
        with bbc_session() as session:
            session.add(
                BbcSheetSnapshot(
                    source=source,
                    spreadsheet_id=spreadsheet_id,
                    worksheet=worksheet or "(первый лист)",
                    content_hash=digest,
                    revision=revision,
                    row_count=len(rows),
                    payload={"rows": [row.to_dict() for row in rows]},
                )
            )
    except Exception as exc:  # noqa: BLE001 — history is a nice-to-have, not a dependency
        log.warning("BBC: snapshot not persisted (%s: %s)", type(exc).__name__, exc)


def _record_run(started: datetime, changed: list[str], error: str | None) -> None:
    try:
        from app.bbc.db import bbc_session
        from app.bbc.models import BbcSyncRun

        finished = datetime.now(UTC)
        with bbc_session() as session:
            session.add(
                BbcSyncRun(
                    started_at=started,
                    finished_at=finished,
                    changed_sources=changed,
                    duration_ms=int((finished - started).total_seconds() * 1000),
                    error=error,
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("BBC: sync run not recorded (%s)", exc)


def refresh(*, force: bool = False) -> bool:
    """Re-read the sources if they changed. Returns True when the snapshot moved.

    `force=True` skips the cheap `modifiedTime` probe and always re-reads — used by
    the manual refresh button and by the very first load.

    One read at a time. The manual «Обновить» button and the background loop can
    fire together, and several browsers can press it at once; without this gate
    each of them would pull the same 524-row sheet separately and eat the 60/min
    quota for nothing. A caller that arrives mid-read waits for the one in flight
    and takes its answer — by the time the lock frees, the read it needed has
    already happened, so repeating it would add a request and no information.
    """
    global _last_outcome

    if not _read_lock.acquire(blocking=False):
        with _read_lock:
            return _last_outcome

    try:
        _last_outcome = _read_sources(force=force)
        return _last_outcome
    finally:
        _read_lock.release()


def _read_sources(*, force: bool) -> bool:
    """The read itself. Never call directly — `refresh()` owns the lock."""
    started = datetime.now(UTC)
    changed: list[str] = []
    error: str | None = None

    try:
        current = get_snapshot()
        state = current.sources.get(sheets.SOURCE_MASTER, SourceState(sheets.SOURCE_MASTER))
        spreadsheet_id, _ = sheets.source_ref(sheets.SOURCE_MASTER)

        # The content hash is the only change signal: it is exact and immediate,
        # unlike Drive's modifiedTime (see the module docstring).
        grid = sheets.read_source(sheets.SOURCE_MASTER)
        digest = content_hash(grid)
        if digest == state.content_hash and not force:
            return False

        parsed, layout = parse_dataset(grid)
        rows = annotate_all(parsed)
        findings = analyze(rows)
        now = datetime.now(UTC).isoformat()

        with _lock:
            global _snapshot
            revision = _snapshot.revision + 1
            _snapshot = Snapshot(
                revision=revision,
                changed_at=now,
                rows=rows,
                dimensions=collect_dimensions(rows),
                coverage=coverage(rows),
                warnings=[finding.to_dict() for finding in findings],
                warnings_summary=summarize(findings),
                layout=layout.to_dict() if layout else {},
                sources={
                    sheets.SOURCE_MASTER: SourceState(
                        source=sheets.SOURCE_MASTER,
                        content_hash=digest,
                        spreadsheet_id=spreadsheet_id,
                        fetched_at=now,
                        row_count=len(rows),
                    )
                },
            )
        changed.append(sheets.SOURCE_MASTER)
        _persist(sheets.SOURCE_MASTER, digest, revision, rows)
        log.info("BBC: snapshot %s — %s rows, %s warnings", revision, len(rows), len(findings))
        return True

    except BbcError as exc:
        # Человеческая формулировка, а не сырой текст Google API: этот текст
        # уезжает прямо в баннер над дашбордом.
        error = sheets.humanize(exc)
        _mark_error(sheets.SOURCE_MASTER, error)
        log.warning("BBC: refresh failed: %s", error)
        return False
    except Exception as exc:  # noqa: BLE001 — the loop must survive anything
        error = sheets.humanize(exc)
        _mark_error(sheets.SOURCE_MASTER, error)
        log.exception("BBC: unexpected refresh failure")
        return False
    finally:
        if changed or error:
            _record_run(started, changed, error)


def _mark_error(source: str, message: str) -> None:
    with _lock:
        state = _snapshot.sources.setdefault(source, SourceState(source))
        state.error = message


def ensure_loaded() -> Snapshot:
    """Load on first use, so an API call never waits for the background loop."""
    if not get_snapshot().rows:
        refresh(force=True)
    return get_snapshot()


__all__ = [
    "Snapshot",
    "SourceState",
    "ensure_loaded",
    "get_snapshot",
    "refresh",
    "revision_payload",
]
