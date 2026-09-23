"""Память процесса: кэши обязаны забывать, а тяжёлое — не грузиться зря.

Почему набор существует. На проде бэкенд держался около гигабайта и не
отдавал память до перезапуска. Замер на живой книге «Журнал ГК BBC»:

* сырой ответ Google с оформлением для одной вкладки «Журнал» — 240 МБ
  объектов Python. Кэш «Таблиц» хранил его вместо готового ответа (2 МБ JSON)
  и не выбрасывал никогда: просроченная запись лежала до следующего чтения той
  же вкладки, то есть до перезапуска. Три журнала одной книги — почти гигабайт.
  23.09.2026 «Таблицы» перестали читать Google на сервере вовсе;
* aiogram при импорте строит все 635 моделей Telegram — 125 МБ, и платили их
  даже с выключенным ботом, потому что проверка токена жила внутри модуля бота.

Google здесь не участвует: сеть подменяется, проверяется только то, что
хранится и что выбрасывается.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


# ── «Таблицы»: сервер книг не читает ─────────────────────────────────────────


def test_tablicy_ne_hodyat_v_google() -> None:
    """Главный потребитель памяти ушёл вместе с зеркалом книг Google.

    Сырой ответ Google с оформлением для одной вкладки «Журнала» — 240 МБ
    объектов Python. Теперь импорт из Google и из .xlsx живёт в браузере, а
    сервер только хранит снимок строкой. Возврат клиента Google в модуль — это
    возврат той самой утечки, и тест ловит его в момент появления.
    """
    import ast

    for source in sorted((BACKEND / "app" / "webexcel").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        leaked = {name for name in names if name.split(".")[0] in {"gspread", "google", "openpyxl"}}
        assert not leaked, f"{source.name} снова тянет {sorted(leaked)}"


# ── «Финансы»: кэш клиента Google ────────────────────────────────────────────


@pytest.fixture
def finance_google():
    from app.finance import google

    google.invalidate_cache()
    yield google
    google.invalidate_cache()


def test_finansy_zabyvayut_prosrochennye_znacheniya(finance_google, monkeypatch) -> None:
    """Значения вкладок для переноса в учёт — сетки, и раньше жили вечно."""
    now = [100.0]
    monkeypatch.setattr(finance_google.time, "monotonic", lambda: now[0])

    with finance_google._lock:
        finance_google._remember(finance_google._values_cache, ("book", "Журнал"), [["x"]])
        now[0] += 601  # TTL — 600 секунд
        finance_google._remember(finance_google._values_cache, ("book", "Pay Журнал"), [["y"]])

    assert list(finance_google._values_cache) == [("book", "Pay Журнал")]


def test_finansy_derzhat_ogranichennoe_chislo_zapisey(finance_google, monkeypatch) -> None:
    monkeypatch.setattr(finance_google, "_MAX_ENTRIES", 2)

    with finance_google._lock:
        for book in ("A", "B", "C"):
            finance_google._remember(finance_google._meta_cache, book, {"id": book})

    assert list(finance_google._meta_cache) == ["B", "C"]


def test_finansy_ne_keshiruyut_otkaz_google(finance_google, monkeypatch) -> None:
    """Иначе одна ошибка сети отвечала бы отказом ещё десять минут."""

    class Book:
        def fetch_sheet_metadata(self, params=None):
            raise RuntimeError("APIError: [429]: Quota exceeded")

    monkeypatch.setattr(finance_google, "_open", lambda book_id: Book())

    with pytest.raises(finance_google.GoogleError) as caught:
        finance_google.spreadsheet_meta("book")
    assert "ограничил" in str(caught.value)
    assert finance_google._meta_cache == {}


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
