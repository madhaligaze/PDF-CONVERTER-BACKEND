"""Live currency rates from the National Bank of Kazakhstan (auto-updating).

Replaces hardcoded 480/520 rates. The official RSS feed
(https://nationalbank.kz/rss/rates_all.xml) is fetched at most once per day and
cached in the DB; subsequent calls are served from cache. On any failure we fall
back to the last cached value, then to a static default — so a network outage
never breaks preview/export.
"""
from __future__ import annotations

import re
import urllib.request
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.database import db_session
from app.models.persistence import FxRateRecord

# In-process cache: code -> (iso_date, rate). Avoids a DB hit per formula row.
_MEM_CACHE: dict[str, tuple[str, float]] = {}


def _today() -> str:
    return date.today().isoformat()


def get_rate(code: str) -> float | None:
    """Price of one unit of `code` in KZT, or None if completely unavailable."""
    code = code.upper()
    today = _today()

    cached = _MEM_CACHE.get(code)
    if cached and cached[0] == today:
        return cached[1]

    db_today = _read_db(code, today)
    if db_today is not None:
        _MEM_CACHE[code] = (today, db_today)
        return db_today

    if settings.fx_rates_enabled:
        fetched = _fetch_all()
        if fetched:
            _store_db(fetched, today)
            for c, r in fetched.items():
                _MEM_CACHE[c] = (today, r)
            if code in fetched:
                return fetched[code]

    last_known = _read_db_latest(code)
    if last_known is not None:
        return last_known

    return settings.fx_fallback_rates.get(code)


def get_all_rates() -> dict[str, object]:
    """Snapshot for UI: rates + the date/source they came from."""
    today = _today()
    if settings.fx_rates_enabled and not _has_db_for(today):
        fetched = _fetch_all()
        if fetched:
            _store_db(fetched, today)
            for c, r in fetched.items():
                _MEM_CACHE[c] = (today, r)
    rates = {code: _read_db(code, today) for code in ("USD", "EUR", "RUB")}
    rates = {k: v for k, v in rates.items() if v is not None}
    if not rates:
        rates = dict(settings.fx_fallback_rates)
        return {"rates": rates, "date": None, "source": "fallback"}
    return {"rates": rates, "date": today, "source": "nationalbank.kz"}


# ── Network + parsing ───────────────────────────────────────────────────────────

def _fetch_all() -> dict[str, float]:
    try:
        req = urllib.request.Request(
            settings.fx_rates_url,
            headers={"User-Agent": "pdf-converter/1.0"},
        )
        with urllib.request.urlopen(req, timeout=settings.fx_rates_timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return _parse_rss(raw)
    except Exception:  # noqa: BLE001 — best-effort; callers fall back to cache/default
        return {}


def _parse_rss(xml_text: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    for block in re.findall(r"<item>(.*?)</item>", xml_text, re.S | re.I):
        title = re.search(r"<title>\s*([A-Za-z]{3})\s*</title>", block, re.I)
        desc = re.search(r"<description>\s*([\d.,]+)\s*</description>", block, re.I)
        quant = re.search(r"<quant>\s*(\d+)\s*</quant>", block, re.I)
        if not title or not desc:
            continue
        try:
            value = float(desc.group(1).replace(",", "."))
        except ValueError:
            continue
        nominal = int(quant.group(1)) if quant else 1
        if nominal > 0 and value > 0:
            rates[title.group(1).upper()] = value / nominal
    return rates


# ── DB cache ──────────────────────────────────────────────────────────────────

def _read_db(code: str, day: str) -> float | None:
    with db_session() as session:
        record = session.get(FxRateRecord, f"{code}:{day}")
        return record.rate if record else None


def _has_db_for(day: str) -> bool:
    with db_session() as session:
        return session.scalar(
            select(FxRateRecord.id).where(FxRateRecord.rate_date == day).limit(1)
        ) is not None


def _read_db_latest(code: str) -> float | None:
    with db_session() as session:
        record = session.scalar(
            select(FxRateRecord)
            .where(FxRateRecord.code == code)
            .order_by(FxRateRecord.rate_date.desc())
            .limit(1)
        )
        return record.rate if record else None


def _store_db(rates: dict[str, float], day: str) -> None:
    now = datetime.now(UTC)
    with db_session() as session:
        for code, rate in rates.items():
            rec_id = f"{code}:{day}"
            existing = session.get(FxRateRecord, rec_id)
            if existing:
                existing.rate = rate
                existing.fetched_at = now
            else:
                session.add(FxRateRecord(
                    id=rec_id, code=code, rate_date=day, rate=rate,
                    source="nationalbank.kz", fetched_at=now,
                ))
