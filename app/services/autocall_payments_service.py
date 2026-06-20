"""AutoCall.kz balance ledger → Google Sheets («Пополнения» worksheet).

The cabinet's balance movements live behind the web session (the API token only
exposes /autocalls), so we log in with phone+password, scrape the /payment ledger
and append every operation to a single sheet, split into typed columns:

    Дата и время | Пополнение | Возврат | Расход | Описание

Each row is one transaction; the amount lands in exactly one of the three money
columns (as a real, summable number) so each column totals independently:
  • Пополнение — top-ups ("Пополнение через систему …")
  • Возврат    — any other credit (refunds for calls/SMS, registration bonus)
  • Расход     — every debit (SMS sends, freezes for campaigns, spam penalties)

Primary purpose: audit — compare real top-ups against who claims to have paid,
catching people who say they topped up but took cash instead.

Dedup: rows have no id, so we key on a hash of (timestamp, amount, description).
Timestamps are second-precise, so collisions are effectively impossible.
"""
from __future__ import annotations

import hashlib
import logging
import re

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import db_session
from app.models.persistence import AutocallTopupSyncRecord
from app.services.autocall_service import (
    _AMOUNT_FORMAT,
    AutocallError,
    _parse_cost,
    get_or_create_worksheet,
    open_spreadsheet,
)

log = logging.getLogger(__name__)

_LOGIN_URL = "https://autocall.kz/login"
_PAYMENT_URL = "https://autocall.kz/payment"
_TOPUP_MARKER = "Пополн"  # "Пополнение через систему …"
_TOKEN_RE = re.compile(r'name="_token"[^>]*value="([^"]+)"')
_ROW_RE = re.compile(r'<tr[^>]*class="([^"]*)"[^>]*>(.*?)</tr>', re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PAGE_RE = re.compile(r"payment\?page=(\d+)")

_HEADER_ROW = ["Дата и время", "Пополнение", "Возврат", "Расход", "Описание"]
_APPEND_CHUNK = 2000  # rows per Sheets append call (first sync is ~8k rows)


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


def _fetch_ledger() -> list[dict]:
    """Log in, walk every page of /payment, return all operations (newest first)."""
    rows: list[dict] = []
    with httpx.Client(
        timeout=settings.autocall_http_timeout_seconds, follow_redirects=True
    ) as client:
        _login(client)
        first = client.get(f"{_PAYMENT_URL}?page=1")
        last_page = max((int(p) for p in _PAGE_RE.findall(first.text)), default=1)
        for pg in range(1, last_page + 1):
            html = first.text if pg == 1 else client.get(f"{_PAYMENT_URL}?page={pg}").text
            for _cls, body in _ROW_RE.findall(html):
                cells = [_clean(td) for td in _TD_RE.findall(body)]
                if len(cells) < 3:
                    continue
                rows.append({"date_time": cells[0], "amount": cells[1], "desc": cells[2]})
    return rows


def _classify(amount: float | None, desc: str) -> str:
    """Return one of 'topup' | 'refund' | 'expense'."""
    if _TOPUP_MARKER in desc:
        return "topup"
    if amount is not None and amount < 0:
        return "expense"
    return "refund"  # any other credit (call/SMS refunds, registration bonus)


def _key(date_time: str, amount: float | None, desc: str) -> str:
    """Stable dedup key, reconstructable from both a scraped row and a sheet row."""
    amt = f"{abs(amount):.2f}" if amount is not None else ""
    return hashlib.sha1(f"{date_time}|{amt}|{desc}".encode()).hexdigest()[:32]


def _entry_key(row: dict) -> str:
    return _key(row["date_time"], _parse_cost(row["amount"]), row["desc"])


def _sheet_row_key(sheet_row: list) -> str | None:
    """Reconstruct the dedup key from a written sheet row [dt, topup, refund, expense, desc]."""
    cells = list(sheet_row) + [""] * (5 - len(sheet_row))
    date_time, desc = cells[0], cells[4]
    if not date_time.strip():
        return None
    amount = next((_parse_cost(c) for c in cells[1:4] if c.strip()), None)
    return _key(date_time, amount, desc)


def _read_sheet_keys(worksheet) -> set[str]:
    """Keys already present in the sheet — the source of truth for dedup."""
    keys: set[str] = set()
    for row in worksheet.get_all_values()[1:]:  # skip header
        key = _sheet_row_key(row)
        if key:
            keys.add(key)
    return keys


def _to_row(row: dict) -> list:
    """Build a sheet row with the amount in its typed column (positive, summable)."""
    cost = _parse_cost(row["amount"])
    kind = _classify(cost, row["desc"])
    value = abs(cost) if cost is not None else row["amount"]
    topup = value if kind == "topup" else ""
    refund = value if kind == "refund" else ""
    expense = value if kind == "expense" else ""
    return [row["date_time"], topup, refund, expense, row["desc"]]


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

def _append_rows(worksheet, rows: list[list]) -> None:
    if not rows:
        return
    try:
        existing = worksheet.get_values("A1:E1")
        is_empty = not any(cell.strip() for row in existing for cell in row)
        if is_empty:
            worksheet.append_rows([_HEADER_ROW], value_input_option="USER_ENTERED")
        for start in range(0, len(rows), _APPEND_CHUNK):
            worksheet.append_rows(
                rows[start : start + _APPEND_CHUNK], value_input_option="USER_ENTERED"
            )
        try:
            worksheet.format("B2:D", _AMOUNT_FORMAT)  # keep the money columns summable
        except Exception:  # noqa: BLE001 — formatting is cosmetic
            pass
    except Exception as exc:  # noqa: BLE001
        raise AutocallError(f"Не удалось записать операции в Google Sheets: {exc}") from exc


# ── Public API ───────────────────────────────────────────────────────────────────

def sync_ledger() -> dict:
    """Fetch new balance operations and append them to the «Пополнения» worksheet.

    Dedup is against the sheet itself (plus the DB as a fast-path), so re-runs never
    duplicate — even if the backend DB was reset between deploys.

    Returns {added, skipped, total_seen, last_date}.
    """
    entries = _fetch_ledger()
    worksheet = get_or_create_worksheet(
        open_spreadsheet(), settings.google_sheets_topups_worksheet_name, cols=5
    )
    seen = _read_sheet_keys(worksheet) | _get_synced_keys()

    fresh: list[tuple[str, dict]] = []
    for row in entries:
        key = _entry_key(row)
        if key not in seen:
            fresh.append((key, row))
            seen.add(key)  # guard against duplicates within this same batch
    # Oldest first so the sheet reads chronologically downward.
    fresh.reverse()

    _append_rows(worksheet, [_to_row(row) for _key, row in fresh])
    for key, row in fresh:
        _record_synced(key, row)

    last_date = fresh[-1][1]["date_time"] if fresh else None
    log.info("autocall ledger sync: added=%s total=%s", len(fresh), len(entries))
    return {
        "added": len(fresh),
        "skipped": len(entries) - len(fresh),
        "total_seen": len(entries),
        "last_date": last_date,
    }


def get_metrics() -> dict:
    """Read-only summary for the UI (no writes)."""
    entries = _fetch_ledger()
    synced = _get_synced_keys()
    pending = sum(1 for r in entries if _entry_key(r) not in synced)

    totals = {"topup": 0.0, "refund": 0.0, "expense": 0.0}
    for r in entries:
        cost = _parse_cost(r["amount"])
        if cost is None:
            continue
        totals[_classify(cost, r["desc"])] += abs(cost)

    latest = entries[0] if entries else None
    return {
        "total_entries": len(entries),
        "pending_count": pending,
        "synced_count": len(synced),
        "total_topups": _format_amount(totals["topup"]),
        "total_refunds": _format_amount(totals["refund"]),
        "total_expenses": _format_amount(totals["expense"]),
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
