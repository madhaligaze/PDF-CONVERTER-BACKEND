"""SQLAlchemy models for the BBC module — everything lives in the `bbc` schema.

Two groups:

* **Access** — `users`, `user_sessions`, `access_links`, `audit_log`. Needed now:
  the dashboard is behind a login and department heads get scoped referral links.
* **History** — `sheet_snapshots`, `sync_runs`. Google Sheets is still the source
  of truth, but the live poll already computes a content hash, so storing a
  snapshot whenever that hash changes buys a full change history for free — and
  becomes the foundation for moving the domain into Postgres later.

`JSON` (not `JSONB`) is deliberate: the test suite runs on SQLite, and the payloads
are read whole rather than queried by key.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.bbc.db import BBC_SCHEMA, BbcBase


def _now() -> datetime:
    return datetime.now(UTC)


# ── Access ───────────────────────────────────────────────────────────────────────


class BbcUser(BbcBase):
    """A dashboard operator. Today that is the admin; `role` leaves room for more."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # argon2id hash — never the password itself.
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BbcUserSession(BbcBase):
    """Server-side session for a logged-in user (cookie carries the raw token)."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{BBC_SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of the cookie token: a database leak must not hand out live sessions.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BbcAccessLink(BbcBase):
    """A referral link handed to a department head.

    The visibility scope is bound to the token **here, on the server** — never to
    anything the recipient can edit. `expires_at IS NULL` means the link is
    permanent ("публичная"); setting it makes the link temporary.
    """

    __tablename__ = "access_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Short department label shown on the button: ОБО / НО / ЮО / HR / ФО.
    label: Mapped[str] = mapped_column(String(32), index=True)
    # {"departments": ["НО"], "blocks": ["receivables", "analytics", "calendar"]}
    scope: Mapped[dict] = mapped_column(JSON)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The address itself, so the account page can show it again after a reload.
    # Deliberate trade-off (see migration 0005): resolution still goes through
    # `token_hash`, but a working link now sits in the database in the clear, so
    # database access equals access to the departments' data.
    token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{BBC_SCHEMA}.users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class BbcAuditEntry(BbcBase):
    """Who saw what, through which credential. Append-only."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # "user" | "link" | "anon"
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    # Snapshot of the scope actually applied — so an audit can prove isolation held.
    scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── History ──────────────────────────────────────────────────────────────────────


class BbcSheetSnapshot(BbcBase):
    """One version of one worksheet, stored only when its content hash changes."""

    __tablename__ = "sheet_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # master|journal|sales|omip
    spreadsheet_id: Mapped[str] = mapped_column(String(64))
    worksheet: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # Normalized rows, not the raw grid — markedly smaller and directly usable.
    payload: Mapped[dict] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_bbc_snapshot_source_revision", "source", "revision"),
        {"schema": BBC_SCHEMA},
    )


class BbcSyncRun(BbcBase):
    """Provenance for each pass of the background poll loop."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Sources whose content actually changed on this pass ([] on a no-op poll).
    changed_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "BbcAccessLink",
    "BbcAuditEntry",
    "BbcSheetSnapshot",
    "BbcSyncRun",
    "BbcUser",
    "BbcUserSession",
]
