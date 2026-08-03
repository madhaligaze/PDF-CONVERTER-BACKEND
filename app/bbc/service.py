"""BBC Dashboard business logic.

Every function that returns data takes a `Scope` and applies it **before** doing
anything else. That ordering is the point: rows are filtered first, then
aggregated, so a total can never carry another department's money. There is no
"unscoped" variant of these calls — the only way to see everything is to hold an
admin scope.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.bbc import calendar as calendar_module
from app.bbc import journal as journal_module
from app.bbc import live
from app.bbc import msfo as msfo_module
from app.bbc import sales as sales_module
from app.bbc import sheets
from app.bbc import touches as touches_module
from app.bbc.config import bbc_settings
from app.bbc.dataset import ContractRow, collect_dimensions, coverage
from app.bbc.recognition import DEFAULT_MODE, MODES, PRESETS, describe_mode
from app.bbc.schemas import BbcSheetInfo, BbcSnapshot, BbcStatus
from app.bbc.scope import Scope, filter_rows
from app.bbc.sheets import BbcError
from app.bbc.validators import analyze, summarize

log = logging.getLogger(__name__)


def get_status() -> BbcStatus:
    detail: str | None = None
    if not bbc_settings.enabled:
        detail = "BBC Dashboard выключен (BBC_DASHBOARD_ENABLED=false)"
    elif not bbc_settings.spreadsheet_id:
        detail = "Не задан BBC_SPREADSHEET_ID"
    elif not bbc_settings.credentials_available:
        detail = "Не найден файл кредов Google (BBC_SERVICE_ACCOUNT_JSON)"
    return BbcStatus(
        enabled=bbc_settings.enabled,
        configured=bbc_settings.configured,
        spreadsheet_id=bbc_settings.spreadsheet_id,
        worksheet_name=bbc_settings.worksheet_name,
        credentials_available=bbc_settings.credentials_available,
        detail=detail,
    )


def list_sheets() -> list[BbcSheetInfo]:
    return [BbcSheetInfo(**item) for item in sheets.list_worksheets()]


def get_snapshot(worksheet: str | None = None, *, refresh: bool = False) -> BbcSnapshot:
    """Raw grid of one worksheet — admin-only, kept for the original contract."""
    values = sheets.read_values(worksheet)
    headers = [str(cell).strip() for cell in values[0]] if values else []
    rows = values[1:] if len(values) > 1 else []
    return BbcSnapshot(
        worksheet=worksheet or bbc_settings.worksheet_name or "(первый лист)",
        headers=headers,
        rows=rows,
        row_count=len(rows),
        fetched_at=datetime.now(UTC).isoformat(),
        metrics={"columns": len(headers), "rows": len(rows)},
    )


# ── Scoped data ──────────────────────────────────────────────────────────────────


def _visible_rows(scope: Scope) -> list[ContractRow]:
    """The only entry point to row data. Empty scope → empty list, by design."""
    if scope.sees_nothing:
        return []
    return filter_rows(live.ensure_loaded().rows, scope)


def visible_client_keys(scope: Scope) -> set[str] | None:
    """Клиенты, доступные вызывающему. None у админа — ему доступны все.

    Это определение «своего клиента» для журнала касаний: если строка клиента
    доехала до пользователя, то и касания по нему доехать должны. Второй,
    независимой проверки прав в журнале нет — она бы неизбежно разошлась с этой.
    """
    if scope.is_admin:
        return None
    return {
        key
        for key in (touches_module.client_key(row.client) for row in _visible_rows(scope))
        if key
    }


def employee_names() -> list[dict[str, Any]]:
    """Написания из колонки «Сотрудник» — для привязки учётки к её клиентам.

    Не просто список имён, а имя вместе с его отделами и объёмом: «Жанара —
    ОБО, 18 клиентов». Без этого админ собирает учётку вслепую и может отметить
    человека из НО, выдав при этом отдел ОБО, — пересечение пусто, аккаунт
    выглядит рабочим и не показывает ни одной строки. Сказать об этом надо в тот
    момент, когда галочка ставится, а не когда сотрудник увидит пустой экран.

    Читается из полного снимка, а не из области видимости: список нужен админу,
    и урезать его там нечем.
    """
    seen: dict[str, dict[str, Any]] = {}
    for row in live.ensure_loaded().rows:
        name = (row.employee or "").strip()
        if not name:
            continue
        entry = seen.setdefault(
            name.casefold(),
            {"name": name, "departments": set(), "clients": set(), "rows": 0},
        )
        entry["rows"] += 1
        entry["departments"].update(row.departments)
        if row.client:
            entry["clients"].add(row.client)

    return sorted(
        (
            {
                "name": entry["name"],
                "departments": sorted(entry["departments"]),
                "clients": len(entry["clients"]),
                "rows": entry["rows"],
            }
            for entry in seen.values()
        ),
        key=lambda item: (-item["clients"], item["name"].casefold()),
    )


def get_dataset(scope: Scope, *, refresh: bool = False) -> dict[str, Any]:
    """Everything the frontend needs for one load, already narrowed to the scope.

    Dimensions, coverage and warnings are recomputed on the *visible* rows —
    otherwise a filter list or a total would betray rows the caller may not see.
    """
    if refresh:
        live.refresh(force=True)

    snapshot = live.ensure_loaded()
    rows = _visible_rows(scope)
    findings = analyze(rows)

    return {
        "revision": snapshot.revision,
        "changed_at": snapshot.changed_at,
        "scope": scope.to_dict(),
        "modes": list(MODES),
        "default_mode": DEFAULT_MODE,
        "presets": PRESETS,
        "mode_descriptions": {mode: describe_mode(mode) for mode in MODES},
        "rows": [row.to_dict() for row in rows],
        "dimensions": collect_dimensions(rows),
        "coverage": coverage(rows),
        "warnings": [finding.to_dict() for finding in findings],
        "warnings_summary": summarize(findings),
        "sources": live.revision_payload()["sources"],
    }


def get_calendar(scope: Scope, method: str = calendar_module.PREDICTIVE) -> dict[str, Any]:
    """Платёжный календарь в границах области видимости."""
    rows = _visible_rows(scope)
    payload = calendar_module.build_calendar(rows, method).to_dict()
    payload["methods"] = [
        {
            "key": key,
            "title": calendar_module.METHOD_TITLES[key],
            "hint": calendar_module.METHOD_HINTS[key],
        }
        for key in calendar_module.METHODS
    ]
    return payload


def get_sales(scope: Scope, worksheet: str | None = None) -> dict[str, Any]:
    """Блок «Отдел продаж»: план/факт, ФОТ с бонусами, отдача на канал.

    Данные лежат в отдельной таблице ОМиП и не разрезаются по отделам сводной,
    поэтому блок доступен только тем, чья область его разрешает (админ).
    """
    tab = worksheet or _latest_report_tab()
    report_grid = sheets.read_cached(tab, bbc_settings.omip_spreadsheet_id)
    report = sales_module.parse_sales_report(report_grid, tab)

    try:
        registry = sales_module.parse_sales_registry(
            sheets.read_source_cached(sheets.SOURCE_SALES)
        )
    except BbcError as exc:  # реестр может быть недоступен — блок всё равно полезен
        log.warning("BBC: sales registry unavailable: %s", exc)
        registry = []

    report.channels = sales_module.channel_results(report, registry)
    payload = report.to_dict()
    payload["registry"] = registry
    payload["tabs"] = _report_tabs()
    return payload


def _report_tabs() -> list[str]:
    """Листы «Отчет …» в таблице ОМиП, свежие первыми."""
    titles = [
        item["title"]
        for item in sheets.list_worksheets_cached(bbc_settings.omip_spreadsheet_id)
        if item["title"].lower().startswith("отчет")
    ]
    return titles


def _latest_report_tab() -> str:
    tabs = _report_tabs()
    return tabs[-1] if tabs else "Отчет Июль"


def get_journal(scope: Scope, group: str = "counterparty", measure: str = "outflow") -> dict[str, Any]:
    """Журнал операций плюс мини-свод по выбранному измерению.

    Строки журнала несут фирму, но не отдел, поэтому по отделам они не режутся —
    блок доступен только тем, чья область его разрешает (админ).
    """
    rows = journal_module.parse_journal(sheets.read_source_cached(sheets.SOURCE_JOURNAL))
    return {
        "rows": [row.to_dict() for row in rows],
        "coverage": journal_module.coverage(rows),
        "dimensions": journal_module.dimensions(rows),
        "summary": journal_module.summarize(rows, group, measure),
        "groups": [
            {"key": key, "title": title}
            for key, (title, _) in journal_module.GROUPS.items()
        ],
        "measures": [
            {"key": key, "title": title}
            for key, (title, _) in journal_module.MEASURES.items()
        ],
    }


def export_msfo(scope: Scope) -> dict[str, Any]:
    """Выгрузить отчётность по МСФО в настроенную Google-таблицу."""
    return msfo_module.export(_visible_rows(scope))


def get_warnings(scope: Scope) -> dict[str, Any]:
    """The «Предупреждения» block alone, so it can be polled cheaply."""
    findings = analyze(_visible_rows(scope))
    return {
        "warnings": [finding.to_dict() for finding in findings],
        "summary": summarize(findings),
    }


def get_revision() -> dict[str, Any]:
    """Served from memory — this is what browsers poll every few seconds."""
    return live.revision_payload()


def invalidate_cache() -> None:
    """Кнопка «Обновить»: человек просит свежее — отдаём свежее.

    Сбрасывается и кэш второстепенных источников: иначе «Обновить» перечитывало
    бы мастер-таблицу, а журнал и продажи молча оставались бы прежними.
    """
    sheets.invalidate_read_cache()
    live.refresh(force=True)


__all__ = [
    "BbcError",
    "get_calendar",
    "get_dataset",
    "get_journal",
    "get_revision",
    "get_sales",
    "get_snapshot",
    "get_status",
    "export_msfo",
    "get_warnings",
    "invalidate_cache",
    "list_sheets",
]
