"""Сверка долга, посчитанного дашбордом, с тем, что видит начальник отдела.

Отделы живут по вкладкам «НО (для Рук)», «ОБО (для Рук)» и так далее: там
колонка «Сумма Конец» — их рабочая цифра. Дашборд считает долг сам, по колонке
«Дебет / Кредит (в т.ч без АВР)» листа «Сводка все ЮР лица» плюс входящий
остаток строки «Старые…». Если эти две цифры разойдутся, спорить будут не с
таблицей, а с дашбордом — поэтому расхождение нужно видеть раньше пользователя.

Это инструмент, а не тест: книга живая, и расхождение завтра — сигнал
разобраться, а не повод уронить сборку.

    python -m scripts.reconcile_receivables            # все отделы
    python -m scripts.reconcile_receivables ОБО НО     # выбранные

Сравнение идёт по клиенту, а не по строке: вкладка «для Рук» ведёт строку на
договор, и у клиента с двумя договорами там две строки против одной суммы у нас.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

from app.bbc import sheets
from app.bbc.dataset import ContractRow, parse_contract_rows
from app.bbc.normalize import clean, parse_money

DEPARTMENTS = ("ОБО", "НО", "ЮО", "HR")

#: Расхождение мельче тенге — это арифметика с плавающей точкой, а не проблема.
TOLERANCE = 1.0

_NOISE = re.compile(r"[«»\"'`()]|\bТОО\b|\bИП\b|\bАО\b|\bLTD\b|\bLLP\b", re.IGNORECASE)


def normalize_client(name: str) -> str:
    """Имена клиентов в двух вкладках пишут по-разному: «ТОО "Окстрой"» и «Окстрой»."""
    text = _NOISE.sub(" ", clean(name))
    # Латинская N в «Nº» и кириллическая в «№» на глаз неразличимы.
    text = text.replace("N", "Н").replace("º", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


def our_debt_by_client(rows: list[ContractRow], department: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if department not in row.departments:
            continue
        if not row.client:
            continue
        totals[normalize_client(row.client)] += row.total_debt
    return dict(totals)


def their_debt_by_client(department: str) -> dict[str, float]:
    """Читает «Сумма Конец» из вкладки отдела, суммируя строки одного клиента."""
    values = sheets.read_values(f"{department} (для Рук)")
    if not values:
        return {}

    header_at = next(
        (i for i, row in enumerate(values[:8]) if any("Клиент" in clean(c) for c in row)),
        None,
    )
    if header_at is None:
        raise RuntimeError(f"Во вкладке «{department} (для Рук)» не нашлась шапка")

    header = values[header_at]
    client_at = next(i for i, c in enumerate(header) if "Клиент" in clean(c))
    total_at = next(i for i, c in enumerate(header) if "Конец" in clean(c))

    totals: dict[str, float] = defaultdict(float)
    for row in values[header_at + 1 :]:
        if client_at >= len(row):
            continue
        name = clean(row[client_at])
        if not name:
            continue
        amount = parse_money(row[total_at]) if total_at < len(row) else None
        # В книге долг отдела записан со знаком минус — сравниваем модули.
        totals[normalize_client(name)] += abs(amount or 0.0)
    return dict(totals)


def report(department: str, rows: list[ContractRow]) -> int:
    """Печатает сверку отдела. Возвращает число настоящих расхождений.

    Вкладки «для Рук» — выборка, а не полный реестр: в ОБО их 57 строк против
    103 наших клиентов. Клиент, которого во вкладке нет, — не расхождение, и
    смешивать его с расхождением нельзя, иначе сверка перестанет что-либо
    значить. Считается только то, что есть в обоих источниках.
    """
    ours = our_debt_by_client(rows, department)
    theirs = their_debt_by_client(department)

    shared = sorted(set(ours) & set(theirs))
    mismatched = [
        (name, ours[name], theirs[name])
        for name in shared
        if abs(ours[name] - theirs[name]) > TOLERANCE
    ]
    only_ours = sorted(set(ours) - set(theirs))

    print(f"\n{'=' * 78}\n{department}: клиентов у нас {len(ours)}, в книге {len(theirs)}")
    print(f"  наш итог   {sum(ours.values()):>16,.0f} ₸")
    print(f"  книга      {sum(theirs.values()):>16,.0f} ₸")
    print(f"  общих клиентов: {len(shared)}, только у нас: {len(only_ours)}")

    if mismatched:
        print(f"\n  РАСХОДИТСЯ: {len(mismatched)}")
        print(f"  {'клиент':40}{'у нас':>15}{'в книге':>15}{'разница':>15}")
        for name, mine, ref in sorted(mismatched, key=lambda x: -abs(x[1] - x[2])):
            print(f"  {name[:40]:40}{mine:>15,.0f}{ref:>15,.0f}{mine - ref:>15,.0f}")
    else:
        print("  по общим клиентам сходится до тенге")

    if only_ours:
        shown = ", ".join(name[:24] for name in only_ours[:5])
        print(f"  нет во вкладке отдела: {len(only_ours)} ({shown}{', …' if len(only_ours) > 5 else ''})")
    return len(mismatched)


def main(argv: list[str]) -> int:
    wanted = [d for d in argv if d in DEPARTMENTS] or list(DEPARTMENTS)
    rows = parse_contract_rows(sheets.read_source("master"))
    print(f"Разобрано строк: {len(rows)}")
    print(f"Дебиторка всего: {sum(r.total_debt for r in rows):,.0f} ₸")

    mismatched = sum(report(department, rows) for department in wanted)
    print(f"\n{'=' * 78}")
    print(
        "Всё сходится."
        if not mismatched
        else f"Расхождений по общим клиентам: {mismatched} — разобраться до показа цифр."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
