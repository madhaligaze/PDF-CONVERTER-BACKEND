"""AutoCall.kz → Google Sheets endpoints.

Powers the "Сервисы → Autocall.kz" modal in the frontend:
  GET  /autocall/metrics  — read-only summary for the modal
  POST /autocall/sync     — pull new campaigns and append them to the sheet
  GET  /autocall/status   — last sync record
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.services import autocall_payments_service, autocall_service
from app.services.autocall_service import AutocallError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/autocall")


@router.get("/metrics")
async def metrics() -> dict:
    if not settings.autocall_configured:
        raise HTTPException(
            status_code=400,
            detail="AutoCall не настроен: задайте AUTOCALL_ENABLED и AUTOCALL_API_KEY",
        )
    try:
        return await autocall_service.get_metrics()
    except AutocallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sync")
async def sync() -> dict:
    if not settings.autocall_configured:
        raise HTTPException(
            status_code=400,
            detail="AutoCall не настроен: задайте AUTOCALL_ENABLED и AUTOCALL_API_KEY",
        )
    if not settings.google_sheets_configured:
        raise HTTPException(
            status_code=400,
            detail="Google Sheets не настроен: задайте GOOGLE_SHEETS_SPREADSHEET_ID и "
            "GOOGLE_SERVICE_ACCOUNT_JSON",
        )
    try:
        return await autocall_service.sync_autocalls()
    except AutocallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Balance top-ups ("Пополнения") ───────────────────────────────────────────────
# Defined as sync `def` so Starlette runs the blocking httpx/gspread calls in a
# threadpool instead of blocking the event loop.

@router.get("/topups/metrics")
def topups_metrics() -> dict:
    if not settings.autocall_session_configured:
        raise HTTPException(
            status_code=400,
            detail="Не заданы логин/пароль кабинета: AUTOCALL_LOGIN и AUTOCALL_PASSWORD",
        )
    try:
        return autocall_payments_service.get_metrics()
    except AutocallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/topups/sync")
def topups_sync() -> dict:
    if not settings.autocall_session_configured:
        raise HTTPException(
            status_code=400,
            detail="Не заданы логин/пароль кабинета: AUTOCALL_LOGIN и AUTOCALL_PASSWORD",
        )
    if not settings.google_sheets_configured:
        raise HTTPException(
            status_code=400,
            detail="Google Sheets не настроен: задайте GOOGLE_SHEETS_SPREADSHEET_ID и "
            "GOOGLE_SERVICE_ACCOUNT_JSON",
        )
    try:
        return autocall_payments_service.sync_topups()
    except AutocallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/status")
async def status() -> dict:
    last = autocall_service.get_last_sync()
    return {
        "configured": settings.autocall_configured,
        "sheets_configured": settings.google_sheets_configured,
        "auto_sync_enabled": settings.autocall_auto_sync_enabled,
        "cutoff_date": settings.autocall_sync_since,
        "last_sync": last,
    }
