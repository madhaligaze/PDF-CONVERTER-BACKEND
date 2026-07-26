"""Turning the spreadsheet's raw strings into typed values.

The source is hand-maintained, so every parser here is written against dirt that
actually exists in the sheets:

* numbers as text with NBSP/narrow-NBSP thousands separators — `50 000`, `190,00`;
* formula errors leaking into values — `#N/A`, `#VALUE!`, `#REF!`, `#DIV/0!`;
* two date formats — `21.05.2026` and `пн 18.05.26` (with U+202F after it);
* booleans that are not booleans — `TRUE` / `FALSE` / `Часть` / `Еще рано` / `-`;
* the same category spelled several ways — `Разовый` / `Разовая` / `Разовая услуга`.

Everything returns `None` rather than guessing, so a bad cell is visibly empty
instead of silently becoming zero.
"""
from __future__ import annotations

import re
from datetime import date

# Formula errors and placeholders that must never be read as data.
_ERRORS = frozenset(
    {"#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#ERROR!"}
)

# NBSP, narrow NBSP, thin space, zero-width space — all used as separators here.
_SPACES = "   ​"
_SPACE_RE = re.compile(f"[{_SPACES}\\s]+")

_DATE_DMY = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")


def clean(value: object) -> str:
    """Trim a cell and drop the invisible separators the sheet is full of."""
    if value is None:
        return ""
    text = str(value)
    for char in _SPACES:
        text = text.replace(char, " ")
    return text.strip()


def is_error(value: object) -> bool:
    return clean(value).upper() in _ERRORS


def parse_money(value: object) -> float | None:
    """Parse an amount. Returns None for blanks, formula errors and garbage.

    Handles `50 000` (NBSP), `1 200 000,00` (comma decimal), `190,00`, `-2 500`
    and plain `500000`.
    """
    text = clean(value)
    if not text or text.upper() in _ERRORS:
        return None

    text = _SPACE_RE.sub("", text)
    text = text.replace("₸", "").replace("KZT", "").replace("kzt", "")
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    if "," in text and "." in text:
        # Whichever separator comes last is the decimal one.
        text = (
            text.replace(".", "").replace(",", ".")
            if text.rfind(",") > text.rfind(".")
            else text.replace(",", "")
        )
    elif "," in text:
        text = text.replace(",", ".")

    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value: object) -> date | None:
    """Parse `21.05.2026` and `пн 18.05.26`. Two-digit years are 2000-based."""
    text = clean(value)
    if not text or text.upper() in _ERRORS:
        return None

    match = _DATE_DMY.search(text)
    if match is None:
        return None

    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


_TRUE = frozenset({"TRUE", "ДА", "YES", "1", "+", "V", "ИСТИНА"})
_FALSE = frozenset({"FALSE", "НЕТ", "NO", "0", "-", "ЛОЖЬ", ""})


def parse_bool(value: object) -> bool | None:
    """Parse a checkbox-ish cell.

    Returns None for the free text that also lives in these columns («Часть»,
    «Еще рано») — the caller decides what a partial state means.
    """
    text = clean(value).upper()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


# ── Canonical dimensions ─────────────────────────────────────────────────────────

# Legal entity codes as typed in «Наша Фирма» → readable names.
FIRM_LABELS: dict[str, str] = {
    "BBC": "Big Business Consulting",
    "BBCS": "SAKOMPA-M",
    "BBCL": "BBC Legal Support",
    "BBCA": "Big Business Consulting & Audit",
    "BBC HR": "BBC HR",
    "BBC ASTANA": "BBC Astana",
    "EA": "Elite Advisory",
}

_SERVICE_KINDS: dict[str, str] = {
    "АБОН.П.": "Абонентская плата",
    "АБОНЕНТСКОЕ ОБСЛУЖИВАНИЕ": "Абонентская плата",
    "РАЗОВЫЙ": "Разовая услуга",
    "РАЗОВАЯ": "Разовая услуга",
    "РАЗОВАЯ УСЛУГА": "Разовая услуга",
    "АРЕНДА": "Аренда",
    "ПЕРИОДИЧЕСКИ": "Периодическая",
    "ПО КВАРТАЛЬНЫЙ ???": "Периодическая",
    "ИНОЕ": "Иное",
}

_STATUSES: dict[str, str] = {
    "ПРОДЛЕНИЕ": "Продление",
    "ДЕЙСТВ.": "Действующий",
    "НА ИСПОЛНЕНИИ": "На исполнении",
    "НА ИСП.": "На исполнении",
    "РАЗОВЫЙ": "Разовый",
    "НЕ ИЗВЕСТНО": "Неизвестно",
    "НЕ СОСТОЯЛОСЬ": "Не состоялось",
}

DEPARTMENT_LABELS: dict[str, str] = {
    "ОБО": "Бухгалтерский отдел",
    "НО": "Налоговый отдел",
    "ЮО": "Юридический отдел",
    "HR": "Кадровый отдел",
    "ФО": "Финансовый отдел",
}


def canonical_firm(value: object) -> str:
    """Uppercase entity code, unchanged if unknown (never silently dropped)."""
    return _SPACE_RE.sub(" ", clean(value)).upper()


def firm_label(code: str) -> str:
    return FIRM_LABELS.get(canonical_firm(code), code)


def canonical_service_kind(value: object) -> str:
    """Collapse «Разовый»/«Разовая»/«Разовая услуга» into one label."""
    text = _SPACE_RE.sub(" ", clean(value)).upper()
    if not text:
        return ""
    return _SERVICE_KINDS.get(text, clean(value))


def canonical_status(value: object) -> str:
    """Collapse the status spellings; free-text notes are kept verbatim."""
    text = _SPACE_RE.sub(" ", clean(value)).upper()
    if not text:
        return ""
    if text in _STATUSES:
        return _STATUSES[text]
    # «Приостоновление в Июне» / «Приостоновили в Июне» → one bucket.
    if text.startswith("ПРИОСТ"):
        return "Приостановлен"
    if text.startswith("РАСТОРЖ"):
        return "Расторжение"
    return clean(value)


def department_label(code: str) -> str:
    return DEPARTMENT_LABELS.get(clean(code).upper(), code)


__all__ = [
    "DEPARTMENT_LABELS",
    "FIRM_LABELS",
    "canonical_firm",
    "canonical_service_kind",
    "canonical_status",
    "clean",
    "department_label",
    "firm_label",
    "is_error",
    "parse_bool",
    "parse_date",
    "parse_money",
]
