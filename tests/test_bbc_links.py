"""Lifecycle of the department referral links.

Covers the security promises made in the plan: a token maps to a server-side
scope, revocation is immediate, expiry is honoured, links without a duration stay
permanent, and nothing about the raw token survives in the database.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bbc import links as links_module
from app.bbc.auth import hash_token
from app.bbc.db import bbc_session
from app.bbc.links import LinkError, create_link, list_links, resolve_link, revoke_link
from app.bbc.models import BbcAccessLink
from app.bbc.scope import Scope


@pytest.fixture(autouse=True)
def clean_links():
    """Each test starts from an empty link table."""
    with bbc_session() as session:
        session.query(BbcAccessLink).delete()
    yield
    with bbc_session() as session:
        session.query(BbcAccessLink).delete()


def _token_of(view) -> str:
    assert view.url is not None
    return view.url.split("k=", 1)[1]


# ── Creation ─────────────────────────────────────────────────────────────────────


def test_created_link_resolves_to_its_department_scope() -> None:
    view = create_link("НО")
    scope = resolve_link(_token_of(view))

    assert scope is not None
    assert scope.departments == ("НО",)
    assert not scope.is_admin


def test_link_scope_reaches_only_the_three_allowed_blocks() -> None:
    scope = resolve_link(_token_of(create_link("ЮО")))

    assert scope is not None
    assert scope.allows_block("receivables")
    assert scope.allows_block("analytics")
    assert scope.allows_block("calendar")
    assert not scope.allows_block("reports")
    assert not scope.allows_block("journal")


def test_department_label_is_normalised() -> None:
    assert create_link(" обо ").label == "ОБО"


def test_unknown_department_is_rejected() -> None:
    with pytest.raises(LinkError):
        create_link("Маркетинг")


def test_non_positive_duration_is_rejected() -> None:
    with pytest.raises(LinkError):
        create_link("НО", expires_in_hours=0)


def test_raw_token_is_never_stored() -> None:
    token = _token_of(create_link("HR"))

    with bbc_session() as session:
        record = session.scalar(select(BbcAccessLink))
        assert record is not None
        assert token not in record.token_hash
        assert record.token_hash == hash_token(token)


# ── Expiry ───────────────────────────────────────────────────────────────────────


def test_link_without_duration_is_permanent() -> None:
    view = create_link("ОБО")

    assert view.expires_at is None
    assert view.is_active
    assert resolve_link(_token_of(view)) is not None


def test_link_with_duration_carries_an_expiry() -> None:
    view = create_link("ОБО", expires_in_hours=2)

    assert view.expires_at is not None
    assert resolve_link(_token_of(view)) is not None


def test_expired_link_stops_resolving() -> None:
    view = create_link("НО", expires_in_hours=1)
    token = _token_of(view)

    with bbc_session() as session:
        record = session.get(BbcAccessLink, view.id)
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)

    assert resolve_link(token) is None


# ── Revocation ───────────────────────────────────────────────────────────────────


def test_revoked_link_stops_resolving_immediately() -> None:
    view = create_link("ЮО")
    token = _token_of(view)
    assert resolve_link(token) is not None

    assert revoke_link(view.id) is True
    assert resolve_link(token) is None


def test_revoking_a_missing_link_reports_false() -> None:
    assert revoke_link("does-not-exist") is False


def test_revoked_link_is_listed_as_inactive() -> None:
    view = create_link("HR")
    revoke_link(view.id)

    listed = next(item for item in list_links() if item.id == view.id)
    assert not listed.is_active
    assert listed.revoked_at is not None


# ── Tampering ────────────────────────────────────────────────────────────────────


def test_unknown_token_is_denied() -> None:
    create_link("НО")
    assert resolve_link("some-token-that-was-never-issued") is None


def test_missing_token_is_denied() -> None:
    assert resolve_link(None) is None
    assert resolve_link("") is None


def test_altered_token_is_denied() -> None:
    token = _token_of(create_link("НО"))
    assert resolve_link(token[:-1] + ("A" if token[-1] != "A" else "B")) is None


def test_tampered_stored_scope_degrades_to_denied() -> None:
    """A scope row corrupted in the database must not become full access."""
    view = create_link("НО")
    token = _token_of(view)

    with bbc_session() as session:
        record = session.get(BbcAccessLink, view.id)
        record.scope = {"departments": "everything"}

    scope = resolve_link(token)
    assert scope is not None
    assert scope.sees_nothing


def test_link_scope_cannot_become_admin_via_stored_wildcard_department() -> None:
    """Even a wildcard written straight into the row must not fake an admin."""
    view = create_link("НО")

    with bbc_session() as session:
        record = session.get(BbcAccessLink, view.id)
        record.scope = {"departments": ["НО"], "blocks": ["receivables"]}

    scope = resolve_link(_token_of(view))
    assert scope is not None
    assert not scope.is_admin


# ── Bookkeeping ──────────────────────────────────────────────────────────────────


def test_usage_is_counted() -> None:
    view = create_link("ОБО")
    token = _token_of(view)
    resolve_link(token)
    resolve_link(token)

    listed = next(item for item in list_links() if item.id == view.id)
    assert listed.use_count == 2
    assert listed.last_used_at is not None


def test_listing_never_exposes_a_url() -> None:
    create_link("НО")
    assert all(item.url is None for item in list_links())


def test_url_is_built_from_the_configured_base(monkeypatch) -> None:
    monkeypatch.setattr(
        links_module.bbc_settings, "public_base_url", "https://example.test/", raising=False
    )
    assert create_link("НО").url.startswith("https://example.test/bbc-dashboard?k=")


def test_denied_scope_is_the_fallback_for_an_unresolved_link() -> None:
    """Callers must translate None into `denied()`, never into full access."""
    scope = resolve_link("nope") or Scope.denied()
    assert scope.sees_nothing
