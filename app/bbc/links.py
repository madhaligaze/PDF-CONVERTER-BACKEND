"""Referral links: scoped, read-only access for department heads.

One link = one department. The scope is stored next to the token **on the
server**, so the recipient cannot widen it — editing the URL changes nothing,
because the URL carries only an opaque token.

Lifetime follows the account page: creating a link without a duration makes it
permanent («публичная»), the clock control sets `expires_at`, and the cross
revokes it immediately.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.bbc.auth import hash_token, new_token
from app.bbc.config import bbc_settings
from app.bbc.db import bbc_session
from app.bbc.models import BbcAccessLink
from app.bbc.scope import DEPARTMENTS, LINK_BLOCKS, Scope, canonical_department

log = logging.getLogger(__name__)


class LinkError(Exception):
    """Invalid link request, with a user-facing message."""


@dataclass(frozen=True)
class LinkView:
    """A link as shown in the account page (never carries the raw token)."""

    id: str
    label: str
    departments: list[str]
    blocks: list[str]
    created_at: str | None
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    use_count: int
    is_active: bool
    url: str | None = None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:  # permanent link
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def build_url(token: str, base_url: str | None = None) -> str:
    base = (base_url or bbc_settings.public_base_url or "").rstrip("/")
    return f"{base}/bbc-dashboard?k={token}"


def _to_view(record: BbcAccessLink, now: datetime, url: str | None = None) -> LinkView:
    scope = record.scope if isinstance(record.scope, dict) else {}
    active = record.revoked_at is None and not _is_expired(record.expires_at, now)
    return LinkView(
        id=record.id,
        label=record.label,
        departments=list(scope.get("departments") or []),
        blocks=list(scope.get("blocks") or []),
        created_at=_iso(record.created_at),
        expires_at=_iso(record.expires_at),
        revoked_at=_iso(record.revoked_at),
        last_used_at=_iso(record.last_used_at),
        use_count=record.use_count or 0,
        is_active=active,
        url=url,
    )


# ── Management ───────────────────────────────────────────────────────────────────


def create_link(
    department: str,
    *,
    expires_in_hours: float | None = None,
    blocks: tuple[str, ...] = LINK_BLOCKS,
    created_by: int | None = None,
    base_url: str | None = None,
) -> LinkView:
    """Issue a link for one department. Returns the view **with** the raw URL.

    The raw token is shown exactly once, here — afterwards only its hash is kept.
    """
    code = canonical_department(department)
    if code is None:
        raise LinkError(f"Неизвестный отдел: {department!r}. Доступны: {', '.join(DEPARTMENTS)}")
    if expires_in_hours is not None and expires_in_hours <= 0:
        raise LinkError("Срок действия должен быть больше нуля")

    scope = Scope.for_departments([code], blocks, label=code)
    token = new_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=expires_in_hours) if expires_in_hours else None

    with bbc_session() as session:
        record = BbcAccessLink(
            id=secrets.token_hex(16),
            label=code,
            scope=scope.to_dict(),
            token_hash=hash_token(token),
            created_by=created_by,
            expires_at=expires_at,
        )
        session.add(record)
        session.flush()
        view = _to_view(record, now, url=build_url(token, base_url))

    log.info("BBC: access link for %s created (expires=%s)", code, expires_at or "never")
    return view


def list_links(base_url: str | None = None) -> list[LinkView]:
    """All links, newest first. URLs are absent — the token cannot be recovered."""
    now = datetime.now(UTC)
    with bbc_session() as session:
        records = session.scalars(
            select(BbcAccessLink).order_by(BbcAccessLink.created_at.desc())
        ).all()
        return [_to_view(record, now) for record in records]


def revoke_link(link_id: str) -> bool:
    """Kill a link immediately. Returns False when it does not exist."""
    with bbc_session() as session:
        record = session.get(BbcAccessLink, link_id)
        if record is None:
            return False
        if record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
        return True


# ── Resolution ───────────────────────────────────────────────────────────────────


def resolve_link(token: str | None) -> Scope | None:
    """Map a link token to its stored scope.

    Returns None for unknown, revoked or expired tokens — callers must then fall
    back to `Scope.denied()`, never to full access.
    """
    if not token:
        return None
    now = datetime.now(UTC)
    with bbc_session() as session:
        record = session.scalar(
            select(BbcAccessLink).where(BbcAccessLink.token_hash == hash_token(token))
        )
        if record is None or record.revoked_at is not None:
            return None
        if _is_expired(record.expires_at, now):
            return None

        record.last_used_at = now
        record.use_count = (record.use_count or 0) + 1
        # Rebuilt through from_dict so a tampered/legacy row degrades to denied
        # rather than being trusted as-is.
        return Scope.from_dict(record.scope)


__all__ = [
    "LinkError",
    "LinkView",
    "build_url",
    "create_link",
    "list_links",
    "resolve_link",
    "revoke_link",
]
