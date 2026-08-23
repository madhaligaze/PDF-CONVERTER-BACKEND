"""MCP-коннектор: протокол, защита ссылки и потолки на объём ответа.

Google здесь не участвует — сетки подменяются. Проверяем ровно то, что ломается
незаметно: неверный секрет должен выглядеть как отсутствующий маршрут, а не как
защищённый; уведомление не должно получать тела ответа; и ни один инструмент не
должен уметь вернуть таблицу целиком, если она больше потолка.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mcp import google, protocol, tools
from app.mcp.config import PROTOCOL_VERSION, mcp_settings
from app.mcp.routes import router as mcp_router

SECRET = "s3cr3t-for-tests-only-000000000000"

GRID = [
    ["Клиент", "Месяц", "Сумма"],
    ["ImpExtrans", "июль", "1 200 000"],
    ["Аркада", "июль", "340 000"],
    ["ImpExtrans", "август", "980 000"],
]


@pytest.fixture(autouse=True)
def stub_google(monkeypatch):
    """Все походы в Google заменены — тесты про протокол, а не про сеть."""
    google.invalidate_cache()
    monkeypatch.setattr(mcp_settings, "enabled", True, raising=False)
    monkeypatch.setattr(mcp_settings, "secret", SECRET, raising=False)
    monkeypatch.setattr(mcp_settings, "max_cells", 20000, raising=False)
    monkeypatch.setattr(
        type(mcp_settings), "credentials_available", property(lambda self: True)
    )
    monkeypatch.setattr(
        google,
        "list_spreadsheet_files",
        lambda: [{"id": "sheet-1", "name": "Реестр продаж", "modified": "2026-08-01T00:00:00Z"}],
    )
    monkeypatch.setattr(
        google,
        "spreadsheet_meta",
        lambda sid: {
            "id": sid,
            "title": "Реестр продаж",
            "tabs": [{"title": "Общий Реестр Продаж", "index": 0, "rows": 4, "cols": 3}],
        },
    )
    monkeypatch.setattr(google, "read_grid", lambda sid, tab: GRID)
    yield
    google.invalidate_cache()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mcp_router)
    return TestClient(app)


def call(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/mcp/{SECRET}", json=body)


def tool(client, name, **arguments):
    response = call(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


# ── Защита ссылки ────────────────────────────────────────────────────────────


def test_wrong_secret_looks_like_a_missing_route(client) -> None:
    """404, а не 401: защищённый эндпоинт приглашает подбирать, отсутствующий — нет."""
    response = client.post(
        f"/mcp/{SECRET}-wrong", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_empty_secret_setting_closes_the_endpoint(client, monkeypatch) -> None:
    """MCP_ENABLED без MCP_SECRET не должен открывать отчётность в интернет."""
    monkeypatch.setattr(mcp_settings, "secret", "", raising=False)
    response = client.post(f"/mcp/{SECRET}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 404


def test_get_offers_no_server_stream(client) -> None:
    response = client.get(f"/mcp/{SECRET}")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_router_is_not_mounted_when_disabled() -> None:
    """Модуль удаляемый: без configured маршрута в приложении быть не должно."""
    assert mcp_settings.configured is True  # включён фикстурой
    from app.mcp.config import McpSettings

    assert McpSettings(MCP_ENABLED=False, MCP_SECRET=SECRET).configured is False
    assert McpSettings(MCP_ENABLED=True, MCP_SECRET="").configured is False


# ── Протокол ─────────────────────────────────────────────────────────────────


def test_initialize_reports_tools_capability(client) -> None:
    result = call(client, "initialize", {"protocolVersion": PROTOCOL_VERSION}).json()["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]


def test_initialize_echoes_a_known_older_version(client) -> None:
    result = call(client, "initialize", {"protocolVersion": "2024-11-05"}).json()["result"]
    assert result["protocolVersion"] == "2024-11-05"


def test_initialize_falls_back_on_an_unknown_version(client) -> None:
    result = call(client, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION


def test_notification_gets_no_body(client) -> None:
    """Запрос без id — уведомление. Ответ на него сломал бы рукопожатие."""
    response = client.post(
        f"/mcp/{SECRET}", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response.status_code == 202
    assert response.content == b""


def test_tools_list_declares_five_read_only_tools(client) -> None:
    result = call(client, "tools/list").json()["result"]
    names = {t["name"] for t in result["tools"]}
    assert names == {
        "list_spreadsheets",
        "describe_spreadsheet",
        "peek_sheet",
        "read_range",
        "search_rows",
    }
    for declared in result["tools"]:
        assert declared["description"].strip()
        assert declared["inputSchema"]["type"] == "object"


def test_unknown_method_is_a_jsonrpc_error(client) -> None:
    body = call(client, "resources/list").json()
    assert body["error"]["code"] == protocol.METHOD_NOT_FOUND


def test_malformed_body_is_a_parse_error(client) -> None:
    response = client.post(
        f"/mcp/{SECRET}", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == protocol.PARSE_ERROR


def test_batch_returns_one_reply_per_request(client) -> None:
    response = client.post(
        f"/mcp/{SECRET}",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    )
    assert response.status_code == 200
    replies = response.json()
    assert [r["id"] for r in replies] == [1, 2]


# ── Отказы инструментов ──────────────────────────────────────────────────────


def test_tool_failure_stays_a_successful_jsonrpc_reply(client, monkeypatch) -> None:
    """Отказ Google должен дойти до директора словами, а не как «коннектор не отвечает»."""

    def boom(sid, tab):
        raise google.McpError("У сервисного аккаунта нет доступа к таблице")

    monkeypatch.setattr(google, "read_grid", boom)
    result = tool(client, "peek_sheet", spreadsheet="Реестр продаж")
    assert result["isError"] is True
    assert "нет доступа" in result["content"][0]["text"]


def test_unknown_tool_is_reported_as_a_tool_error(client) -> None:
    result = tool(client, "delete_everything")
    assert result["isError"] is True


def test_unknown_argument_is_refused(client) -> None:
    result = tool(client, "peek_sheet", spreadsheet="Реестр продаж", sql="drop")
    assert result["isError"] is True
    assert "sql" in result["content"][0]["text"]


# ── Инструменты ──────────────────────────────────────────────────────────────


def test_peek_sheet_reports_real_row_numbers(client) -> None:
    result = tool(client, "peek_sheet", spreadsheet="Реестр продаж")
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert '"row": 1' in text and "Клиент" in text
    assert '"total_rows": 4' in text


def test_search_rows_finds_by_substring_case_insensitively(client) -> None:
    result = tool(client, "search_rows", spreadsheet="Реестр продаж", query="impextrans")
    text = result["content"][0]["text"]
    assert '"total_matches": 2' in text
    # Строки 2 и 4 таблицы — нумерация как в Google Sheets, не как в срезе.
    assert '"row": 2' in text and '"row": 4' in text


def test_search_rows_can_be_pinned_to_one_column(client) -> None:
    result = tool(client, "search_rows", spreadsheet="Реестр продаж", query="июль", column="Месяц")
    assert '"total_matches": 2' in result["content"][0]["text"]


def test_search_rows_rejects_an_unknown_column(client) -> None:
    result = tool(client, "search_rows", spreadsheet="Реестр продаж", query="x", column="Нет такой")
    assert result["isError"] is True


def test_read_range_slices_and_keeps_the_column_offset(client) -> None:
    result = tool(client, "read_range", spreadsheet="Реестр продаж", range="B2:C3")
    text = result["content"][0]["text"]
    assert '"first_column_index": 2' in text
    assert '"row": 2' in text
    assert "Клиент" not in text  # колонка A в срез не входит


def test_read_range_understands_a_tab_prefix(client) -> None:
    result = tool(
        client, "read_range", spreadsheet="Реестр продаж", range="Общий Реестр Продаж!A1:C2"
    )
    assert result["isError"] is False
    assert '"tab": "Общий Реестр Продаж"' in result["content"][0]["text"]


def test_read_range_rejects_a_malformed_range(client) -> None:
    result = tool(client, "read_range", spreadsheet="Реестр продаж", range="A1:!!")
    assert result["isError"] is True


# ── Потолки ──────────────────────────────────────────────────────────────────


def test_a_huge_sheet_is_truncated_and_says_so(client, monkeypatch) -> None:
    """Без потолка Клод вычитал бы «Сводку все ЮР лица» целиком и упёрся в контекст."""
    monkeypatch.setattr(mcp_settings, "max_cells", 30, raising=False)
    monkeypatch.setattr(
        google, "read_grid", lambda sid, tab: [["a", "b", "c"] for _ in range(500)]
    )
    result = tool(client, "read_range", spreadsheet="Реестр продаж")
    text = result["content"][0]["text"]
    assert '"truncated": true' in text
    assert '"returned_rows": 10' in text  # 30 ячеек / 3 колонки
    assert "search_rows" in text  # подсказка, что делать дальше


def test_max_rows_is_honoured(client) -> None:
    result = tool(client, "read_range", spreadsheet="Реестр продаж", max_rows=2)
    assert '"returned_rows": 2' in result["content"][0]["text"]


def test_search_limit_caps_the_reply(client) -> None:
    result = tool(client, "search_rows", spreadsheet="Реестр продаж", query="июль", limit=1)
    text = result["content"][0]["text"]
    assert '"returned": 1' in text and '"total_matches": 2' in text


# ── Разбор A1 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Лист!A1:H50", ("Лист", "A1:H50")),
        ("'Отчёт по Дебету'!A1:C9", ("Отчёт по Дебету", "A1:C9")),
        ("A1:C9", (None, "A1:C9")),
        ("Общий Реестр Продаж", ("Общий Реестр Продаж", "")),
        ("", (None, "")),
    ],
)
def test_split_range(raw, expected) -> None:
    assert google.split_range(raw) == expected


def test_col_index() -> None:
    assert google._col_index("A") == 0
    assert google._col_index("Z") == 25
    assert google._col_index("AA") == 26


def test_resolve_spreadsheet_id_accepts_a_url() -> None:
    url = "https://docs.google.com/spreadsheets/d/1h5-zZkwuZ3hiKHl0CW22GAJGBdxBl8Byb/edit#gid=0"
    assert google.resolve_spreadsheet_id(url) == "1h5-zZkwuZ3hiKHl0CW22GAJGBdxBl8Byb"


def test_resolve_spreadsheet_id_matches_a_partial_name() -> None:
    assert google.resolve_spreadsheet_id("реестр") == "sheet-1"


def test_resolve_spreadsheet_id_lists_options_when_nothing_matches() -> None:
    with pytest.raises(google.McpError) as exc:
        google.resolve_spreadsheet_id("баланс")
    assert "Реестр продаж" in str(exc.value)


def test_allowed_ids_block_a_foreign_spreadsheet(monkeypatch) -> None:
    monkeypatch.setattr(mcp_settings, "allowed_ids_raw", "sheet-1", raising=False)
    with pytest.raises(google.McpError):
        google.resolve_spreadsheet_id("1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")


# ── Ни одного пишущего инструмента ───────────────────────────────────────────


def test_no_tool_can_write() -> None:
    """Второй барьер под тем, что токен Google выпущен только на чтение."""
    forbidden = ("write", "append", "update", "delete", "set_", "create")
    assert not [name for name in tools.HANDLERS if name.startswith(forbidden)]
