"""AutoCall.kz → Google Sheets integration.

Pulls finished campaigns ("обзвоны") from the AutoCall.kz REST API and appends
one row per campaign to a Google Sheet, in the exact format the finance team uses:

    Дата (dd.mm.yyyy) | Проект (б/д) | Обзвоны Сумма (1 200,66)

Dedup is by autocall id (persisted in `autocall_syncs`) plus a cutoff date
(`settings.autocall_sync_since`) so we never re-append the rows already entered by
hand. Network / Google errors are surfaced to the caller with a readable message;
the daily background loop swallows them so one failure never crashes the app.

Field mapping (verified on campaign id=555239 "17.06 Д2"):
    created_at  -> Дата           (= "Дата создания" in the cabinet)
    name        -> Проект letter  (first Б/Д in the name, lowercased)
    final_cost  -> Сумма          (= "Фактическая стоимость")
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import db_session
from app.models.persistence import AutocallSyncRecord

log = logging.getLogger(__name__)

_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# The cabinet's campaign names are typed on whatever keyboard layout was active:
# "18.06 Д2" and "02.08 D2" mean the same project. Latin B/D are visually identical
# to Cyrillic Б/Д, so a Cyrillic-only class silently left the Проект column empty
# for every Latin-typed name — 155 of the 176 rows written so far.
_PROJECT_RE = re.compile(r"[БбBbДдDd]")
# Fold the Latin lookalikes onto the Cyrillic letters the sheet already uses.
_PROJECT_FOLD = {"b": "б", "d": "д"}
_THOUSANDS_SEP = " "  # regular space; flip to " " if the sheet needs nbsp


class AutocallError(Exception):
    """Raised for configuration / network / Google failures (carries a user message)."""


# ── Transform helpers ────────────────────────────────────────────────────────────

def _extract_project_letter(name: str | None) -> str:
    """First Б/Д in the campaign name as a lowercase Cyrillic letter, '' if none.

    Accepts the Latin lookalikes too, so '17.06 Д2' and '02.08 D2' both give 'д'.
    """
    if not name:
        return ""
    match = _PROJECT_RE.search(name)
    if not match:
        return ""
    letter = match.group(0).lower()
    return _PROJECT_FOLD.get(letter, letter)


def _parse_created_at(created_at: str | None) -> datetime | None:
    """Parse the API's 'YYYY-MM-DD HH:MM:SS' (or ISO) timestamp."""
    if not created_at:
        return None
    text = created_at.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(created_at.strip())
    except ValueError:
        return None


def _format_date(created_at: str | None) -> str:
    dt = _parse_created_at(created_at)
    return dt.strftime("%d.%m.%Y") if dt else ""


def _parse_cost(final_cost: object) -> float | None:
    """Robustly parse '1200.66', '1 200,66', nbsp-separated → 1200.66."""
    if final_cost is None:
        return None
    text = str(final_cost).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _format_amount(final_cost: object) -> str:
    """1200.66 -> '1 200,66' (thousands sep + comma decimal — finance-team format)."""
    value = _parse_cost(final_cost)
    if value is None:
        return str(final_cost or "")
    formatted = f"{value:,.2f}"  # '1,200.66'
    return formatted.replace(",", _THOUSANDS_SEP).replace(".", ",")


def _passes_cutoff(created_at: str | None) -> bool:
    dt = _parse_created_at(created_at)
    if dt is None:
        return False
    try:
        cutoff = datetime.strptime(settings.autocall_sync_since.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return True
    return dt.date() >= cutoff.date()


def _to_row(autocall: dict) -> list:
    # Amount must be a real number so Google Sheets can SUM it; the comma/space
    # "1 200,66" look comes from the column's number format, not a text value.
    cost = _parse_cost(autocall.get("final_cost"))
    return [
        _format_date(autocall.get("created_at")),
        _extract_project_letter(autocall.get("name")),
        cost if cost is not None else _format_amount(autocall.get("final_cost")),
    ]


# ── AutoCall API ─────────────────────────────────────────────────────────────────

async def _fetch_autocalls(stop_at_cutoff: bool = True) -> tuple[list[dict], int]:
    """Walk the Laravel paginator and return (campaigns, total_count).

    The API returns campaigns newest-first (by id/created_at desc), so when
    `stop_at_cutoff` is set we stop paginating as soon as a page's oldest item is
    older than `settings.autocall_sync_since` — there are ~150 pages of history we
    never need to read. `total_count` is taken from the paginator's `total` field
    so callers still know the full campaign count without fetching everything.
    """
    if not settings.autocall_configured:
        raise AutocallError("AutoCall не настроен: задайте AUTOCALL_ENABLED и AUTOCALL_API_KEY")

    headers = {
        "Authorization": f"Bearer {settings.autocall_api_key}",
        "Accept": "application/json",
    }
    url = f"{settings.autocall_api_url.rstrip('/')}/api/v1/autocalls"
    collected: list[dict] = []
    total = 0

    async with httpx.AsyncClient(timeout=settings.autocall_http_timeout_seconds) as client:
        page = 1
        while True:
            try:
                resp = await client.get(url, headers=headers, params={"page": page})
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPStatusError as exc:
                raise AutocallError(
                    f"AutoCall API вернул {exc.response.status_code} (проверьте API-ключ)"
                ) from exc
            except httpx.HTTPError as exc:
                raise AutocallError(f"Не удалось связаться с AutoCall API: {exc}") from exc

            is_paginator = isinstance(payload, dict)
            items = payload.get("data", []) if is_paginator else payload
            if is_paginator and isinstance(payload.get("total"), int):
                total = payload["total"]
            if not items:
                break
            collected.extend(items)

            # Newest-first: once the oldest item on this page predates the cutoff,
            # every later page is older still — stop early.
            if stop_at_cutoff and not _passes_cutoff(items[-1].get("created_at")):
                break

            # Laravel paginator: stop when there is no next page.
            if is_paginator:
                if not payload.get("next_page_url"):
                    break
                last = payload.get("last_page")
                if isinstance(last, int) and page >= last:
                    break
            else:
                break
            page += 1
            if page > 1000:  # safety valve
                break

    if not total:
        total = len(collected)
    return collected, total


# ── Google Sheets ────────────────────────────────────────────────────────────────

def open_spreadsheet():
    """Authorize via service account and return the gspread Spreadsheet."""
    if not settings.google_sheets_configured:
        raise AutocallError(
            "Google Sheets не настроен: задайте GOOGLE_SHEETS_SPREADSHEET_ID и "
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        )
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:  # pragma: no cover
        raise AutocallError("Пакеты gspread/google-auth не установлены") from exc

    raw = settings.google_service_account_json.strip()
    try:
        if raw.startswith("{"):
            info = json.loads(raw)
            creds = Credentials.from_service_account_info(info, scopes=_GOOGLE_SCOPES)
        else:
            creds = Credentials.from_service_account_file(raw, scopes=_GOOGLE_SCOPES)
    except (ValueError, OSError) as exc:
        raise AutocallError(f"Некорректные креды Google Service Account: {exc}") from exc

    try:
        client = gspread.authorize(creds)
        return client.open_by_key(settings.google_sheets_spreadsheet_id)
    except Exception as exc:  # noqa: BLE001 — gspread raises many types; wrap for the caller
        raise AutocallError(f"Не удалось открыть таблицу Google Sheets: {exc}") from exc


def get_or_create_worksheet(spreadsheet, name: str, cols: int = 3):
    """Return the named worksheet, creating it if missing. Empty name = first sheet."""
    name = (name or "").strip()
    if not name:
        return spreadsheet.sheet1
    try:
        return spreadsheet.worksheet(name)
    except Exception:  # noqa: BLE001 — gspread raises WorksheetNotFound; create it
        return spreadsheet.add_worksheet(title=name, rows=1000, cols=cols)


def _open_worksheet():
    """The autocalls worksheet (first sheet or GOOGLE_SHEETS_WORKSHEET_NAME)."""
    try:
        return get_or_create_worksheet(
            open_spreadsheet(), settings.google_sheets_worksheet_name
        )
    except AutocallError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AutocallError(f"Не удалось открыть таблицу Google Sheets: {exc}") from exc


_HEADER_ROW = ["Дата", "Проект", "Обзвоны Сумма"]
# Number format that renders 1200.66 as "1 200,66" while keeping it a real number.
_AMOUNT_FORMAT = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}


def _append_rows_to_sheet(worksheet, rows: list[list]) -> None:
    if not rows:
        return
    try:
        # Write the header once if the target sheet is still empty.
        # gspread returns [[]] / [['']] for blank cells, so check actual content.
        existing = worksheet.get_values("A1:C1")
        is_empty = not any(cell.strip() for row in existing for cell in row)
        if is_empty:
            worksheet.append_rows([_HEADER_ROW], value_input_option="USER_ENTERED")
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        # Ensure the amount column stays a summable number with the finance format.
        try:
            worksheet.format("C2:C", _AMOUNT_FORMAT)
        except Exception:  # noqa: BLE001 — formatting is cosmetic, never fail the sync
            pass
    except Exception as exc:  # noqa: BLE001
        raise AutocallError(f"Не удалось записать строки в Google Sheets: {exc}") from exc


def _is_settled(autocall: dict) -> bool:
    """True only when final_cost is populated (>0).

    AutoCall freezes max_cost during a campaign and refunds the unused part on
    completion; the real final_cost is billed with a delay, so a freshly finished
    campaign can report final_cost=0 for a while. Writing that 0 would lock in a
    wrong row (dedup never updates it), so we skip until it settles.
    """
    cost = _parse_cost(autocall.get("final_cost"))
    return cost is not None and cost > 0


def _autocall_key(date_str: str, project: str, amount: float | None) -> str:
    amt = f"{amount:.2f}" if amount is not None else ""
    return f"{date_str}|{project}|{amt}"


def _already_in_sheet(seen: set[str], date_str: str, project: str, amount: float | None) -> bool:
    """Is this campaign already a row in the sheet?

    Rows written before the Latin-B/D fix carry an empty Проект cell, so their key
    is `date||amount` while the same campaign now keys as `date|б|amount`. Matching
    the blank variant too keeps those legacy rows from being appended a second time
    even if the backfill missed one. Date+amount stays unique in practice: the only
    same-day/same-amount clashes are the 0,00 campaigns, which `_is_settled` drops.
    """
    if _autocall_key(date_str, project, amount) in seen:
        return True
    return bool(project) and _autocall_key(date_str, "", amount) in seen


def _read_autocall_sheet_keys(worksheet) -> set[str]:
    """Keys already present in the «Фактическая стоимость» sheet — dedup source of truth."""
    keys: set[str] = set()
    for row in worksheet.get_all_values()[1:]:  # skip header
        cells = list(row) + [""] * (3 - len(row))
        date_str, project, amount = cells[0], cells[1], cells[2]
        if not date_str.strip():
            continue
        keys.add(_autocall_key(date_str, project, _parse_cost(amount)))
    return keys


# ── Dedup persistence ────────────────────────────────────────────────────────────

def _get_synced_ids() -> set[str]:
    with db_session() as session:
        return set(session.scalars(select(AutocallSyncRecord.autocall_id)).all())


def _record_synced(autocall: dict) -> None:
    with db_session() as session:
        ac_id = str(autocall.get("id"))
        if session.get(AutocallSyncRecord, ac_id):
            return
        session.add(
            AutocallSyncRecord(
                autocall_id=ac_id,
                name=(autocall.get("name") or "")[:255],
                created_at_src=str(autocall.get("created_at") or "")[:32],
                final_cost=str(autocall.get("final_cost") or "")[:32],
                synced_at=datetime.now(UTC),
                status="success",
            )
        )


# ── Public API ───────────────────────────────────────────────────────────────────

async def sync_autocalls() -> dict:
    """Fetch new campaigns past the cutoff and append them to Google Sheets.

    Returns {added, skipped, total_seen, last_date}.
    """
    autocalls, total = await _fetch_autocalls(stop_at_cutoff=True)
    worksheet = _open_worksheet()
    # Dedup against the sheet itself (DB-independent): deleting a row re-syncs it.
    seen = _read_autocall_sheet_keys(worksheet)

    fresh = []
    for a in autocalls:
        if not _passes_cutoff(a.get("created_at")) or not _is_settled(a):
            continue
        date_str = _format_date(a.get("created_at"))
        project = _extract_project_letter(a.get("name"))
        amount = _parse_cost(a.get("final_cost"))
        if _already_in_sheet(seen, date_str, project, amount):
            continue
        seen.add(_autocall_key(date_str, project, amount))
        fresh.append(a)
    fresh.sort(key=lambda a: _parse_created_at(a.get("created_at")) or datetime.min)

    rows = [_to_row(a) for a in fresh]
    _append_rows_to_sheet(worksheet, rows)

    for autocall in fresh:
        _record_synced(autocall)

    last_date = rows[-1][0] if rows else None
    log.info("autocall sync: added=%s total=%s", len(fresh), total)
    return {
        "added": len(fresh),
        "skipped": len(autocalls) - len(fresh),
        "total_seen": total,
        "last_date": last_date,
    }


async def get_metrics() -> dict:
    """Read-only summary for the UI modal (no writes)."""
    # Only fetches pages back to the cutoff; `total` is the full campaign count.
    autocalls, total = await _fetch_autocalls(stop_at_cutoff=True)
    seen = _read_autocall_sheet_keys(_open_worksheet())

    eligible = [a for a in autocalls if _passes_cutoff(a.get("created_at"))]
    # Pending = settled (final_cost>0) and not yet in the sheet. Unsettled (0) ones
    # are intentionally held back until their cost is billed.
    pending = [
        a for a in eligible
        if _is_settled(a)
        and not _already_in_sheet(
            seen,
            _format_date(a.get("created_at")),
            _extract_project_letter(a.get("name")),
            _parse_cost(a.get("final_cost")),
        )
    ]

    total_cost = sum((_parse_cost(a.get("final_cost")) or 0.0) for a in eligible)

    latest = max(
        autocalls,
        key=lambda a: _parse_created_at(a.get("created_at")) or datetime.min,
        default=None,
    )
    latest_info = None
    if latest:
        latest_info = {
            "name": latest.get("name"),
            "date": _format_date(latest.get("created_at")),
            "final_cost": _format_amount(latest.get("final_cost")),
            "status": latest.get("status"),
        }

    return {
        "total_autocalls": total,
        "eligible_count": len(eligible),
        "pending_count": len(pending),
        "synced_count": len(seen),
        "total_cost": _format_amount(total_cost),
        "cutoff_date": settings.autocall_sync_since,
        "latest": latest_info,
    }


def get_last_sync() -> dict | None:
    with db_session() as session:
        record = session.scalar(
            select(AutocallSyncRecord).order_by(AutocallSyncRecord.synced_at.desc()).limit(1)
        )
        if not record:
            return None
        return {
            "autocall_id": record.autocall_id,
            "name": record.name,
            "synced_at": record.synced_at.isoformat(),
            "status": record.status,
        }
