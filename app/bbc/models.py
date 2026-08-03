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

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    """A dashboard operator: the admin, or an employee added by one.

    Three axes, deliberately orthogonal — collapsing any two of them into a
    single «роль» is what makes access models rot:

    * `role` — `admin` | `employee`. Admin bypasses every check below.
    * `blocks` — *what* you may open. Same vocabulary as `scope.BLOCKS`.
    * `data_scope` + `departments` + `employee_aliases` — *whose* rows you see.

    `employee_aliases` holds the spellings of this person in the sheet's
    «Сотрудник» column («Дана», «Дана Ж.», «Жумабекова Д.»). The sheet is typed
    by humans and the drift is real, so ownership is a list, never one string.
    """

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

    # ── Сотрудник ───────────────────────────────────────────────────────────
    #: «Дана Жумабекова» — подпись под касанием. Пустое у бутстрап-админа.
    full_name: Mapped[str] = mapped_column(String(120), default="")
    #: `active` | `dismissed`. Увольнение мягкое: касания уволенного остаются в
    #: журнале с его именем, а войти он больше не может.
    status: Mapped[str] = mapped_column(String(16), default="active")
    #: Пароль выдан админом и ещё не сменён. Пока True — доступа к данным нет.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    #: ["ОБО"] — коды из scope.DEPARTMENTS.
    departments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: ["receivables", "touches"] — ключи из scope.BLOCKS.
    blocks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: `own` | `department` | `all`.
    data_scope: Mapped[str] = mapped_column(String(16), default="own")
    #: Написания в колонке «Сотрудник», принадлежащие этому человеку.
    employee_aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)


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


# ── Касания ─────────────────────────────────────────────────────────────────────


class BbcClientTouch(BbcBase):
    """Одно обращение к должнику: кто, кому, когда, к чему пришли.

    Привязано к клиенту целиком, а не к договору. Так работают люди: звонят
    главбуху, а не «по договору №247». Разложить одну переписку по трём
    договорам всё равно не выйдет, а общая картина по клиенту потерялась бы.

    `client_key` — нормализованное имя (регистр и пробелы сняты), оно же ключ
    группировки в реестре дебиторки. `client_name` хранит написание на момент
    записи: в таблице оно дрейфует, и через полгода строка журнала должна
    читаться так, как её видел автор.

    `author_name` дублирует имя автора намеренно. Уволенного сотрудника можно
    удалить, FK обнулится — но «Жанара написала главбуху» обязано пережить
    её увольнение, иначе журнал теряет ровно то, ради чего заведён.
    """

    __tablename__ = "client_touches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_key: Mapped[str] = mapped_column(String(255), index=True)
    client_name: Mapped[str] = mapped_column(String(255))

    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{BBC_SCHEMA}.users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_name: Mapped[str] = mapped_column(String(120))

    #: Когда связались — не когда записали. Задним числом пишут постоянно.
    contacted_at: Mapped[date] = mapped_column(Date, index=True)
    #: Код должности со стороны клиента из touches.CONTACT_ROLES.
    contact_role: Mapped[str] = mapped_column(String(32), index=True)
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    #: whatsapp | call | email | meeting | other
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    summary: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    #: Мягкое удаление: история касаний — доказательная база по долгу.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_bbc_touch_client_date", "client_key", "contacted_at"),
        {"schema": BBC_SCHEMA},
    )


class BbcTouchFile(BbcBase):
    """Скрин или документ, приложенный к касанию.

    Байты живут либо в S3-совместимом хранилище (`storage_backend = "s3"`,
    `data` пустое), либо прямо здесь (`"postgres"`) — переключается настройкой,
    см. `app/bbc/storage.py`. Файловая система контейнера не используется
    никогда: на Railway она стирается при каждом деплое.

    Отдаются файлы своим эндпоинтом с проверкой прав, а не публичной ссылкой:
    скрин переписки о долге не должен открываться по угаданному адресу.
    """

    __tablename__ = "touch_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    touch_id: Mapped[int] = mapped_column(
        ForeignKey(f"{BBC_SCHEMA}.client_touches.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_backend: Mapped[str] = mapped_column(String(16), default="postgres")
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{BBC_SCHEMA}.users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    "BbcClientTouch",
    "BbcSheetSnapshot",
    "BbcSyncRun",
    "BbcTouchFile",
    "BbcUser",
    "BbcUserSession",
]
