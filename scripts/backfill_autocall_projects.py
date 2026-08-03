"""Дозаполнение колонки «Проект» на листе «Фактическая стоимость».

Имя обзвона в кабинете набирают на той раскладке, которая была включена: «18.06 Д2»
и «02.08 D2» — это один и тот же проект. Латинские B и D неотличимы от Б и Д на
экране, но регулярка ловила только кириллицу, поэтому у 155 из 176 записанных
строк колонка «Проект» осталась пустой.

Сама выгрузка уже исправлена — этот скрипт чинит то, что записано раньше.
Буква восстанавливается сопоставлением с API по паре (дата, сумма): именно эти
два поля строка и сохранила.

    python -m scripts.backfill_autocall_projects          # показать, ничего не менять
    python -m scripts.backfill_autocall_projects --apply  # записать в таблицу

Без --apply скрипт только печатает план: колонка живая, и перезаписывать её
вслепую нельзя.
"""
from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.services.autocall_service import (
    _extract_project_letter,
    _fetch_autocalls,
    _format_date,
    _open_worksheet,
    _parse_cost,
)


def _amount_key(date_str: str, amount: float | None) -> tuple[str, str]:
    """Строка таблицы хранит только дату и сумму — по ним и ищем обзвон."""
    return date_str, f"{amount:.2f}" if amount is not None else ""


async def build_letter_map() -> dict[tuple[str, str], set[str]]:
    """(дата, сумма) → буквы проектов из API. Множество, чтобы увидеть неоднозначность."""
    autocalls, _total = await _fetch_autocalls(stop_at_cutoff=True)
    mapping: dict[tuple[str, str], set[str]] = {}
    for autocall in autocalls:
        letter = _extract_project_letter(autocall.get("name"))
        if not letter:
            continue
        key = _amount_key(
            _format_date(autocall.get("created_at")),
            _parse_cost(autocall.get("final_cost")),
        )
        mapping.setdefault(key, set()).add(letter)
    return mapping


def main() -> int:
    apply = "--apply" in sys.argv
    letters = asyncio.run(build_letter_map())

    worksheet = _open_worksheet()
    values = worksheet.get_all_values()
    if not values:
        print("Лист пуст — нечего чинить.")
        return 0

    column: list[str] = []          # новая колонка B, начиная со строки 2
    filled: list[tuple[int, str, str, str]] = []
    ambiguous: list[tuple[int, list[str]]] = []
    unmatched: list[tuple[int, list[str]]] = []

    for offset, row in enumerate(values[1:]):  # без заголовка
        cells = list(row) + [""] * (3 - len(row))
        line = offset + 2
        date_str, project, amount = cells[0], cells[1], cells[2]

        if project.strip() or not date_str.strip():
            column.append(project)
            continue

        found = letters.get(_amount_key(date_str, _parse_cost(amount)))
        if not found:
            column.append(project)
            unmatched.append((line, cells[:3]))
        elif len(found) > 1:
            # Две буквы на одну (дату, сумму) — угадывать нельзя, оставляем пусто.
            column.append(project)
            ambiguous.append((line, sorted(found)))
        else:
            letter = next(iter(found))
            column.append(letter)
            filled.append((line, date_str, amount, letter))

    print(f"строк с данными: {len(values) - 1}")
    print(f"будет заполнено: {len(filled)}")
    if ambiguous:
        print(f"неоднозначных (пропущены): {len(ambiguous)}")
        for line, found in ambiguous[:10]:
            print(f"  строка {line}: варианты {found}")
    if unmatched:
        print(f"не нашлись в API (пропущены): {len(unmatched)}")
        for line, cells in unmatched[:10]:
            print(f"  строка {line}: {cells}")
    for line, date_str, amount, letter in filled[:10]:
        print(f"  строка {line}: {date_str} {amount} → {letter}")
    if len(filled) > 10:
        print(f"  … и ещё {len(filled) - 10}")

    if not apply:
        print("\nЭто прогон вхолостую. Записать: --apply")
        return 0
    if not filled:
        print("\nНечего записывать.")
        return 0

    worksheet.update(
        [[value] for value in column],
        f"B2:B{len(column) + 1}",
        value_input_option="USER_ENTERED",
    )
    print(f"\nЗаписано в {settings.google_sheets_worksheet_name}: {len(filled)} ячеек.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
