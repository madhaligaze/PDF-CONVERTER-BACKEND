"""Выгрузка отчётности по МСФО в Google Sheets.

Одна таблица — несколько листов, по листу на логику признания плюс лист сверки.
**Структура строк на листах вариантов совпадает построчно** — это главное
требование: листы отличаются только цифрами, поэтому их можно сравнивать глазами
и переключаться между ними, не переучиваясь. Обеспечивается тем, что строки
собираются из одной спецификации `REPORT_SPEC`.

Формально корректен для МСФО первый лист: по IFRS 15 выручка по абонентскому
обслуживанию признаётся по мере оказания услуги, а не по подписанию акта.
Документарный лист даётся рядом — для сопоставления с бухгалтерией.

**Куда пишем.** Сервис-аккаунт не может создавать файлы: у него нет собственной
квоты в Google Drive (проверено — 403 «storage quota has been exceeded» и в
папке, и в корне). Поэтому выгрузка идёт в таблицу, которой владеете вы:
создайте пустую таблицу, дайте сервис-аккаунту доступ «Редактор» и укажите её id
в `BBC_MSFO_SPREADSHEET_ID`. Листы внутри перезаписываются при каждой выгрузке.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import gspread
from google.oauth2.service_account import Credentials

from app.bbc.config import bbc_settings
from app.bbc.dataset import ContractRow
from app.bbc.recognition import (
    V1_PERIOD_PRORATA_WIP,
    V2_PRORATA_WIP,
    by_month,
    wip_total,
)
from app.bbc.scope import DEPARTMENTS
from app.bbc.sheets import BbcError

log = logging.getLogger(__name__)

# Единственный скоуп с правом записи во всём модуле — и он живёт здесь, а не в
# общих кредах. Приложение читает источники read-only кредами из sheets.py;
# писать умеет только этот клиент и только в таблицу-приёмник (см. _open_target).
EXPORT_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_PERIODS = "МСФО (по периодам)"
SHEET_DOCUMENTS = "МСФО (по документам)"
SHEET_RECONCILIATION = "Сверка"


@dataclass(frozen=True)
class Line:
    """Строка отчёта: подпись, как её считать и как показать."""

    label: str
    # Ссылка на стандарт — чтобы отчёт читался как отчёт, а не как выгрузка.
    standard: str = ""
    kind: str = "value"  # value | header | total | spacer
    value_of: Callable[[dict[str, Any], str], float] | None = None


def _revenue_of(department: str) -> Callable[[dict[str, Any], str], float]:
    def pick(ctx: dict[str, Any], month: str) -> float:
        return ctx["by_department"].get(department, {}).get(month, 0.0)

    return pick


# Единая спецификация строк: оба варианта строятся из неё, поэтому расходятся
# только цифрами.
REPORT_SPEC: tuple[Line, ...] = (
    Line("ОТЧЁТ О ПРИБЫЛЯХ И УБЫТКАХ", "IAS 1", "header"),
    Line("Выручка по договорам с покупателями", "IFRS 15", "value",
         lambda ctx, month: ctx["revenue"].get(month, 0.0)),
    *[
        Line(f"    в том числе · {code}", "", "value", _revenue_of(code))
        for code in DEPARTMENTS
    ],
    Line("Выручка, не признанная (обязательства к исполнению)", "IFRS 15.116", "value",
         lambda ctx, month: ctx["wip"] if month == ctx["last_month"] else 0.0),
    Line("Итого признанная выручка", "", "total",
         lambda ctx, month: ctx["revenue"].get(month, 0.0)),
    Line("", "", "spacer"),

    Line("ОТЧЁТ О ДВИЖЕНИИ ДЕНЕЖНЫХ СРЕДСТВ", "IAS 7", "header"),
    Line("Поступления от покупателей", "IAS 7.14", "value",
         lambda ctx, month: ctx["cash_in"].get(month, 0.0)),
    Line("Итого денежный поток от операционной деятельности", "", "total",
         lambda ctx, month: ctx["cash_in"].get(month, 0.0)),
    Line("", "", "spacer"),

    Line("ОТЧЁТ О ФИНАНСОВОМ ПОЛОЖЕНИИ", "IAS 1", "header"),
    Line("Торговая дебиторская задолженность", "IFRS 15.105", "value",
         lambda ctx, month: ctx["receivable"].get(month, 0.0)),
    Line("Обязательства по договорам (авансы покупателей)", "IFRS 15.106", "value",
         lambda ctx, month: ctx["contract_liability"].get(month, 0.0)),
    Line("Чистая позиция по договорам", "", "total",
         lambda ctx, month: ctx["receivable"].get(month, 0.0)
         - ctx["contract_liability"].get(month, 0.0)),
)


def _context(rows: list[ContractRow], mode: str) -> dict[str, Any]:
    """Все цифры одного варианта, разложенные по месяцам."""
    monthly = by_month(rows, mode)
    months = list(monthly)

    by_department = {
        code: by_month([row for row in rows if code in row.departments], mode)
        for code in DEPARTMENTS
    }

    cash_in: dict[str, float] = {}
    receivable: dict[str, float] = {}
    liability: dict[str, float] = {}
    for row in rows:
        if row.month is None:
            continue
        key = f"2026-{row.month:02d}"
        cash_in[key] = cash_in.get(key, 0.0) + (row.paid_amount or 0.0)
        if row.avr_signed is True and row.paid is not True:
            receivable[key] = receivable.get(key, 0.0) + (row.avr_amount or row.contract_amount or 0.0)
        if row.paid is True and row.avr_signed is not True:
            liability[key] = liability.get(key, 0.0) + (row.paid_amount or 0.0)

    all_months = sorted({*months, *cash_in, *receivable, *liability})
    return {
        "months": all_months,
        "last_month": all_months[-1] if all_months else "",
        "revenue": monthly,
        "by_department": by_department,
        "wip": wip_total(rows, mode),
        "cash_in": cash_in,
        "receivable": receivable,
        "contract_liability": liability,
    }


def _month_title(key: str) -> str:
    names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    try:
        year, month = key.split("-")
        return f"{names[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return key


def all_months(rows: list[ContractRow]) -> list[str]:
    """Общий набор месяцев для всех листов.

    Без него у вариантов получились бы разные колонки: по документам в сентябре
    актов нет, и лист оказался бы на месяц короче — сравнивать построчно стало бы
    невозможно, а это главное требование к выгрузке.
    """
    months: set[str] = set()
    for mode in (V2_PRORATA_WIP, V1_PERIOD_PRORATA_WIP):
        months.update(_context(rows, mode)["months"])
    return sorted(months)


def build_sheet(
    rows: list[ContractRow],
    mode: str,
    title: str,
    note: str,
    months: list[str] | None = None,
) -> list[list[Any]]:
    """Сетка одного листа. Порядок строк и набор колонок одинаковы для всех вариантов."""
    ctx = _context(rows, mode)
    months = months if months is not None else all_months(rows)

    grid: list[list[Any]] = [
        [title],
        [note],
        [f"Сформировано: {datetime.now(UTC).strftime('%d.%m.%Y %H:%M UTC')}"],
        [],
        ["Статья", "Стандарт", *[_month_title(month) for month in months], "Итого"],
    ]

    for line in REPORT_SPEC:
        if line.kind == "spacer":
            grid.append([])
            continue
        if line.kind == "header":
            grid.append([line.label, line.standard, *[""] * (len(months) + 1)])
            continue

        values = [round(line.value_of(ctx, month), 2) if line.value_of else 0.0 for month in months]
        grid.append([line.label, line.standard, *values, round(sum(values), 2)])

    return grid


def build_reconciliation(rows: list[ContractRow]) -> list[list[Any]]:
    """Построчная разница между вариантами — та самая «дельта» из плана."""
    periods = _context(rows, V2_PRORATA_WIP)
    documents = _context(rows, V1_PERIOD_PRORATA_WIP)
    months = all_months(rows)

    grid: list[list[Any]] = [
        ["СВЕРКА ВАРИАНТОВ ПРИЗНАНИЯ"],
        ["Заработано по периодам услуг против закрытого документами (АВР)"],
        [],
        ["Показатель", *[_month_title(month) for month in months], "Итого"],
    ]

    earned = [round(periods["revenue"].get(month, 0.0), 2) for month in months]
    closed = [round(documents["revenue"].get(month, 0.0), 2) for month in months]
    gap = [round(a - b, 2) for a, b in zip(earned, closed)]
    share = [round(b / a, 4) if a else 0.0 for a, b in zip(earned, closed)]

    grid.append(["Заработано (по периодам)", *earned, round(sum(earned), 2)])
    grid.append(["Закрыто документами (АВР)", *closed, round(sum(closed), 2)])
    grid.append(["Разрыв", *gap, round(sum(gap), 2)])
    grid.append(["Доля закрытого", *share, ""])
    grid.append([])
    grid.append([
        "Разрыв — это выручка, которая заработана по факту оказания услуги, "
        "но ещё не подтверждена подписанным актом."
    ])
    return grid


def export(rows: list[ContractRow]) -> dict[str, Any]:
    """Записать три листа в настроенную таблицу и вернуть ссылку на неё."""
    target = (bbc_settings.msfo_spreadsheet_id or "").strip()
    if not target:
        raise BbcError(
            "Не задана таблица для выгрузки МСФО. Создайте пустую Google-таблицу, "
            f"дайте доступ «Редактор» сервис-аккаунту {_service_account_hint()} "
            "и укажите её id в BBC_MSFO_SPREADSHEET_ID. "
            "Своей таблицы сервис-аккаунт создать не может: у него нет квоты в Google Drive."
        )

    spreadsheet = _open_target(target)
    months = all_months(rows)  # общие колонки для обоих вариантов
    sheets_payload = [
        (SHEET_PERIODS, build_sheet(
            rows, V2_PRORATA_WIP,
            "ОТЧЁТНОСТЬ ПО МСФО — признание по периодам оказания услуг",
            "Основной вариант: по IFRS 15 выручка по абонентскому обслуживанию признаётся "
            "по мере оказания услуги, а не по подписанию акта.",
            months,
        )),
        (SHEET_DOCUMENTS, build_sheet(
            rows, V1_PERIOD_PRORATA_WIP,
            "ОТЧЁТНОСТЬ ПО МСФО — признание по подписанным актам",
            "Документарный вариант для сопоставления с бухгалтерским учётом. "
            "Структура строк совпадает с основным листом.",
            months,
        )),
        (SHEET_RECONCILIATION, build_reconciliation(rows)),
    ]

    written: list[str] = []
    for title, grid in sheets_payload:
        worksheet = _ensure_worksheet(spreadsheet, title, len(grid) + 5, max(len(r) for r in grid) + 2)
        worksheet.clear()
        worksheet.update(values=grid, range_name="A1")
        written.append(title)

    log.info("BBC: МСФО exported to %s (%s)", target, ", ".join(written))
    return {
        "spreadsheet_id": target,
        "url": f"https://docs.google.com/spreadsheets/d/{target}/edit",
        "sheets": written,
        "rows": len(rows),
        "exported_at": datetime.now(UTC).isoformat(),
    }


def _open_target(spreadsheet_id: str):
    """Открыть таблицу-приёмник кредами с правом записи.

    Отказывает для любого id, кроме настроенного в `BBC_MSFO_SPREADSHEET_ID`.
    Это страховка, а не формальность: исходная сводка целиком генерируется одной
    формулой-массивом, и запись в неё схлопывает лист. Даже при ошибке в вызывающем
    коде писать больше некуда.
    """
    configured = (bbc_settings.msfo_spreadsheet_id or "").strip()
    if spreadsheet_id != configured:
        raise BbcError("Запись разрешена только в таблицу из BBC_MSFO_SPREADSHEET_ID")

    raw = (bbc_settings.service_account_json or "").strip()
    try:
        if raw.startswith("{"):
            import json

            creds = Credentials.from_service_account_info(json.loads(raw), scopes=EXPORT_SCOPES)
        else:
            path = bbc_settings.credentials_path
            if path is None or not path.is_file():
                raise BbcError(f"BBC: файл кредов не найден: {path or raw}")
            creds = Credentials.from_service_account_file(str(path), scopes=EXPORT_SCOPES)
        return gspread.authorize(creds).open_by_key(spreadsheet_id)
    except BbcError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BbcError(
            f"Не удалось открыть таблицу для выгрузки: {exc}. "
            f"Проверьте, что сервис-аккаунту {_service_account_hint()} выдан доступ «Редактор»."
        ) from exc


def _ensure_worksheet(spreadsheet, title: str, rows: int, cols: int):
    """Взять лист по имени или создать. Существующий переиспользуется, чтобы
    ссылки на конкретные вкладки не ломались между выгрузками."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:  # noqa: BLE001 — gspread бросает своё исключение на отсутствие листа
        return spreadsheet.add_worksheet(title=title, rows=max(rows, 50), cols=max(cols, 12))


def _service_account_hint() -> str:
    raw = (bbc_settings.service_account_json or "").strip()
    if raw.startswith("{"):
        import json

        try:
            return json.loads(raw).get("client_email", "сервис-аккаунта")
        except ValueError:
            return "сервис-аккаунта"
    path = bbc_settings.credentials_path
    if path and path.is_file():
        import json

        try:
            return json.loads(path.read_text(encoding="utf-8")).get("client_email", "сервис-аккаунта")
        except (ValueError, OSError):
            return "сервис-аккаунта"
    return "сервис-аккаунта"


__all__ = [
    "EXPORT_SCOPES",
    "REPORT_SPEC",
    "all_months",
    "SHEET_DOCUMENTS",
    "SHEET_PERIODS",
    "SHEET_RECONCILIATION",
    "build_reconciliation",
    "build_sheet",
    "export",
]
