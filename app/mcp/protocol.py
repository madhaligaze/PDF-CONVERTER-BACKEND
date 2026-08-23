"""Диспетчер MCP поверх JSON-RPC 2.0.

Реализовано вручную, без SDK `mcp`, и это осознанно. Streamable HTTP в
stateless-режиме — это обычный POST с телом JSON-RPC и обычным JSON-ответом;
здесь на это уходит полторы сотни строк. SDK принёс бы свой session manager со
своим lifespan, который пришлось бы вкладывать в уже существующий lifespan в
`app/main.py`, плюс новую зависимость сразу в двух файлах: прод ставится из
requirements-prod.txt, а не из pyproject.toml, и они уже расходятся.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

from app.mcp.config import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from app.mcp.google import McpError
from app.mcp.tools import HANDLERS, TOOLS

log = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _fail(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def negotiate_version(requested: Any) -> str:
    """Версия протокола: эхо клиентской, если знакома, иначе наша."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Выполнить инструмент.

    Отказ инструмента возвращается как `isError: true` внутри УСПЕШНОГО
    JSON-RPC-ответа, а не как транспортная ошибка. Разница практическая:
    транспортную ошибку Клод не может пересказать — директор увидит «коннектор
    не отвечает» вместо «у сервисного аккаунта нет доступа к этой таблице».
    """
    handler = HANDLERS.get(name)
    if handler is None:
        known = ", ".join(sorted(HANDLERS))
        return {
            "content": [{"type": "text", "text": f"Нет такого инструмента: {name}. Есть: {known}"}],
            "isError": True,
        }

    accepted = set(inspect.signature(handler).parameters)
    unknown = set(arguments) - accepted
    if unknown:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Инструмент {name} не принимает аргументы: {', '.join(sorted(unknown))}. "
                        f"Принимает: {', '.join(sorted(accepted)) or 'ничего'}"
                    ),
                }
            ],
            "isError": True,
        }

    try:
        text = handler(**arguments)
    except McpError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except TypeError as exc:  # не хватает обязательного аргумента
        return {"content": [{"type": "text", "text": f"{name}: {exc}"}], "isError": True}
    except Exception as exc:  # noqa: BLE001 — наружу не должно улетать ничего сырого
        log.exception("MCP: инструмент %s упал", name)
        return {
            "content": [{"type": "text", "text": f"Не удалось выполнить {name}: {exc}"}],
            "isError": True,
        }

    return {"content": [{"type": "text", "text": text}], "isError": False}


def dispatch(message: Any) -> dict[str, Any] | None:
    """Обработать одно JSON-RPC сообщение. None = это уведомление, ответа нет."""
    if not isinstance(message, dict):
        return _fail(None, INVALID_REQUEST, "Ожидается JSON-объект")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _fail(request_id, INVALID_PARAMS, "params должен быть объектом")

    # Уведомление — запроса без id. Ответа по протоколу быть не должно.
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _fail(request_id, INVALID_REQUEST, "Не указан method")

    if is_notification:
        return None

    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": negotiate_version(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Доступ к рабочим Google-таблицам компании: отчётность, реестр "
                    "продаж, журнал, сводки. Только чтение. Таблицы большие — сначала "
                    "list_spreadsheets и peek_sheet, чтобы понять раскладку, и "
                    "search_rows для поиска по значению. Читать таблицу целиком через "
                    "read_range без диапазона не нужно."
                ),
            },
        )

    if method == "ping":
        return _ok(request_id, {})

    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _fail(request_id, INVALID_PARAMS, "Не указано имя инструмента")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _fail(request_id, INVALID_PARAMS, "arguments должен быть объектом")
        return _ok(request_id, _call_tool(name, arguments))

    return _fail(request_id, METHOD_NOT_FOUND, f"Метод не поддерживается: {method}")
