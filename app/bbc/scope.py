"""Visibility scope — which rows a given credential is allowed to see.

This is the security core of the module. Two rules it must never break:

1. **Default deny.** A missing or empty scope yields *nothing*, never everything.
   Forgetting to pass a scope must produce an empty screen, not a data leak.
2. **Aggregates are scoped too.** Filtering rows but summing the full set leaks
   other departments' figures through the totals — the classic dashboard leak.
   Callers must aggregate over `filter_rows(...)` output, never the raw list.

Why departments and not legal entities: the two are orthogonal in the source
sheet. ОБО spans BBC/BBCA/BBCS/BBC Astana, and the BBC entity is split across
ОБО/ЮО/HR/НО — so filtering by «Наша Фирма» does not isolate a department.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Canonical department codes as they appear in «Отдел» of the master sheet.
DEPARTMENTS: tuple[str, ...] = ("ОБО", "НО", "ЮО", "HR", "ФО")

# Blocks a credential may reach. Referral links get the first three.
BLOCKS: tuple[str, ...] = (
    "receivables",
    "analytics",
    "calendar",
    "reports",
    "journal",
    "sales",
    "warnings",
    "roadmap",
)
LINK_BLOCKS: tuple[str, ...] = ("receivables", "analytics", "calendar")

WILDCARD = "*"

# «ОБО, НО,\n ЮО, HR» — one row can belong to several departments.
_SPLIT = re.compile(r"[,;/]|\s{2,}|\n")

# Tolerate the spelling drift that exists in the sheet.
_ALIASES: dict[str, str] = {
    "ОБО": "ОБО",
    "БО": "ОБО",
    "НО": "НО",
    "ЮО": "ЮО",
    "ФО": "ФО",
    "HR": "HR",
    "НR": "HR",  # Cyrillic Н + Latin R
    "КАДРЫ": "HR",
}


def canonical_department(raw: str) -> str | None:
    """Normalise one department token, or None when it is not recognised."""
    token = re.sub(r"\s+", "", (raw or "")).upper()
    return _ALIASES.get(token)


def parse_departments(raw: str | None) -> tuple[str, ...]:
    """Parse the «Отдел» cell into a tuple of canonical departments.

    Returns an empty tuple for blank or unrecognised values — those rows are
    "нераспределённые" and, per the access rules, visible to admins only.
    """
    if not raw:
        return ()
    seen: list[str] = []
    for chunk in _SPLIT.split(raw):
        code = canonical_department(chunk)
        if code and code not in seen:
            seen.append(code)
    return tuple(seen)


@dataclass(frozen=True)
class Scope:
    """What a credential may see. Construct via the classmethods, not by hand."""

    departments: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    label: str = ""

    @classmethod
    def admin(cls) -> "Scope":
        return cls(departments=(WILDCARD,), blocks=(WILDCARD,), label="admin")

    @classmethod
    def for_departments(
        cls,
        departments: Iterable[str],
        blocks: Iterable[str] = LINK_BLOCKS,
        label: str = "",
    ) -> "Scope":
        codes = tuple(
            code for code in (canonical_department(d) for d in departments) if code
        )
        return cls(departments=codes, blocks=tuple(blocks), label=label or ",".join(codes))

    @classmethod
    def denied(cls) -> "Scope":
        """The default. Sees nothing — used whenever a credential is absent."""
        return cls(departments=(), blocks=(), label="denied")

    @property
    def is_admin(self) -> bool:
        return WILDCARD in self.departments

    @property
    def sees_nothing(self) -> bool:
        return not self.departments

    def allows_block(self, block: str) -> bool:
        return WILDCARD in self.blocks or block in self.blocks

    def to_dict(self) -> dict[str, Any]:
        return {
            "departments": list(self.departments),
            "blocks": list(self.blocks),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Scope":
        """Rebuild a stored scope. Anything malformed degrades to `denied()`."""
        if not isinstance(data, dict):
            return cls.denied()
        departments = data.get("departments") or []
        blocks = data.get("blocks") or []
        if not isinstance(departments, list) or not isinstance(blocks, list):
            return cls.denied()
        if WILDCARD in departments:
            return cls(
                departments=(WILDCARD,),
                blocks=(WILDCARD,) if WILDCARD in blocks else tuple(blocks),
                label=str(data.get("label") or "admin"),
            )
        return cls.for_departments(departments, blocks, str(data.get("label") or ""))


def row_visible(row_departments: Sequence[str], scope: Scope) -> bool:
    """Whether a row belongs to the scope.

    * admin sees everything, including rows with no department;
    * a department scope sees rows that list at least one of its departments —
      so the three «ОБО, НО, ЮО, HR» rows are visible to each of the four;
    * rows with no department are admin-only.
    """
    if scope.is_admin:
        return True
    if not scope.departments or not row_departments:
        return False
    return any(code in scope.departments for code in row_departments)


def filter_rows(
    rows: Iterable[Any],
    scope: Scope,
    *,
    attr: str = "departments",
) -> list[Any]:
    """Keep only the rows the scope may see.

    Works with objects (attribute) and dicts (key). Aggregate over the result of
    this call — never over the input — or the totals will leak.
    """
    visible: list[Any] = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get(attr)
        else:
            value = getattr(row, attr, None)
        if isinstance(value, str):
            value = parse_departments(value)
        if row_visible(tuple(value or ()), scope):
            visible.append(row)
    return visible


__all__ = [
    "BLOCKS",
    "DEPARTMENTS",
    "LINK_BLOCKS",
    "Scope",
    "canonical_department",
    "filter_rows",
    "parse_departments",
    "row_visible",
]
