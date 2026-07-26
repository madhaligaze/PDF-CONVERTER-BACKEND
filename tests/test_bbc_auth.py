"""Login, sessions and credential changes for the BBC dashboard."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bbc import auth as auth_module
from app.bbc.auth import (
    AuthError,
    change_credentials,
    ensure_bootstrap_admin,
    hash_password,
    hash_token,
    login,
    logout,
    purge_expired_sessions,
    resolve_session,
    verify_password,
)
from app.bbc.db import bbc_session
from app.bbc.models import BbcUser, BbcUserSession

PASSWORD = "secret123"


@pytest.fixture(autouse=True)
def clean_users():
    def _wipe():
        with bbc_session() as session:
            session.query(BbcUserSession).delete()
            session.query(BbcUser).delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def admin() -> BbcUser:
    with bbc_session() as session:
        user = BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin")
        session.add(user)
        session.flush()
        session.expunge(user)
        return user


# ── Hashing ──────────────────────────────────────────────────────────────────────


def test_password_hash_is_not_the_password() -> None:
    digest = hash_password(PASSWORD)
    assert PASSWORD not in digest
    assert verify_password(digest, PASSWORD)
    assert not verify_password(digest, "wrong")


def test_hashing_is_salted() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_survives_a_corrupt_hash() -> None:
    assert not verify_password("not-a-hash", PASSWORD)


# ── Login ────────────────────────────────────────────────────────────────────────


def test_login_returns_a_working_session(admin) -> None:
    user = resolve_session(login("admin", PASSWORD))

    assert user is not None
    assert user.username == "admin"
    assert user.role == "admin"


def test_wrong_password_is_rejected(admin) -> None:
    with pytest.raises(AuthError):
        login("admin", "wrong")


def test_unknown_user_is_rejected(admin) -> None:
    with pytest.raises(AuthError):
        login("nobody", PASSWORD)


def test_error_message_does_not_reveal_which_field_was_wrong(admin) -> None:
    """Login enumeration guard: both failures read the same."""
    with pytest.raises(AuthError) as wrong_password:
        login("admin", "wrong")
    with pytest.raises(AuthError) as unknown_user:
        login("nobody", PASSWORD)

    assert str(wrong_password.value) == str(unknown_user.value)


def test_disabled_account_cannot_log_in(admin) -> None:
    with bbc_session() as session:
        session.get(BbcUser, admin.id).is_active = False

    with pytest.raises(AuthError):
        login("admin", PASSWORD)


def test_garbage_token_resolves_to_nothing(admin) -> None:
    login("admin", PASSWORD)
    assert resolve_session("garbage") is None
    assert resolve_session(None) is None


def test_raw_session_token_is_never_stored(admin) -> None:
    token = login("admin", PASSWORD)

    with bbc_session() as session:
        record = session.scalar(select(BbcUserSession))
        assert record is not None
        assert record.token_hash == hash_token(token)
        assert token not in record.token_hash


def test_logout_ends_the_session(admin) -> None:
    token = login("admin", PASSWORD)
    logout(token)
    assert resolve_session(token) is None


def test_expired_session_is_rejected_and_cleaned(admin) -> None:
    token = login("admin", PASSWORD)

    with bbc_session() as session:
        record = session.scalar(select(BbcUserSession))
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)

    assert resolve_session(token) is None


def test_purge_removes_only_expired_sessions(admin) -> None:
    stale = login("admin", PASSWORD)
    fresh = login("admin", PASSWORD)

    with bbc_session() as session:
        record = session.scalar(
            select(BbcUserSession).where(BbcUserSession.token_hash == hash_token(stale))
        )
        record.expires_at = datetime.now(UTC) - timedelta(hours=1)

    assert purge_expired_sessions() == 1
    assert resolve_session(fresh) is not None


# ── Credential changes ───────────────────────────────────────────────────────────


def test_password_change_requires_the_current_password(admin) -> None:
    with pytest.raises(AuthError):
        change_credentials(admin.id, current_password="wrong", new_password="brandnew123")


def test_password_change_invalidates_existing_sessions(admin) -> None:
    token = login("admin", PASSWORD)
    change_credentials(admin.id, current_password=PASSWORD, new_password="brandnew123")

    assert resolve_session(token) is None
    assert resolve_session(login("admin", "brandnew123")) is not None


def test_username_change_works(admin) -> None:
    change_credentials(admin.id, current_password=PASSWORD, new_username="director")
    assert resolve_session(login("director", PASSWORD)) is not None


def test_short_password_is_rejected(admin) -> None:
    with pytest.raises(AuthError):
        change_credentials(admin.id, current_password=PASSWORD, new_password="short")


def test_short_username_is_rejected(admin) -> None:
    with pytest.raises(AuthError):
        change_credentials(admin.id, current_password=PASSWORD, new_username="ab")


def test_duplicate_username_is_rejected(admin) -> None:
    with bbc_session() as session:
        session.add(BbcUser(username="taken", password_hash=hash_password(PASSWORD)))

    with pytest.raises(AuthError):
        change_credentials(admin.id, current_password=PASSWORD, new_username="taken")


def test_empty_change_request_is_rejected(admin) -> None:
    with pytest.raises(AuthError):
        change_credentials(admin.id, current_password=PASSWORD)


# ── Bootstrap ────────────────────────────────────────────────────────────────────


def test_bootstrap_creates_the_first_admin(monkeypatch) -> None:
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_admin", "root", raising=False)
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_password", PASSWORD, raising=False)

    ensure_bootstrap_admin()
    assert resolve_session(login("root", PASSWORD)) is not None


def test_bootstrap_is_ignored_once_a_user_exists(admin, monkeypatch) -> None:
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_admin", "root", raising=False)
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_password", PASSWORD, raising=False)

    ensure_bootstrap_admin()

    with bbc_session() as session:
        assert session.query(BbcUser).count() == 1


def test_bootstrap_without_settings_does_nothing(monkeypatch) -> None:
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_admin", "", raising=False)
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_password", "", raising=False)

    ensure_bootstrap_admin()

    with bbc_session() as session:
        assert session.query(BbcUser).count() == 0
