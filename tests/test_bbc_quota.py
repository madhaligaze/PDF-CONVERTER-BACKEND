"""Квота Google: кэш второстепенных источников и человеческие сообщения об отказе.

Дашборд поймал 429 «Quota exceeded ... Read requests per minute per user».
Мастер-таблицу читает один фоновый цикл — четыре запроса в минуту независимо от
числа вкладок. А «Журнал операций» и «Отдел продаж» ходили в Google на каждый
запрос, мимо всей этой конструкции: одно открытие «Продаж» — три обращения к API
(список листов, отчёт, реестр). С появлением учёток сотрудников по дашборду
ходит уже не один человек, и 60 чтений в минуту выбирались за полминуты.
"""
from __future__ import annotations

import pytest

from app.bbc import sheets


@pytest.fixture(autouse=True)
def clean_cache():
    sheets.invalidate_read_cache()
    yield
    sheets.invalidate_read_cache()


# ── Кэш ──────────────────────────────────────────────────────────────────────────


def test_second_read_within_ttl_does_not_touch_google(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_read(name, spreadsheet_id=None):
        calls.append((name, spreadsheet_id))
        return [["a", "b"]]

    monkeypatch.setattr(sheets, "read_values", fake_read)
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)

    first = sheets.read_cached("Журнал", "sheet-1")
    second = sheets.read_cached("Журнал", "sheet-1")

    assert first == second == [["a", "b"]]
    assert len(calls) == 1, "второе чтение всё-таки ушло в Google"


def test_different_worksheets_are_cached_apart(monkeypatch) -> None:
    """Один ключ на всё сложил бы журнал и продажи в одну ячейку кэша."""
    monkeypatch.setattr(sheets, "read_values", lambda name, sid=None: [[name or "", sid or ""]])
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)

    assert sheets.read_cached("Журнал", "a") == [["Журнал", "a"]]
    assert sheets.read_cached("Продажи", "b") == [["Продажи", "b"]]


def test_expired_entry_is_re_read(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(sheets, "read_values", lambda name, sid=None: calls.append(1) or [["x"]])
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)

    # Ровно по одному значению на чтение: `read_cached` смотрит на часы один раз.
    clock = iter([100.0, 500.0])
    monkeypatch.setattr(sheets.time, "monotonic", lambda: next(clock))

    sheets.read_cached("Журнал", "a")
    sheets.read_cached("Журнал", "a")

    assert len(calls) == 2, "просроченная запись должна перечитываться"


def test_zero_ttl_disables_the_cache(monkeypatch) -> None:
    """Выключенный кэш обязан выключаться, а не притворяться."""
    calls: list[int] = []
    monkeypatch.setattr(sheets, "read_values", lambda name, sid=None: calls.append(1) or [["x"]])
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 0.0, raising=False)

    sheets.read_cached("Журнал", "a")
    sheets.read_cached("Журнал", "a")

    assert len(calls) == 2


def test_manual_refresh_drops_the_cache(monkeypatch) -> None:
    """Кнопка «Обновить» просит свежее — иначе журнал молча остался бы прежним."""
    calls: list[int] = []
    monkeypatch.setattr(sheets, "read_values", lambda name, sid=None: calls.append(1) or [["x"]])
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)

    sheets.read_cached("Журнал", "a")
    sheets.invalidate_read_cache()
    sheets.read_cached("Журнал", "a")

    assert len(calls) == 2


def test_worksheet_list_is_cached(monkeypatch) -> None:
    """«Отдел продаж» спрашивал состав листов на каждый запрос — только чтобы
    выбрать самый свежий «Отчет …». Состав меняется раз в месяц."""
    calls: list[int] = []
    monkeypatch.setattr(
        sheets, "list_worksheets", lambda sid=None: calls.append(1) or [{"title": "Отчет Июль"}]
    )
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)

    sheets.list_worksheets_cached("omip")
    sheets.list_worksheets_cached("omip")

    assert len(calls) == 1


def test_master_source_is_not_read_through_the_cache() -> None:
    """Мастер обязан читаться по-настоящему.

    Цикл живого обновления сравнивает содержимое, чтобы заметить правку в
    таблице. Отдай ему кэш — и он перестанет замечать изменения, то есть
    перестанет делать единственное, ради чего существует.
    """
    import inspect

    from app.bbc import live

    source = inspect.getsource(live)
    assert "read_source_cached" not in source
    assert "read_cached" not in source


# ── Сообщения ────────────────────────────────────────────────────────────────────


def test_quota_error_becomes_a_human_sentence() -> None:
    raw = (
        "APIError: [429]: Quota exceeded for quota metric 'Read requests' and limit "
        "'Read requests per minute per user' of service 'sheets.googleapis.com' for "
        "consumer 'project_number:992129259158'."
    )
    message = sheets.humanize(Exception(raw))

    assert "Google" in message
    assert "проходит само" in message
    # Ни номера проекта, ни имени метрики — это не для человека за экраном.
    assert "992129259158" not in message
    assert "quota metric" not in message


def test_permission_and_missing_sheet_are_named_too() -> None:
    assert "доступа" in sheets.humanize(Exception("APIError: [403]: PERMISSION_DENIED"))
    assert "не найдена" in sheets.humanize(Exception("APIError: [404]: Requested entity"))


def test_unknown_failure_is_passed_through_unchanged() -> None:
    """Придумывать формулировку для незнакомого отказа — врать о причине."""
    assert sheets.humanize(Exception("что-то новое")) == "что-то новое"
