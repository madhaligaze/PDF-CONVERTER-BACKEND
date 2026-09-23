"""Фоновый цикл BBC: в Google — только пока дашборд смотрят, и одним запросом.

Цикл читал мастер-лист каждые 15 секунд круглые сутки, ночью и в выходные, когда
дашборд не открыт ни у кого. Каждый проход — три обращения к Google (gspread
открывает таблицу и ищет вкладку двумя запросами метаданных, третий — сами
значения), то есть 12 в минуту из общей квоты 60 и ~17 тысяч в сутки впустую.
Отступ при отказе был мёртвым кодом: `refresh()` глотает исключения, и на 429 цикл
продолжал ходить в Google с прежней частотой.
"""
from __future__ import annotations

import pytest

from app.bbc import live, sheets
from app.bbc.sheets import BbcError


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(live.bbc_settings, "idle_after_seconds", 120.0, raising=False)
    monkeypatch.setattr(live, "_last_demand", None)
    monkeypatch.setattr(live, "_waker", None)
    monkeypatch.setattr(live, "_snapshot", live.Snapshot())
    monkeypatch.setattr(live, "_last_error", None)
    # История снимков и прогонов — не предмет этих тестов, и базы у них нет.
    monkeypatch.setattr(live, "_persist", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sheets, "source_ref", lambda source: ("sheet-id", "Сводка"))
    sheets.invalidate_read_cache()
    yield
    sheets.invalidate_read_cache()


class Google:
    """Подмена чтения мастер-листа со счётчиком обращений."""

    def __init__(self) -> None:
        self.reads = 0
        self.fail: Exception | None = None
        self.grid = [["Клиент"], ["ТОО Тест"]]

    def __call__(self, source: str) -> list[list[str]]:
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        return [list(row) for row in self.grid]


@pytest.fixture
def google(monkeypatch) -> Google:
    fake = Google()
    monkeypatch.setattr(sheets, "read_source", fake)
    # Разбор настоящего листа тут не проверяется: хватит того, что он не падает.
    monkeypatch.setattr(live, "parse_dataset", lambda grid: ([], None))
    return fake


# ── Кто смотрит ──────────────────────────────────────────────────────────────


def test_nobody_watching_means_no_google_read(google: Google) -> None:
    assert live.background_pass() is True
    assert google.reads == 0


def test_the_revision_poll_marks_the_dashboard_as_watched(google: Google) -> None:
    live.revision_payload()

    assert live.background_pass() is True
    assert google.reads == 1


def test_loading_the_data_counts_as_watching_too(google: Google) -> None:
    """Любой, кто берёт строки через `ensure_loaded`: дашборд, выгрузка, бот."""
    live.ensure_loaded()
    reads_to_load = google.reads

    live.background_pass()
    assert google.reads == reads_to_load + 1


def test_watching_expires(monkeypatch, google: Google) -> None:
    clock = iter([1000.0, 1000.0 + 121.0])
    monkeypatch.setattr(live.time, "monotonic", lambda: next(clock))

    live.note_demand()
    assert not live.watched()


def test_zero_window_keeps_the_old_always_on_loop(monkeypatch, google: Google) -> None:
    """Выключатель: 0 — читать всегда, как было до этой правки."""
    monkeypatch.setattr(live.bbc_settings, "idle_after_seconds", 0.0, raising=False)

    live.background_pass()
    assert google.reads == 1


def test_first_look_after_idle_wakes_the_loop_once() -> None:
    """Иначе первый зритель после простоя смотрел бы на старые цифры до 15 с."""
    wakes: list[int] = []
    live.set_waker(lambda: wakes.append(1))

    live.note_demand()
    live.note_demand()
    live.note_demand()

    assert wakes == [1], "будить надо на переходе из простоя, а не на каждом опросе"


# ── Отказ Google ─────────────────────────────────────────────────────────────


def test_a_refused_read_is_reported_to_the_loop(google: Google) -> None:
    """По этому признаку цикл отступает. Раньше отказ был не виден: 429 → опять 15 с."""
    live.note_demand()
    google.fail = BbcError("Google временно ограничил чтение")

    assert live.background_pass() is False


def test_the_error_banner_clears_once_google_answers_again(google: Google) -> None:
    """Раньше ошибка висела до следующей правки листа: хэш прежний — ранний выход,
    и `error` в источнике никто не стирал."""
    live.note_demand()
    assert live.background_pass() is True

    google.fail = BbcError("Google временно ограничил чтение")
    live.background_pass()
    assert live.revision_payload()["sources"]["master"]["error"]

    google.fail = None
    assert live.background_pass() is True
    assert live.revision_payload()["sources"]["master"]["error"] is None


# ── Одно обращение к Google вместо трёх ──────────────────────────────────────


class Tab:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.reads = 0

    def get_all_values(self) -> list[list[str]]:
        self.reads += 1
        if self.fail:
            raise RuntimeError("APIError: [400]: Unable to parse range")
        return [["x"]]


@pytest.fixture
def opener(monkeypatch):
    opened: list[Tab] = []

    def open_worksheet(name=None, spreadsheet_id=None):
        tab = Tab()
        opened.append(tab)
        return tab

    monkeypatch.setattr(sheets, "open_worksheet", open_worksheet)
    return opened


def test_the_tab_is_opened_once_and_then_only_read(opener) -> None:
    """Открытие вкладки — два запроса метаданных. Повторять их на каждом проходе
    незачем: вкладка та же, меняются только значения."""
    for _ in range(3):
        assert sheets.read_values("Сводка", "sheet-id") == [["x"]]

    assert len(opener) == 1
    assert opener[0].reads == 3


def test_a_failed_read_forgets_the_tab(opener) -> None:
    """Вкладку переименовали — следующий проход обязан искать её заново."""
    sheets.read_values("Сводка", "sheet-id")
    opener[0].fail = True

    with pytest.raises(BbcError):
        sheets.read_values("Сводка", "sheet-id")

    sheets.read_values("Сводка", "sheet-id")
    assert len(opener) == 2


def test_the_tab_is_re_resolved_after_a_while(monkeypatch, opener) -> None:
    """Переставили вкладки — «первый лист» уже другой. Держим не дольше срока."""
    # Ровно по одному значению на чтение: `read_values` смотрит на часы один раз.
    clock = iter([0.0, sheets.TAB_HANDLE_TTL_SECONDS + 1])
    monkeypatch.setattr(sheets.time, "monotonic", lambda: next(clock))

    sheets.read_values(None, "sheet-id")
    sheets.read_values(None, "sheet-id")

    assert len(opener) == 2


def test_manual_refresh_forgets_the_tab(opener) -> None:
    sheets.read_values("Сводка", "sheet-id")
    sheets.invalidate_read_cache()
    sheets.read_values("Сводка", "sheet-id")

    assert len(opener) == 2


def test_different_tabs_keep_their_own_handles(opener) -> None:
    sheets.read_values("Журнал", "a")
    sheets.read_values("Продажи", "b")
    sheets.read_values("Журнал", "a")

    assert len(opener) == 2
