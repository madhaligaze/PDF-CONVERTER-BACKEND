"""Pydantic contracts for the BBC Dashboard API.

Mirrored 1:1 by src/components/bbc-dashboard/types.ts on the frontend — keep both
in sync when the shape changes.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BbcStatus(BaseModel):
    """Health/config probe — lets the UI render a helpful message instead of a 500."""

    enabled: bool
    configured: bool
    spreadsheet_id: str | None = None
    worksheet_name: str = ""
    credentials_available: bool = False
    detail: str | None = None


class BbcSheetInfo(BaseModel):
    title: str
    index: int
    rows: int
    cols: int


class BbcSnapshot(BaseModel):
    """One read of the sheet: header row + data rows + derived metrics."""

    worksheet: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    row_count: int = 0
    fetched_at: str
    # Aggregations computed server-side; shape is intentionally open while the
    # dashboard's widgets are still being designed.
    metrics: dict[str, Any] = Field(default_factory=dict)


class BbcCellUpdate(BaseModel):
    range: str = Field(description="A1 notation, e.g. 'C12'")
    value: str = ""


class BbcUpdateRequest(BaseModel):
    worksheet: str | None = None
    updates: list[BbcCellUpdate] = Field(default_factory=list)


class BbcUpdateResult(BaseModel):
    applied: int
    detail: str | None = None


# ── Access ───────────────────────────────────────────────────────────────────────


class BbcLoginRequest(BaseModel):
    username: str
    password: str


class BbcCredentialsRequest(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None


class BbcMe(BaseModel):
    """Who is asking and what they may see — drives the whole frontend shell."""

    authenticated: bool = False
    username: str | None = None
    role: str | None = None
    # Present when the caller arrived through a referral link.
    link_label: str | None = None
    # When that link stops working, so the holder is not cut off mid-sentence.
    link_expires_at: str | None = None
    departments: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    is_admin: bool = False
    # True before the first admin exists — the UI then offers initial setup.
    needs_setup: bool = False


class BbcLinkCreateRequest(BaseModel):
    department: str = Field(description="ОБО / НО / ЮО / HR / ФО")
    # None = permanent ("публичная") link; a value makes it temporary.
    expires_in_hours: float | None = None


class BbcLinkExpiryRequest(BaseModel):
    """Change the deadline of a link that is already out there.

    Minutes rather than hours: the account page offers «15 минут», and expressing
    that as 0.25 hours is a rounding error waiting to happen.
    """

    # None makes the link permanent again.
    expires_in_minutes: float | None = None


class BbcLink(BaseModel):
    id: str
    label: str
    departments: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    created_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None
    use_count: int = 0
    is_active: bool = True
    # Only ever populated in the response that creates the link.
    url: str | None = None


class BbcOk(BaseModel):
    ok: bool = True
    detail: str | None = None
