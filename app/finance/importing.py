"""Чтение файла учёта: выписки, книги, выгрузки — во что угодно наоборот.

Зачем этот файл написан заново, а не взят у соседей по рынку
────────────────────────────────────────────────────────────
17 сентября 2026 мы прогнали импорт Finmap двадцатью файлами и записали, что
именно он делает с человеческой неточностью (полный протокол — в
`docs/finmap-audit.md`). Четыре вывода оттуда определили устройство этого
модуля:

1. **Одна плохая строка отменяла весь файл.** Двести строк, испорчена 137-я —
   не завелось ничего, сообщение «В строке 138». Здесь наоборот: хорошие строки
   заводятся, плохие остаются в партии импорта с объяснением и правятся по
   одной. Отказ от импорта целиком — это решение человека, а не парсера.

2. **Лишняя колонка ломала разбор технической ошибкой** («Cannot read
   properties of undefined») без номера строки. Здесь колонки ищутся по
   названиям (тот же механизм, что в `app.books.layout`), а незнакомая колонка
   не мешает: она попадает в `raw` и показывается как «не использована».

3. **Минус у суммы терялся молча.** «−15 000» приезжало доходом 15 000.
   Здесь знак — это вид операции, и он разбирается явно: минус, скобки,
   «Дт/Кт», две колонки «Приход»/«Расход».

4. **Порядок частей даты угадывался в каждой строке отдельно.** «12/25/2026»
   читалось как 25 декабря, а «08/03/2026» — как 8 марта, в одном и том же
   файле. Половина американской выгрузки уезжает на другой месяц, и никакой
   ошибки при этом нет. Здесь порядок решается **один раз на файл** по всем
   датам сразу, а если однозначного ответа нет — парсер не угадывает, а
   спрашивает (`Preview.question`).

Правило, из которого всё выведено: **не угадывать там, где ошибка тихая.**
Громкий отказ дороже одной минуты человека. Тихо неверная цифра стоит доверия
ко всему отчёту, и найти её нельзя — она выглядит как цифра.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from app.books.layout import Column, norm, squash

log = logging.getLogger(__name__)


class ImportError_(RuntimeError):
    """Файл не читается целиком: не тот формат, нет шапки, пустой."""


# ── Колонки, которые мы понимаем ─────────────────────────────────────────────
#
# `names` перечисляет написания, встречающиеся в выписках казахстанских банков,
# в выгрузках 1С и в книгах, которые ведут руками. Список открытый: новое
# написание — это строка здесь, а не правка парсера.
#
# `hint` не ставится намеренно. У книги, которую ведут руками, «привычной
# позиции» не существует: файл каждый раз другой. Подсказка нужна там, где
# читают один и тот же лист годами (см. `app.bbc`), здесь она врала бы.

COLUMNS: tuple[Column, ...] = (
    Column(
        key="paid_at",
        title="Дата платежа",
        names=(
            "дата платежа", "дата", "дата операции", "дата проводки", "дата документа",
            "дата транзакции", "дата и время", "дата/время", "date", "payment date",
            "дата списания", "дата поступления денег", "дата зачисления",
        ),
        required=False,
    ),
    Column(
        key="accrued_at",
        title="Дата начисления",
        names=("дата начисления", "дата сделки", "дата акта", "дата реализации", "accrual date"),
        required=False,
    ),
    Column(
        key="period_start",
        title="Период начисления (с)",
        names=('период начисления (дата "с")', "период начисления с", "период с", "начало периода"),
        required=False,
    ),
    Column(
        key="period_end",
        title="Период начисления (по)",
        names=('период начисления (дата "по")', "период начисления по", "период по", "конец периода"),
        required=False,
    ),
    Column(
        key="amount",
        title="Сумма",
        names=(
            "сумма", "сумма в валюте счета", "сумма операции", "сумма платежа", "amount",
            "сумма в валюте счёта", "сумма, тенге", "сумма (kzt)",
        ),
        required=False,
    ),
    Column(
        key="amount_income",
        title="Приход",
        names=("приход", "поступление", "поступления", "кредит", "credit", "дебет счета", "зачисление", "доход"),
        required=False,
    ),
    Column(
        key="amount_expense",
        title="Расход",
        names=("расход", "списание", "списания", "дебет", "debit", "кредит счета", "выплата"),
        required=False,
    ),
    Column(
        key="currency",
        title="Валюта",
        names=("валюта", "currency", "валюта операции", "валюта счета"),
        required=False,
    ),
    Column(
        key="account_from",
        title="Со счёта",
        names=("со счета", "со счёта", "счет списания", "счёт списания", "account from", "откуда"),
        required=False,
    ),
    Column(
        key="account_to",
        title="На счёт",
        names=("на счет", "на счёт", "счет зачисления", "счёт зачисления", "account to", "куда"),
        required=False,
    ),
    Column(
        key="account",
        title="Счёт",
        names=("счет", "счёт", "счет/остаток", "касса", "account"),
        required=False,
    ),
    Column(
        key="kind",
        title="Тип операции",
        names=("тип", "тип операции", "вид операции", "операция", "type", "подтип"),
        required=False,
    ),
    Column(
        key="category",
        title="Категория",
        names=("категория", "статья", "статья затрат", "статья доходов", "category", "назначение"),
        required=False,
    ),
    Column(
        key="subcategory",
        title="Подкатегория",
        names=("подкатегория", "подстатья", "subcategory"),
        required=False,
    ),
    Column(
        key="counterparty",
        title="Контрагент",
        names=(
            "контрагент", "клиент", "поставщик", "плательщик", "получатель", "counterparty",
            "наименование контрагента", "кто заплатил", "кому заплатили",
        ),
        required=False,
    ),
    Column(
        key="project",
        title="Проект",
        names=("проект", "направление", "project", "объект"),
        required=False,
    ),
    Column(
        key="subproject",
        title="Подпроект",
        names=("подпроект", "subproject"),
        required=False,
    ),
    Column(key="tags", title="Теги", names=("теги", "тег", "метки", "tags"), required=False),
    Column(
        key="comment",
        title="Комментарий",
        names=(
            "комментарий", "назначение платежа", "описание", "примечание", "comment",
            "детали операции", "содержание операции",
        ),
        required=False,
    ),
)

#: Что именно нельзя не знать, чтобы строка стала операцией.
ESSENTIAL = ("paid_at", "amount")

#: Слова, по которым строка опознаётся как итоговая, а не как операция.
#: Итоговую строку не удаляют перед импортом почти никогда — и это не ошибка
#: человека, а свойство файла: в книге итог нужен. Finmap на таком файле
#: отказывается целиком («Итого — неверный формат даты»).
TOTAL_WORDS = (
    "итого", "всего", "итог", "баланс", "остаток на конец", "остаток на начало",
    "оборот", "сальдо", "total", "subtotal",
)

#: Слова, означающие вид операции в колонке «Тип».
KIND_WORDS: dict[str, tuple[str, ...]] = {
    "income": ("доход", "поступление", "приход", "income", "зачисление", "кредит", "продажа", "оплата от"),
    "expense": ("расход", "списание", "выплата", "expense", "дебет", "покупка", "оплата поставщику"),
    "transfer": ("перевод", "transfer", "перемещение", "инкассация", "внутренний перевод"),
}

_SPACES = re.compile(r"[\s   ]+")
_MONEY_JUNK = re.compile(r"[^0-9,.\-()]")
_DATE_PARTS = re.compile(r"^\s*(\d{1,4})[.\-/\\](\d{1,2})[.\-/\\](\d{1,4})")
_MONTH_NAMES = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Чтение файла в строки ────────────────────────────────────────────────────


def read_rows(data: bytes, file_name: str) -> list[list[Any]]:
    """Файл → строки. Excel читается с типами ячеек, CSV — как текст.

    Значения ячеек Excel не приводятся к строкам: дата, пришедшая датой, не
    имеет неоднозначности порядка частей, и терять это знание, обратив её в
    текст, — значит создать себе задачу угадывания на пустом месте.
    """
    lower = (file_name or "").lower()
    if lower.endswith((".xlsx", ".xlsm", ".xltx")):
        return _read_xlsx(data)
    if lower.endswith((".csv", ".txt", ".tsv")):
        return _read_csv(data)
    if lower.endswith(".xls"):
        raise ImportError_(
            "Формат .xls (Excel 97–2003) не читается. Откройте файл и сохраните "
            "как .xlsx — это займёт меньше времени, чем настройка конвертера."
        )
    raise ImportError_(
        f"Не понимаю формат файла «{file_name}». Ждём .xlsx или .csv."
    )


def _read_xlsx(data: bytes) -> list[list[Any]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover — библиотека в зависимостях
        raise ImportError_("На сервере нет openpyxl — чтение Excel недоступно") from exc

    try:
        book = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — сюда попадает любой битый zip
        raise ImportError_(f"Файл не открывается как Excel: {exc}") from exc

    # Берём первую вкладку — как и все импортёры на рынке. Но, в отличие от
    # них, если на первой вкладке шапки нет, а на второй есть, читаем вторую:
    # выгрузки из банков любят кладь титульный лист первым.
    best: list[list[Any]] = []
    best_score = -1
    for sheet in book.worksheets:
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        rows = _trim(rows)
        if not rows:
            continue
        try:
            score = _header_score(rows[_guess_header_index(rows)])
        except ImportError_:
            score = 0
        if score > best_score:
            best, best_score = rows, score
    book.close()
    if not best:
        raise ImportError_("В файле нет ни одной заполненной строки")
    return best


def _read_csv(data: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("Не удалось определить кодировку файла")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = [list(row) for row in csv.reader(io.StringIO(text), dialect)]
    return _trim(rows)


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    """Убрать пустые строки с конца и хвостовые пустые колонки."""
    while rows and not any(_has_value(cell) for cell in rows[-1]):
        rows.pop()
    return rows


def _has_value(cell: Any) -> bool:
    if cell is None:
        return False
    if isinstance(cell, str):
        return bool(cell.strip())
    return True


# ── Поиск шапки ──────────────────────────────────────────────────────────────


def _header_score(row: Sequence[Any]) -> int:
    """Сколько ячеек строки похожи на известные нам заголовки."""
    score = 0
    for cell in row:
        header = str(cell or "")
        if not squash(header):
            continue
        for column in COLUMNS:
            if column.matches_exact(header) or column.matches_squashed(header):
                score += 2
                break
            if column.matches_loose(header):
                score += 1
                break
    return score


@dataclass(frozen=True)
class Mapping:
    """Какая колонка файла стала каким полем учёта.

    Своя привязка, а не `app.books.layout.resolve_layout`, и это решение, а не
    дубль. Тот резолвер написан под лист, который читают годами: у колонки есть
    привычная позиция, а спор двух колонок за один заголовок означает, что лист
    подменили, — и отказ читать там правильный.

    Здесь лист каждый раз другой. Заголовок «Дата» законно подходит и под «дату
    платежа», и под «дату начисления», и отказываться читать из-за этого нельзя:
    файл нормальный. Поэтому спор решается порядком объявления в `COLUMNS` —
    сначала то, без чего операции не бывает.

    Что осталось от того резолвера — **запрет угадывать на деньгах**: если на
    денежную колонку претендуют два непохожих заголовка, файл не читается. Цена
    ошибки здесь не «неудобно», а «неверная сумма с уверенным видом».
    """

    columns: dict[str, int]
    how: dict[str, str]
    headers: tuple[str, ...]

    def at(self, key: str) -> int | None:
        return self.columns.get(key)

    def has(self, key: str) -> bool:
        return key in self.columns

    @property
    def used(self) -> set[int]:
        return set(self.columns.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": {
                key: {"index": index, "header": self.headers[index], "how": self.how.get(key, "")}
                for key, index in self.columns.items()
            },
            "width": len(self.headers),
        }


#: Денежные колонки. Для них нечёткое совпадение с несколькими кандидатами —
#: отказ читать файл, а не выбор наугад.
_MONEY_KEYS = frozenset({"amount", "amount_income", "amount_expense"})

#: Насколько заголовок в файле может быть длиннее известного нам названия,
#: чтобы всё ещё считаться им. Три символа — это «Сумма» против «Суммы» и
#: «Дата» против «Даты», то есть падеж и опечатка.
#:
#: Без этого порога нечёткое совпадение хватает лишнее, и это уже случилось:
#: «Номер счёта-фактуры» притянулся к колонке «Счёт» (у той было написание
#: «номер счета»), в него попало «СФ-1024» — и строка легла с замечанием «счёт
#: не найден». Симптом выглядел как ошибка человека, а был нашей жадностью.
_LOOSE_SLACK = 3


def _loose_match(column: Column, header: str) -> bool:
    """Нечёткое совпадение заголовка с названием колонки — с порогом длины."""
    actual = squash(header)
    if len(actual) < 4:
        return False
    for name in column.names:
        wanted = squash(name)
        if len(wanted) < 4:
            continue
        if actual == wanted:
            return True
        if actual.startswith(wanted) and len(actual) - len(wanted) <= _LOOSE_SLACK:
            return True
        if wanted.startswith(actual) and len(wanted) - len(actual) <= _LOOSE_SLACK:
            return True
    return False


def resolve_columns(header: Sequence[Any]) -> Mapping:
    """Привязать заголовки файла к полям учёта.

    Три ступени, от строгой к мягкой, и каждая проходит по всем колонкам в
    порядке объявления: точное совпадение всегда бьёт похожее, а спор за один
    и тот же заголовок решается в пользу того поля, что объявлено раньше.
    """
    headers = [str(value or "") for value in header]
    columns: dict[str, int] = {}
    how: dict[str, str] = {}
    taken: set[int] = set()

    for stage, predicate in (
        ("exact", lambda column, text: column.matches_exact(text)),
        ("squashed", lambda column, text: column.matches_squashed(text)),
        ("loose", _loose_match),
    ):
        for column in COLUMNS:
            if column.key in columns:
                continue
            found = [
                index
                for index, text in enumerate(headers)
                if index not in taken and squash(text) and predicate(column, text)
            ]
            if not found:
                continue
            if len(found) > 1 and stage == "loose" and column.key in _MONEY_KEYS:
                raise ImportError_(
                    f"«{column.title}»: точного заголовка нет, а похожих сразу несколько — "
                    + ", ".join(f"«{headers[index]}»" for index in found)
                    + ". Переименуйте колонку суммы однозначно: читать деньги "
                    "наугад нельзя."
                )
            columns[column.key] = found[0]
            how[column.key] = stage
            taken.add(found[0])

    return Mapping(columns=columns, how=how, headers=tuple(headers))


def _guess_header_index(rows: Sequence[Sequence[Any]], look: int = 25) -> int:
    """Номер строки с шапкой.

    Выгрузки почти всегда начинаются с названия отчёта, периода и реквизитов, и
    шапка таблицы стоит третьей-пятой строкой. Finmap на таком файле падает с
    внутренней ошибкой; здесь шапка ищется как строка с наибольшим числом
    узнанных заголовков.
    """
    best_index, best_score = -1, 0
    for index, row in enumerate(rows[:look]):
        score = _header_score(row)
        if score > best_score:
            best_index, best_score = index, score
    if best_index < 0 or best_score < 2:
        raise ImportError_(
            "Не нашёл строку заголовков. Нужна строка, где названы хотя бы дата "
            "и сумма — например «Дата платежа» и «Сумма»."
        )
    return best_index


# ── Числа ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Money:
    """Разобранная сумма: величина по модулю и знак отдельно.

    Знак вынесен из величины намеренно: дальше он превращается в вид операции,
    а не в отрицательное число в базе. Тогда запись «минус приход» становится
    невыразимой, а не тихо возможной.
    """

    value: Decimal
    negative: bool


def parse_money(raw: Any) -> Money | None:
    """Сумма из ячейки. Понимает пробелы, запятую, скобки, минус, апостроф.

    Возвращает `None`, если в ячейке нет числа вообще. Пустая ячейка — не
    ошибка: в двухколоночной выписке («Приход»/«Расход») одна из двух всегда
    пуста.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        value = Decimal(str(raw))
        return Money(abs(value), value < 0)

    text = str(raw).strip()
    if not text:
        return None
    text = text.lstrip("'")  # наследие выгрузок 1С: числа как текст
    negative = False
    if text.startswith("(") and text.endswith(")"):
        # Бухгалтерские скобки. Finmap их теряет: «(15 000)» приезжает
        # доходом 15 000, то есть ошибка вдвое больше суммы.
        negative = True
        text = text[1:-1]
    text = _SPACES.sub("", text)
    if text.endswith("-"):  # «1 500-» — так пишет часть банковских выгрузок
        negative = True
        text = text[:-1]
    text = _MONEY_JUNK.sub("", text)
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if not text:
        return None

    # Разделители: последний из «,» и «.» считается десятичным, если после него
    # не три цифры. «1,234.56» → 1234.56; «1.234,56» → 1234.56; «1,234» → 1234.
    comma, dot = text.rfind(","), text.rfind(".")
    cut = max(comma, dot)
    if cut >= 0:
        tail = text[cut + 1 :]
        if len(tail) == 3 and tail.isdigit():
            text = text.replace(",", "").replace(".", "")
        else:
            text = text[:cut].replace(",", "").replace(".", "") + "." + tail
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return Money(abs(value), negative or value < 0)


# ── Даты ─────────────────────────────────────────────────────────────────────


@dataclass
class DateReading:
    """Как читать даты этого файла.

    `order` — «dmy» или «mdy». `evidence` — чем это доказано; попадает в
    протокол импорта, чтобы решение было видно человеку, а не считалось
    магией.
    """

    order: str = "dmy"
    evidence: str = ""
    ambiguous: bool = False
    #: Примеры дат, из-за которых порядок остался неизвестным.
    samples: tuple[str, ...] = ()


def _date_parts(text: str) -> tuple[int, int, int] | None:
    match = _DATE_PARTS.match(text)
    if not match:
        return None
    a, b, c = (int(part) for part in match.groups())
    return a, b, c


def decide_date_order(values: Iterable[Any]) -> DateReading:
    """Решить порядок частей даты один раз на файл.

    Правило: если хоть где-то первая часть больше 12 — порядок «день-месяц»;
    если где-то вторая больше 12 — «месяц-день». Нашлось и то и другое — файл
    внутренне противоречив, и это отдельный разговор с человеком, а не выбор
    большинством. Не нашлось ничего — все даты подходят под оба чтения, и тогда
    мы обязаны спросить, а не взять привычное.

    Именно здесь Finmap ошибается тихо: он решает по каждой строке отдельно, и
    в одном файле «12/25/2026» становится 25 декабря, а «08/03/2026» — 8 марта.
    """
    first_big = second_big = 0
    year_first = 0
    samples: list[str] = []
    for value in values:
        if isinstance(value, (datetime, date)):
            continue
        text = str(value or "").strip()
        if not text:
            continue
        parts = _date_parts(text)
        if not parts:
            continue
        a, b, _c = parts
        if a > 31:  # «2026-08-03» — год впереди, порядок однозначен
            year_first += 1
            continue
        if a > 12:
            first_big += 1
        elif b > 12:
            second_big += 1
        elif len(samples) < 5:
            samples.append(text)

    if first_big and second_big:
        return DateReading(
            order="dmy",
            ambiguous=True,
            evidence=(
                f"в файле есть и даты, где первое число больше 12 ({first_big} шт.), "
                f"и даты, где больше 12 второе ({second_big} шт.) — "
                "один и тот же файл записан двумя разными способами"
            ),
            samples=tuple(samples),
        )
    if first_big:
        return DateReading("dmy", f"нашлись даты с числом больше 12 на первом месте ({first_big} шт.)")
    if second_big:
        return DateReading("mdy", f"нашлись даты с числом больше 12 на втором месте ({second_big} шт.)")
    if year_first and not samples:
        return DateReading("dmy", "все даты записаны с года — порядок однозначен")
    if not samples:
        return DateReading("dmy", "даты пришли из Excel датами — порядок не требуется")
    return DateReading(
        order="dmy",
        ambiguous=True,
        evidence="все даты подходят и под «день-месяц», и под «месяц-день»",
        samples=tuple(samples),
    )


def parse_date(raw: Any, reading: DateReading) -> date | None:
    """Дата из ячейки по решённому для файла порядку частей."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # Серийный номер Excel. Эпоха 1899-12-30 — та же, что в «Книгах»;
        # полдень не добавляем: он округляет дату вверх и уводит книгу на день.
        try:
            from datetime import timedelta

            return (datetime(1899, 12, 30) + timedelta(days=float(raw))).date()
        except (OverflowError, ValueError):
            return None

    text = str(raw).strip()
    if not text:
        return None

    parts = _date_parts(text)
    if parts:
        a, b, c = parts
        if a > 31:
            year, month, day = a, b, c
        elif reading.order == "mdy":
            month, day, year = a, b, c
        else:
            day, month, year = a, b, c
        if year < 100:
            year += 2000 if year < 70 else 1900
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # «3 сентября 2026», «03 сен 2026», «Sep 3, 2026»
    lowered = text.lower().replace(",", " ")
    chunks = _SPACES.sub(" ", lowered).split(" ")
    day = month = year = 0
    for chunk in chunks:
        if chunk.isdigit():
            number = int(chunk)
            if number > 31:
                year = number
            elif not day:
                day = number
        else:
            key = chunk[:3]
            if key in _MONTH_NAMES:
                month = _MONTH_NAMES[key]
    if day and month:
        try:
            return date(year or date.today().year, month, day)
        except ValueError:
            return None
    return None


# ── Разбор строк ─────────────────────────────────────────────────────────────


@dataclass
class ParsedRow:
    """Строка файла, разобранная до значений операции."""

    line: int
    raw: dict[str, Any]
    state: str  # imported | skipped | failed (пока без записи — намерение)
    problems: list[dict[str, str]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)

    def problem(self, field_key: str, text: str) -> None:
        self.problems.append({"field": field_key, "text": text})


@dataclass
class Preview:
    """Что мы поняли про файл целиком.

    `question` — единственное место, где импорт останавливается и спрашивает.
    Сюда попадает только то, чего нельзя решить по данным: порядок частей даты,
    когда все даты подходят под оба чтения. Всё остальное решается и
    показывается как решение.
    """

    file_name: str
    header_line: int
    mapping: dict[str, Any]
    unused_columns: list[str]
    rows: list[ParsedRow]
    date_reading: DateReading
    question: dict[str, Any] | None = None
    accounts_missing: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(row.state for row in self.rows)
        return {
            "total": len(self.rows),
            "ready": counter.get("imported", 0),
            "failed": counter.get("failed", 0),
            "skipped": counter.get("skipped", 0),
        }


def _kind_from_word(text: str) -> str | None:
    lowered = norm(text)
    if not lowered:
        return None
    for kind, words in KIND_WORDS.items():
        if any(word in lowered for word in words):
            return kind
    return None


def _is_total_row(cells: Sequence[Any]) -> bool:
    joined = " ".join(norm(cell) for cell in cells if _has_value(cell))
    if not joined:
        return False
    return any(joined.startswith(word) or f" {word}" in f" {joined}" for word in TOTAL_WORDS[:4]) or any(
        joined == word for word in TOTAL_WORDS
    )


def analyze(
    data: bytes,
    file_name: str,
    known_accounts: Sequence[str],
    *,
    date_order: str | None = None,
    default_account: str | None = None,
) -> Preview:
    """Разобрать файл и объяснить, что получилось, ничего не записывая.

    `known_accounts` — названия счетов компании. Счёт не создаётся импортом
    никогда: счёт — это место, где лежат деньги, и «создался сам из опечатки в
    выписке» для него недопустимо. Категории, контрагенты, проекты и теги,
    наоборот, создаются: их появление — нормальная работа, а не риск.
    """
    rows = read_rows(data, file_name)
    header_index = _guess_header_index(rows)
    header = [str(cell or "") for cell in rows[header_index]]
    layout = resolve_columns(header)
    if not layout.has("paid_at"):
        raise ImportError_(
            "В шапке нет колонки даты платежа. Нужны хотя бы две колонки: "
            "когда и сколько."
        )
    # Сумма приходит либо одной колонкой, либо парой «Приход»/«Расход» — так
    # устроена почти любая банковская выписка, и требовать одну «Сумму» значит
    # не читать выписки вообще.
    if not any(layout.has(key) for key in ("amount", "amount_income", "amount_expense")):
        raise ImportError_(
            "В шапке нет колонки суммы. Подойдёт «Сумма» или пара "
            "«Приход» / «Расход»."
        )

    body = rows[header_index + 1 :]

    # Порядок частей даты решается по всему файлу сразу — см. decide_date_order.
    date_cells: list[Any] = []
    for key in ("paid_at", "accrued_at"):
        index = layout.at(key)
        if index is None:
            continue
        date_cells.extend(row[index] for row in body if index < len(row))
    reading = decide_date_order(date_cells)
    if date_order in ("dmy", "mdy"):
        reading = DateReading(date_order, "порядок указан человеком", ambiguous=False)

    accounts_by_name = {norm(name): name for name in known_accounts}
    parsed: list[ParsedRow] = []
    missing_accounts: set[str] = set()

    for offset, cells in enumerate(body):
        line = header_index + offset + 2  # человеку видна нумерация Excel
        raw = {
            header[i] if i < len(header) and header[i] else f"колонка {i + 1}": _jsonable(cells[i])
            for i in range(len(cells))
            if _has_value(cells[i])
        }
        row = ParsedRow(line=line, raw=raw, state="imported")

        if not any(_has_value(cell) for cell in cells):
            row.state = "skipped"
            row.problem("", "пустая строка")
            parsed.append(row)
            continue
        if _is_total_row(cells):
            # Итоговая строка — не ошибка файла. Она нужна человеку в книге.
            row.state = "skipped"
            row.problem("", "строка итога — не операция")
            parsed.append(row)
            continue

        _parse_row(row, cells, layout, reading, accounts_by_name, default_account, missing_accounts)
        parsed.append(row)

    question = None
    if reading.ambiguous and any(row.values.get("paid_at") for row in parsed):
        question = {
            "kind": "date_order",
            "title": "Как записаны даты в этом файле?",
            "text": (
                "Порядок частей даты по файлу определить нельзя: "
                + reading.evidence
                + ". Выберите один раз — он применится ко всем строкам."
            ),
            "samples": list(reading.samples),
            "options": [
                {"value": "dmy", "label": "День · месяц · год", "example": _sample_as(reading.samples, "dmy")},
                {"value": "mdy", "label": "Месяц · день · год", "example": _sample_as(reading.samples, "mdy")},
            ],
        }

    return Preview(
        file_name=file_name,
        header_line=header_index + 1,
        mapping=layout.to_dict(),
        unused_columns=[
            header[i] for i in range(len(header)) if header[i] and i not in layout.used
        ],
        rows=parsed,
        date_reading=reading,
        question=question,
        accounts_missing=sorted(missing_accounts),
    )


def _sample_as(samples: Sequence[str], order: str) -> str:
    """Как будет прочитан первый пример при выбранном порядке."""
    if not samples:
        return ""
    parsed = parse_date(samples[0], DateReading(order))
    return f"{samples[0]} → {parsed.isoformat()}" if parsed else samples[0]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _parse_row(
    row: ParsedRow,
    cells: Sequence[Any],
    layout: Mapping,
    reading: DateReading,
    accounts_by_name: dict[str, str],
    default_account: str | None,
    missing_accounts: set[str],
) -> None:
    """Разобрать одну строку. Замечания складываются, строка не бросает."""

    def cell(key: str) -> Any:
        index = layout.at(key)
        if index is None or index >= len(cells):
            return None
        return cells[index]

    paid_at = parse_date(cell("paid_at"), reading)
    if paid_at is None:
        raw_date = cell("paid_at")
        row.state = "failed"
        if _has_value(raw_date):
            row.problem("paid_at", f"не понял дату «{str(raw_date).strip()}»")
        else:
            row.problem("paid_at", "нет даты платежа")
    row.values["paid_at"] = paid_at.isoformat() if paid_at else None

    accrued = parse_date(cell("accrued_at"), reading)
    row.values["accrued_at"] = accrued.isoformat() if accrued else None
    for key in ("period_start", "period_end"):
        value = parse_date(cell(key), reading)
        row.values[key] = value.isoformat() if value else None

    # Сумма и вид операции разбираются вместе: в двухколоночной выписке вид
    # задаёт как раз то, в какой из колонок стоит число.
    income = parse_money(cell("amount_income"))
    expense = parse_money(cell("amount_expense"))
    plain = parse_money(cell("amount"))
    kind = _kind_from_word(str(cell("kind") or ""))

    amount: Decimal | None = None
    if income and income.value:
        amount, kind = income.value, kind or "income"
    if expense and expense.value:
        if amount is not None:
            # Обе колонки заполнены — это не операция, а свод. Угадывать нельзя.
            row.state = "failed"
            row.problem("amount", "заполнены обе колонки — и «Приход», и «Расход»")
        else:
            amount, kind = expense.value, kind or "expense"
    if amount is None and plain is not None:
        amount = plain.value
        if plain.negative:
            # Минус — это расход. Finmap здесь теряет знак и записывает доход.
            kind = "expense"
        elif kind is None:
            kind = None  # решим по счетам ниже

    if amount is None:
        raw_amount = cell("amount") or cell("amount_income") or cell("amount_expense")
        row.state = "failed"
        if _has_value(raw_amount):
            row.problem("amount", f"не понял сумму «{str(raw_amount).strip()}»")
        else:
            row.problem("amount", "нет суммы")
    row.values["amount"] = str(amount) if amount is not None else None

    # Счета. Название приводится к счёту компании; неизвестное имя — замечание
    # со списком похожих, а не молчаливая подстановка.
    def account(key: str) -> str | None:
        raw = cell(key)
        if not _has_value(raw):
            return None
        name = str(raw).strip()
        found = accounts_by_name.get(norm(name))
        if found:
            return found
        close = [real for key_, real in accounts_by_name.items() if key_.startswith(norm(name)[:4])]
        missing_accounts.add(name)
        row.state = "failed"
        hint = f"; похожие есть: {', '.join(sorted(close)[:3])}" if close else ""
        row.problem(key, f"счёт «{name}» не найден{hint}")
        return None

    account_from = account("account_from")
    account_to = account("account_to")
    single = account("account")

    if account_from and account_to:
        kind = "transfer"
    elif kind is None:
        # Ни типа, ни двух счетов: одна колонка счёта и сумма без знака.
        kind = "income" if account_to else ("expense" if account_from else None)

    if kind is None:
        row.state = "failed"
        row.problem(
            "kind",
            "не понял, доход это или расход: нет ни колонки типа, ни знака суммы, "
            "ни разделения счетов",
        )
    row.values["kind"] = kind

    if single and not account_from and not account_to:
        if kind == "expense":
            account_from = single
        else:
            account_to = single
    elif kind == "expense" and account_to and not account_from:
        # Знак суммы сказал «деньги ушли», а счёт в файле стоит в колонке
        # «На счёт»: в выписке с одной колонкой счёта так и бывает. Счёт
        # переносится в «со счёта» — иначе строка легла бы с замечанием
        # «не указано, с какого счёта ушли деньги», хотя счёт указан.
        account_from, account_to = account_to, None
    elif kind == "income" and account_from and not account_to:
        account_to, account_from = account_from, None
    if default_account:
        if kind in ("income", "transfer") and not account_to:
            account_to = default_account
        if kind in ("expense",) and not account_from:
            account_from = default_account

    if kind == "income" and not account_to:
        row.state = "failed"
        row.problem("account_to", "не указано, на какой счёт пришли деньги")
    if kind == "expense" and not account_from:
        row.state = "failed"
        row.problem("account_from", "не указано, с какого счёта ушли деньги")
    if kind == "transfer" and not (account_from and account_to):
        row.state = "failed"
        row.problem("account_from", "для перевода нужны оба счёта — и откуда, и куда")

    row.values["account_from"] = account_from
    row.values["account_to"] = account_to

    row.values["currency"] = (str(cell("currency")).strip().upper() or None) if _has_value(cell("currency")) else None
    row.values["category"] = _text(cell("category"))
    row.values["subcategory"] = _text(cell("subcategory"))
    row.values["counterparty"] = _text(cell("counterparty"))
    row.values["project"] = _text(cell("project"))
    row.values["subproject"] = _text(cell("subproject"))
    row.values["comment"] = _text(cell("comment")) or ""
    tags = _text(cell("tags"))
    row.values["tags"] = [part.strip() for part in re.split(r"[,;]", tags) if part.strip()] if tags else []

    row.values["external_key"] = fingerprint(row.values)


def _text(value: Any) -> str | None:
    if not _has_value(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _SPACES.sub(" ", str(value)).strip() or None


def fingerprint(values: dict[str, Any]) -> str:
    """Отпечаток операции — по смыслу строки, а не по её номеру.

    Номер строки не годится: в выписку дописывают операции сверху, и тогда та
    же самая операция получит другой номер и заведётся второй раз. Поэтому в
    отпечаток идут дата, сумма, счета, контрагент и комментарий.
    """
    parts = [
        str(values.get("paid_at") or ""),
        str(values.get("amount") or ""),
        str(values.get("kind") or ""),
        str(values.get("account_from") or ""),
        str(values.get("account_to") or ""),
        str(values.get("counterparty") or ""),
        (values.get("comment") or "")[:120],
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "COLUMNS",
    "Mapping",
    "resolve_columns",
    "DateReading",
    "ImportError_",
    "Money",
    "ParsedRow",
    "Preview",
    "analyze",
    "decide_date_order",
    "fingerprint",
    "parse_date",
    "parse_money",
    "read_rows",
]
