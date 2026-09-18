"""Память процесса: кэши обязаны забывать, а тяжёлое — не грузиться зря.

Почему набор существует. На проде бэкенд держался около гигабайта и не
отдавал память до перезапуска. Замер на живой книге «Журнал ГК BBC»:

* сырой ответ Google с оформлением для одной вкладки «Журнал» — 240 МБ
  объектов Python. Кэш «Таблиц» хранил его вместо готового ответа (2 МБ JSON)
  и не выбрасывал никогда: просроченная запись лежала до следующего чтения той
  же вкладки, то есть до перезапуска. Три журнала одной книги — почти гигабайт;
* aiogram при импорте строит все 635 моделей Telegram — 125 МБ, и платили их
  даже с выключенным ботом, потому что проверка токена жила внутри модуля бота.

Google здесь не участвует: сеть подменяется, проверяется только то, что
хранится и что выбрасывается.
"""
from __future__ import annotations

import gc
import subprocess
import sys
import weakref
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


# ── «Таблицы»: кэш готовых вкладок ──────────────────────────────────────────


class _Raw(dict):
    """Сырой ответ Google. Подкласс — только ради weakref: у dict её нет."""


def _raw(rows: int = 3, cols: int = 2) -> _Raw:
    return _Raw(
        spreadsheet_title="Журнал ГК BBC",
        sheet={"data": [{"rowData": [{"values": [{}] * cols} for _ in range(rows)]}]},
    )


def _payload(raw: dict) -> bytes:
    """Готовый ответ вкладки — байты, как их отдаёт маршрут."""
    return raw["spreadsheet_title"].encode()


@pytest.fixture
def webexcel(monkeypatch):
    from app.webexcel import google

    google.invalidate_cache()
    monkeypatch.setattr(google.webexcel_settings, "cache_ttl_seconds", 600.0, raising=False)
    yield google
    google.invalidate_cache()


def test_syroy_grid_ne_ostaetsya_v_pamyati(webexcel, monkeypatch) -> None:
    """Главный дефект: кэш держал сырой грид, а не готовую вкладку."""
    held: list[weakref.ref] = []

    def fetch(sid, tab):
        raw = _raw()
        held.append(weakref.ref(raw))
        return raw

    monkeypatch.setattr(webexcel, "fetch_tab_grid", fetch)

    result = webexcel.cached_tab("book", "Журнал", _payload)
    gc.collect()

    assert result == "Журнал ГК BBC".encode()
    assert held and held[0]() is None, "сырой ответ Google пережил запрос"


def test_povtornoe_otkrytie_vkladki_ne_hodit_v_google(webexcel, monkeypatch) -> None:
    """Ради этого кэш и заведён: квота одна на дашборд и «Таблицы»."""
    calls: list[str] = []
    monkeypatch.setattr(webexcel, "fetch_tab_grid", lambda sid, tab: calls.append(tab) or _raw())

    first = webexcel.cached_tab("book", "Журнал", _payload)
    second = webexcel.cached_tab("book", "Журнал", _payload)

    assert first == second
    assert calls == ["Журнал"]


def test_prosrochennaya_vkladka_vybrasyvaetsya_a_ne_lezhit(webexcel, monkeypatch) -> None:
    """Раньше просроченное лежало до чтения той же вкладки — то есть вечно."""
    monkeypatch.setattr(webexcel, "fetch_tab_grid", lambda sid, tab: _raw())
    now = [100.0]
    monkeypatch.setattr(webexcel.time, "monotonic", lambda: now[0])

    webexcel.cached_tab("book", "Журнал", _payload)
    now[0] += 601  # TTL — 600 секунд
    webexcel.cached_tab("book", "Справочник", _payload)

    assert list(webexcel._tab_cache) == [("book", "Справочник")]


def test_kesh_vkladok_ogranichen_po_obyomu(webexcel, monkeypatch) -> None:
    """Потолок по байтам, а не по числу: вкладки различаются в двадцать раз."""
    monkeypatch.setattr(webexcel, "_TAB_CACHE_MAX_BYTES", 100)
    monkeypatch.setattr(webexcel, "fetch_tab_grid", lambda sid, tab: _raw())

    def forty_bytes(raw):
        return b"x" * 40

    webexcel.cached_tab("book", "a", forty_bytes)  # 40 байт
    webexcel.cached_tab("book", "b", forty_bytes)  # 80
    webexcel.cached_tab("book", "c", forty_bytes)  # 120 > 100 → уходит самая старая

    assert list(webexcel._tab_cache) == [("book", "b"), ("book", "c")]


def test_vkladka_bolshe_potolka_vsyo_ravno_otdaetsya(webexcel, monkeypatch) -> None:
    """Потолок ограничивает хранение, а не ответ: последняя запись остаётся."""
    monkeypatch.setattr(webexcel, "_TAB_CACHE_MAX_BYTES", 10)
    monkeypatch.setattr(webexcel, "fetch_tab_grid", lambda sid, tab: _raw())

    body = webexcel.cached_tab("book", "Журнал", lambda raw: b"x" * 50)

    assert body == b"x" * 50
    assert list(webexcel._tab_cache) == [("book", "Журнал")]


def test_knopka_obnovit_sbrasyvaet_i_gotovye_vkladki(webexcel, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(webexcel, "fetch_tab_grid", lambda sid, tab: calls.append(tab) or _raw())

    webexcel.cached_tab("book", "Журнал", _payload)
    webexcel.invalidate_cache()
    webexcel.cached_tab("book", "Журнал", _payload)

    assert len(calls) == 2


def test_otkaz_google_ne_kladetsya_v_kesh(webexcel, monkeypatch) -> None:
    """Иначе одна ошибка сети отвечала бы отказом ещё десять минут."""
    def boom(sid, tab):
        raise webexcel.WebExcelError("Google временно ограничил чтение")

    monkeypatch.setattr(webexcel, "fetch_tab_grid", boom)

    with pytest.raises(webexcel.WebExcelError):
        webexcel.cached_tab("book", "Журнал", _payload)
    assert webexcel._tab_cache == {}


def test_ostalnye_keshi_tozhe_zabyvayut_prosrochennoe(webexcel, monkeypatch) -> None:
    """Значения вкладок для переноса в учёт — тоже сетки, и тоже жили вечно."""
    now = [100.0]
    monkeypatch.setattr(webexcel.time, "monotonic", lambda: now[0])

    with webexcel._lock:
        webexcel._remember(webexcel._values_cache, ("book", "Журнал"), [["x"]])
        now[0] += 601
        webexcel._remember(webexcel._values_cache, ("book", "Pay Журнал"), [["y"]])

    assert list(webexcel._values_cache) == [("book", "Pay Журнал")]


def test_ostalnye_keshi_ogranicheny_po_chislu(webexcel, monkeypatch) -> None:
    monkeypatch.setattr(webexcel, "_MAX_ENTRIES", 2)

    with webexcel._lock:
        for ref in ("A", "B", "C"):
            webexcel._remember(webexcel._ref_cache, ("book", ref), [ref])

    assert list(webexcel._ref_cache) == [("book", "B"), ("book", "C")]


def test_marshrut_vkladki_otdaet_to_zhe_chto_ran_she(monkeypatch) -> None:
    """Фронт собирает книгу из этого ответа — форма обязана остаться прежней."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.webexcel import google, routes

    google.invalidate_cache()
    monkeypatch.setattr(google.webexcel_settings, "cache_ttl_seconds", 600.0, raising=False)
    monkeypatch.setattr(routes.webexcel_settings, "enabled", True, raising=False)
    monkeypatch.setattr(
        type(routes.webexcel_settings), "credentials_available", property(lambda self: True)
    )
    grid = {
        "spreadsheet_title": "Журнал ГК BBC",
        "spreadsheet_locale": "ru_RU",
        "sheet": {
            "properties": {"sheetId": 7, "title": "Журнал", "gridProperties": {}},
            "data": [{"rowData": [{"values": [{"effectiveValue": {"stringValue": "Аренда"}}]}]}],
        },
    }
    calls: list[str] = []
    monkeypatch.setattr(google, "fetch_tab_grid", lambda sid, tab: calls.append(tab) or grid)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    first = client.get("/web-excel/sources/book/tab", params={"title": "Журнал"})
    second = client.get("/web-excel/sources/book/tab", params={"title": "Журнал"})
    google.invalidate_cache()

    assert first.status_code == 200
    body = first.json()
    assert set(body) == {
        "spreadsheet_id", "spreadsheet_title", "sheet", "styles", "stats", "fonts",
        "checkboxes", "lists",
    }
    assert body["spreadsheet_id"] == "book"
    assert body["spreadsheet_title"] == "Журнал ГК BBC"
    assert body["sheet"]["cellData"]["0"]["0"]["v"] == "Аренда"
    assert second.content == first.content
    assert calls == ["Журнал"], "повторное открытие ушло в Google"

    # Байт в байт с тем, как FastAPI отдавал этот же словарь раньше, когда
    # маршрут возвращал его, а не готовые байты.
    from app.webexcel.univer import convert_tab

    converted = convert_tab(grid)
    before = FastAPI()

    @before.get("/tab")
    def old_route() -> dict:
        return {
            "spreadsheet_id": "book",
            "spreadsheet_title": grid["spreadsheet_title"],
            **{k: converted[k] for k in ("sheet", "styles", "stats", "fonts", "checkboxes", "lists")},
        }

    old = TestClient(before).get("/tab")
    assert first.content == old.content
    assert first.headers["content-type"] == old.headers["content-type"]


# ── MCP: сетки вкладок ──────────────────────────────────────────────────────


def test_mcp_setki_ogranicheny_i_zabyvayut(monkeypatch) -> None:
    """Сетка через MCP читается целиком, без потолка строк: «Тех.Журнал» —
    11 409 строк. Раньше каждая прочитанная вкладка оставалась в памяти."""
    from app.mcp import google

    google.invalidate_cache()
    monkeypatch.setattr(google, "_GRID_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(google.mcp_settings, "cache_ttl_seconds", 60.0, raising=False)

    class Sheet:
        def worksheet(self, tab):
            return type("W", (), {"get_all_values": lambda self: [[tab]]})()

    monkeypatch.setattr(google, "_open", lambda sid: Sheet())

    for tab in ("a", "b", "c"):
        assert google.read_grid("book", tab) == [[tab]]

    assert list(google._grid_cache) == [("book", "b"), ("book", "c")]
    google.invalidate_cache()


# ── Дашборд: кэш второстепенных источников ──────────────────────────────────


def test_bbc_prosrochennyy_istochnik_vybrasyvaetsya(monkeypatch) -> None:
    """Лист «Отчет …» меняется раз в месяц, и прошломесячный лежал бы вечно."""
    from app.bbc import sheets

    sheets.invalidate_read_cache()
    monkeypatch.setattr(sheets, "read_values", lambda name, sid=None: [[name]])
    monkeypatch.setattr(sheets.bbc_settings, "cache_ttl_seconds", 60.0, raising=False)
    # По одному взгляду на часы на чтение — как и прежде.
    clock = iter([100.0, 500.0])
    monkeypatch.setattr(sheets.time, "monotonic", lambda: next(clock))

    sheets.read_cached("Отчет Август", "omip")
    sheets.read_cached("Отчет Сентябрь", "omip")

    assert list(sheets._cache) == [("omip", "Отчет Сентябрь")]
    sheets.invalidate_read_cache()


# ── Счётчики неудачных входов ───────────────────────────────────────────────


def test_finansy_ne_hranyat_pustye_schetchiki() -> None:
    """Проверка счётчика на каждом входе оставляла пустой список навсегда."""
    from app.finance import auth

    auth._ATTEMPTS.clear()
    assert auth._too_many_attempts("someone@example.com") is False
    assert "someone@example.com" not in auth._ATTEMPTS


def test_finansy_schetchik_vsyo_eshche_blokiruet() -> None:
    from app.finance import auth

    auth._ATTEMPTS.clear()
    for _ in range(auth._MAX_ATTEMPTS):
        auth._note_failure("guess@example.com")
    assert auth._too_many_attempts("guess@example.com") is True
    auth._ATTEMPTS.clear()


def test_finansy_perebor_po_adresam_ne_kopitsya(monkeypatch) -> None:
    from app.finance import auth

    auth._ATTEMPTS.clear()
    monkeypatch.setattr(auth, "_SWEEP_AT", 10)
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])

    for index in range(10):
        auth._note_failure(f"guess{index}@example.com")
    now[0] += auth._WINDOW + 1
    auth._note_failure("fresh@example.com")

    assert set(auth._ATTEMPTS) == {"fresh@example.com"}
    auth._ATTEMPTS.clear()


def test_zharkie_popytki_pri_chistke_ne_teryayutsya(monkeypatch) -> None:
    """Чистка выбрасывает только остывшее — блокировку она снимать не должна."""
    from app.bbc import auth

    auth._failures.clear()
    monkeypatch.setattr(auth, "_FAILURES_SWEEP_AT", 2)
    monkeypatch.setattr(auth.time, "monotonic", lambda: 1000.0)

    for _ in range(auth.MAX_LOGIN_FAILURES):
        auth._record_failure("admin", "10.0.0.1")

    with pytest.raises(auth.AuthError):
        auth._assert_not_throttled("admin", "10.0.0.1")
    auth._failures.clear()


def test_bbc_schetchiki_perebora_ne_rastut_bez_kontsa(monkeypatch) -> None:
    """Перебор по списку имён: каждое имя — новый ключ, и назад его никто не
    спрашивал. Словарь рос бы на каждую попытку, пока процесс жив."""
    from app.bbc import auth

    auth._failures.clear()
    monkeypatch.setattr(auth, "_FAILURES_SWEEP_AT", 10)
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])

    for index in range(10):
        auth._record_failure(f"user{index}", "10.0.0.1")
    now[0] += auth.LOGIN_LOCKOUT_SECONDS + 1  # все старые попытки остыли
    auth._record_failure("fresh", "10.0.0.2")

    assert set(auth._failures) == {"user:fresh", "ip:10.0.0.2"}
    auth._failures.clear()


# ── Telegram и перезапуск по правке файлов ──────────────────────────────────


def test_bez_tokena_aiogram_ne_gruzitsya() -> None:
    """125 МБ и 4 секунды старта — за бота, которого нет.

    Отдельный процесс: в общем процессе тестов aiogram мог загрузить кто-то
    другой, и проверка ничего бы не доказала.
    """
    script = """
import asyncio, sys
import app.main as main
from app.core.config import settings
from app.bbc.config import bbc_settings
main._run_migrations = lambda: None
settings.telegram_bot_token = None
settings.autocall_auto_sync_enabled = False
bbc_settings.poll_interval_seconds = 0
async def run():
    async with main.lifespan(main.app):
        pass
asyncio.run(run())
print("aiogram" in sys.modules)
"""
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True, timeout=180
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "False"


@pytest.mark.parametrize(
    ("environment", "flag", "expected"),
    [
        ("development", None, True),  # локальный `python main.py` — как было
        ("development", False, False),  # контейнер: исходников снаружи нет
        ("production", None, False),
        ("production", True, True),
    ],
)
def test_perezapusk_po_pravke_faylov(monkeypatch, environment, flag, expected) -> None:
    """В контейнере перезапуск по правке — два лишних процесса и слежка за
    файлами, которые никто не правит: 57 МБ и постоянная нагрузка на ядро."""
    import main as entrypoint

    monkeypatch.setattr(entrypoint.settings, "environment", environment)
    monkeypatch.setattr(entrypoint.settings, "app_reload", flag)

    assert entrypoint.reload_enabled() is expected


def test_app_reload_chitaetsya_iz_okruzheniya(monkeypatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("APP_RELOAD", "false")
    assert Settings().app_reload is False
    monkeypatch.delenv("APP_RELOAD")
    assert Settings().app_reload is None
