"""AutoCall.kz balance top-ups → Google Sheets ("Пополнения" worksheet).

Top-ups live behind the web session (the API token only exposes /autocalls), so we
log into the cabinet with phone+password, scrape the /payment ledger, keep only the
"Пополнение …" rows (actual balance top-ups, not call charges or refunds) and append
them to a dedicated worksheet:

    Дата и время | Сумма | Описание

This lets the finance team audit every real top-up against who claims to have paid —
catching cases where someone says they topped up but took cash instead.

Dedup: top-up rows have no id, so we key on a hash of (timestamp, amount, description).
Timestamps are second-precise, so collisions are effectively impossible.
"""
from __future__ import annotations

import hashlib
import logging
import re

import httpx

from app.core.config import settings
from app.core.database import db_session
from app.models.persistence import AutocallTopupSyncRecord
from app.services.autocall_service import (
    AutocallError,
    _AMOUNT_FORMAT,
    _parse_cost,
    get_or_create_worksheet,
    open_spreadsheet,
)
from sqlalchemy import select

log = logging.getLogger(__name__)

_LOGIN_URL = "https://autocall.kz/login"
_PAYMENT_URL = "https://autocall.kz/payment"
_TOPUP_MARKER = "Пополн"  # matches "Пополнение через систему …"
_TOKEN_RE = re.compile(r'name="_token"[^>]*value="([^"]+)"')
_ROW_RE = re.compile(r'<tr[^>]*class="([^"]*)"[^>]*>(.*?)</tr>', re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PAGE_RE = re.compile(r"payment\?page=(\d+)")

_HEADER_ROW = ["Дата и время", "Сумма", "Описание"]


# ── Scrape ───────────────────────────────────────────────────────────────────────

def _clean(html_fragment: str) -> str:
    return _TAG_RE.sub("", html_fragment).strip().replace("&quot;", '"').replace("&amp;", "&")


def _login(client: httpx.Client) -> None:
    if not settings.autocall_session_configured:
        raise AutocallError(
            "Не заданы логин/пароль кабинета: AUTOCALL_LOGIN и AUTOCALL_PASSWORD"
        )
    resp = client.get(_LOGIN_URL)
    match = _TOKEN_RE.search(resp.text)
    if not match:
        raise AutocallError("Не удалось получить CSRF-токен страницы входа")
    resp = client.post(
        _LOGIN_URL,
        data={
            "_token": match.group(1),
            "phone": settings.autocall_login,
            "password": settings.autocall_password,
        },
    )
    if "login" in str(resp.url):
        raise AutocallError("Не удалось войти в кабинет (проверьте логин/пароль)")


def _fetch_topups() -> list[dict]:
    """Log in, walk every page of /payment, return the top-up rows (newest first)."""
    topups: list[dict] = []
    with httpx.Client(
        timeout=settings.autocall_http_timeout_seconds, follow_redirects=True
    ) as client:
        _login(client)
        first = client.get(f"{_PAYMENT_URL}?page=1")
        last_page = max((int(p) for p in _PAGE_RE.findall(first.text)), default=1)
        pages = [(1, first)] + [
            (pg, None) for pg in range(2, last_page + 1)
        ]
        for pg, resp in pages:
            html = resp.text if resp is not None else client.get(f"{_PAYMENT_URL}?page={pg}").text
            for _cls, body in _ROW_RE.findall(html):
                cells = [_clean(td) for td in _TD_RE.findall(body)]
                if len(cells) < 3:
                    continue
                date_time, amount, desc = cells[0], cells[1], cells[2]
                if _TOPUP_MARKER in desc:
                    topups.append({"date_time": date_time, "amount": amount, "desc": desc})
    return topups


def _topup_key(row: dict) -> str:
    raw = f"{row['date_time']}|{row['amount']}|{row['desc']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _to_row(row: dict) -> list:
    cost = _parse_cost(row["amount"])
    return [row["date_time"], cost if cost is not None else row["amount"], row["desc"]]


# ── Dedup persistence ────────────────────────────────────────────────────────────

def _get_synced_keys() -> set[str]:
    with db_session() as session:
        return set(session.scalars(select(AutocallTopupSyncRecord.topup_key)).all())


def _record_synced(key: str, row: dict) -> None:
    with db_session() as session:
        if session.get(AutocallTopupSyncRecord, key):
            return
        session.add(
            AutocallTopupSyncRecord(
                topup_key=key,
                date_time=row["date_time"][:32],
                amount=str(row["amount"])[:32],
                description=row["desc"][:255],
            )
        )


# ── Google Sheets ────────────────────────────────────────────────────────────────

def _append_rows(rows: list[list]) -> None:
    if not rows:
        return
    worksheet = get_or_create_worksheet(
        open_spreadsheet(), settings.google_sheets_topups_worksheet_name
    )
    try:
        existing = worksheet.get_values("A1:C1")
        is_empty = not any(cell.strip() for row in existing for cell in row)
        if is_empty:
            worksheet.append_rows([_HEADER_ROW], value_input_option="USER_ENTERED")
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        try:
            worksheet.format("B2:B", _AMOUNT_FORMAT)  # keep the amount summable
        except Exception:  # noqa: BLE001 — formatting is cosmetic
            pass
    except Exception as exc:  # noqa: BLE001
        raise AutocallError(f"Не удалось записать пополнения в Google Sheets: {exc}") from exc


# ── Public API ───────────────────────────────────────────────────────────────────

def sync_topups() -> dict:
    """Fetch new top-ups and append them to the «Пополнения» worksheet.

    Returns {added, skipped, total_seen, last_date}.
    """
    topups = _fetch_topups()
    synced = _get_synced_keys()

    fresh: list[tuple[str, dict]] = []
    for row in topups:
        key = _topup_key(row)
        if key not in synced:
            fresh.append((key, row))
    # Oldest first so the sheet reads chronologically downward.
    fresh.reverse()

    _append_rows([_to_row(row) for _key, row in fresh])
    for key, row in fresh:
        _record_synced(key, row)

    last_date = fresh[-1][1]["date_time"] if fresh else None
    log.info("autocall topups sync: added=%s total=%s", len(fresh), len(topups))
    return {
        "added": len(fresh),
        "skipped": len(topups) - len(fresh),
        "total_seen": len(topups),
        "last_date": last_date,
    }


def get_metrics() -> dict:
    """Read-only summary for the UI (no writes)."""
    topups = _fetch_topups()
    synced = _get_synced_keys()
    pending = [r for r in topups if _topup_key(r) not in synced]
    total = sum((_parse_cost(r["amount"]) or 0.0) for r in topups)
    latest = topups[0] if topups else None
    return {
        "total_topups": len(topups),
        "pending_count": len(pending),
        "synced_count": len(synced),
        "total_amount": _format_amount(total),
        "latest": (
            {
                "date_time": latest["date_time"],
                "amount": _format_amount(_parse_cost(latest["amount"]) or 0.0),
                "desc": latest["desc"],
            }
            if latest
            else None
        ),
    }


def _format_amount(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")
