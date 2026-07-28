"""Referral links: scoped, read-only access for department heads.

One link = one department. The scope is stored next to the token **on the
server**, so the recipient cannot widen it — editing the URL changes nothing,
because the URL carries only an opaque token.

Lifetime follows the account page: creating a link without a duration makes it
permanent («публичная»), the clock control sets `expires_at` — at any time, not
only at creation — and the cross revokes it immediately.

The address is stored next to the link (`token`) so the page can show it again
after a reload; `token_hash` stays the lookup key. That is a deliberate
trade-off, spelled out in migration 0005: a working link is now readable by
anyone who can read the database.
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


def _to_view(
    record: BbcAccessLink,
    now: datetime,
    url: str | None = None,
    base_url: str | None = None,
) -> LinkView:
    scope = record.scope if isinstance(record.scope, dict) else {}
    active = record.revoked_at is None and not _is_expired(record.expires_at, now)
    # Мёртвой ссылке адрес не отдаём: отозванная или истёкшая ссылка в поле
    # «скопировать» — это приглашение отправить нерабочий адрес.
    if url is None and record.token and active:
        url = build_url(record.token, base_url)
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
    """Issue a link for one department. Returns the view **with** the URL."""
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
            token=token,
            created_by=created_by,
            expires_at=expires_at,
        )
        session.add(record)
        session.flush()
        view = _to_view(record, now, url=build_url(token, base_url))

    log.info("BBC: access link for %s created (expires=%s)", code, expires_at or "never")
    return view


def list_links(base_url: str | None = None) -> list[LinkView]:
    """All links, newest first. Active ones carry their address."""
    now = datetime.now(UTC)
    with bbc_session() as session:
        records = session.scalars(
            select(BbcAccessLink).order_by(BbcAccessLink.created_at.desc())
        ).all()
        return [_to_view(record, now, base_url=base_url) for record in records]


def set_link_expiry(
    link_id: str,
    *,
    expires_in_minutes: float | None,
    base_url: str | None = None,
) -> LinkView | None:
    """Give a live link a deadline, or take it away. Returns None if unknown.

    The address does not change: the point of this call is that a link already
    handed out can be made temporary without invalidating what the recipient
    already has. `None` makes it permanent again.
    """
    if expires_in_minutes is not None and expires_in_minutes <= 0:
        raise LinkError("Срок действия должен быть больше нуля")

    now = datetime.now(UTC)
    with bbc_session() as session:
        record = session.get(BbcAccessLink, link_id)
        if record is None:
            return None
        if record.revoked_at is not None:
            raise LinkError("Ссылка отозвана — задайте срок при выдаче новой")

        record.expires_at = (
            now + timedelta(minutes=expires_in_minutes) if expires_in_minutes else None
        )
        session.flush()
        view = _to_view(record, now, base_url=base_url)

    log.info("BBC: link %s expiry set to %s", link_id, view.expires_at or "never")
    return view


def revoke_link(link_id: str) -> bool:
    """Kill a link immediately. Returns False when it does not exist."""
    with bbc_session() as session:
        record = session.get(BbcAccessLink, link_id)
        if record is None:
            return False
        if record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
        # Отозванный адрес больше нигде не показывается, и хранить его незачем:
        # проверка доступа всё равно идёт по хэшу, который остаётся на месте.
        record.token = None
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


def describe_token(token: str | None) -> LinkView | None:
    """The link behind a token, for telling its holder when access ends."""
    if not token:
        return None
    now = datetime.now(UTC)
    with bbc_session() as session:
        record = session.scalar(
            select(BbcAccessLink).where(BbcAccessLink.token_hash == hash_token(token))
        )
        return None if record is None else _to_view(record, now)


__all__ = [
    "LinkError",
    "LinkView",
    "build_url",
    "create_link",
    "describe_token",
    "list_links",
    "resolve_link",
    "revoke_link",
    "set_link_expiry",
]
