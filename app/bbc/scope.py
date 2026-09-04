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

# Blocks a credential may reach. Referral links get the first four.
BLOCKS: tuple[str, ...] = (
    "receivables",
    "touches",
    "analytics",
    "calendar",
    "reports",
    "journal",
    "sales",
    "warnings",
    "roadmap",
    # Реестры — приложенческий ввод во внутренние книги. Отдельное право, а не
    # часть «журнала»: там читают то, что уже записано, здесь записывают.
    "registries",
)
# Журнал касаний входит: начальник отдела обязан видеть, кто и до кого по его
# долгам уже достучался. Писать он оттуда не сможет — запись требует учётки, а
# у ссылки нет автора, и подписать касание было бы нечем.
LINK_BLOCKS: tuple[str, ...] = ("receivables", "touches", "analytics", "calendar")

WILDCARD = "*"

# Область данных — «чьи строки видно», отдельно от прав «что можно открыть».
#   all        — весь дашборд (админ);
#   department — всё, что относится к отделам области видимости;
#   own        — только строки, где «Сотрудник» — это ты.
# Ось отдельная сознательно: право открыть дебиторку и право видеть в ней чужих
# клиентов — разные вопросы, и склеивать их в одну «роль» значит потерять один
# из них при первой же правке.
DATA_SCOPES: tuple[str, ...] = ("own", "department", "all")

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


def normalize_employee(raw: str | None) -> str:
    """Ключ сравнения имён из колонки «Сотрудник».

    Регистр и лишние пробелы сняты. Больше ничего: расшифровывать «Жумабекова Д.»
    в «Дана» алгоритмически нельзя, поэтому владение задаётся списком написаний,
    который заводит админ, — а здесь только приводятся к общему виду.
    """
    return re.sub(r"\s+", " ", (raw or "")).strip().casefold()


@dataclass(frozen=True)
class Scope:
    """What a credential may see. Construct via the classmethods, not by hand."""

    departments: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    label: str = ""
    #: Одно из DATA_SCOPES. По умолчанию `all`: у ссылок отдела и у админа
    #: сужения по сотруднику нет, оно появляется только у учётки сотрудника.
    data_scope: str = "all"
    #: Нормализованные написания в колонке «Сотрудник». Пусто, кроме `own`.
    employee_aliases: tuple[str, ...] = ()
    #: Кто спрашивает — нужно журналу касаний, чтобы отличить свою запись от
    #: чужой. У ссылки отдела автора нет, поэтому None.
    user_id: int | None = None

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
    def for_employee(
        cls,
        *,
        user_id: int,
        departments: Iterable[str],
        blocks: Iterable[str],
        data_scope: str,
        employee_aliases: Iterable[str] = (),
        label: str = "",
    ) -> "Scope":
        """Область видимости учётки сотрудника.

        Неизвестное значение `data_scope` схлопывается в `own` — самый узкий
        вариант. Порча этой колонки должна отнимать доступ, а не раздавать.
        """
        codes = tuple(code for code in (canonical_department(d) for d in departments) if code)
        scope_kind = data_scope if data_scope in DATA_SCOPES else "own"

        # `own` без единого написания — это не «видит всё», а «видит пусто».
        aliases = tuple(
            key for key in (normalize_employee(name) for name in employee_aliases) if key
        )
        return cls(
            departments=codes,
            blocks=tuple(blocks),
            label=label,
            data_scope=scope_kind,
            employee_aliases=aliases,
            user_id=user_id,
        )

    @classmethod
    def denied(cls) -> "Scope":
        """The default. Sees nothing — used whenever a credential is absent."""
        return cls(departments=(), blocks=(), label="denied", data_scope="own")

    @property
    def is_admin(self) -> bool:
        return WILDCARD in self.departments

    @property
    def sees_nothing(self) -> bool:
        return not self.departments

    def allows_block(self, block: str) -> bool:
        return WILDCARD in self.blocks or block in self.blocks

    def owns_employee(self, employee: str | None) -> bool:
        """Принадлежит ли строка этому сотруднику. Вне `own` — всегда да."""
        if self.data_scope != "own":
            return True
        return normalize_employee(employee) in self.employee_aliases

    def to_dict(self) -> dict[str, Any]:
        return {
            "departments": list(self.departments),
            "blocks": list(self.blocks),
            "label": self.label,
            "data_scope": self.data_scope,
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


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def filter_rows(
    rows: Iterable[Any],
    scope: Scope,
    *,
    attr: str = "departments",
) -> list[Any]:
    """Keep only the rows the scope may see.

    Works with objects (attribute) and dicts (key). Aggregate over the result of
    this call — never over the input — or the totals will leak.

    Две ступени: сначала отдел, потом — при `data_scope == "own"` — сотрудник.
    Порядок неважен для результата, но важен для чтения: сужение по сотруднику
    только доужимает то, что уже разрешено отделом, и никогда не расширяет.
    """
    visible: list[Any] = []
    own_only = scope.data_scope == "own" and not scope.is_admin
    for row in rows:
        value = _field(row, attr)
        if isinstance(value, str):
            value = parse_departments(value)
        if not row_visible(tuple(value or ()), scope):
            continue
        if own_only and not scope.owns_employee(_field(row, "employee")):
            continue
        visible.append(row)
    return visible


__all__ = [
    "BLOCKS",
    "DATA_SCOPES",
    "DEPARTMENTS",
    "LINK_BLOCKS",
    "Scope",
    "canonical_department",
    "filter_rows",
    "normalize_employee",
    "parse_departments",
    "row_visible",
]
