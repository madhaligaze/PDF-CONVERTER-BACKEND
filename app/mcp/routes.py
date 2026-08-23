"""HTTP-транспорт коннектора: POST /mcp/{secret}.

Секрет живёт в пути URL, а не в заголовке, потому что диалог «Add custom
connector» на claude.ai принимает только адрес: поля для произвольного
заголовка там нет (оно есть лишь в Claude Code). Значит ссылка и есть пароль —
отсюда постоянное время сравнения и вычищенный лог.

Роутер подключается на уровне приложения, а не через api_router: иначе адрес
стал бы /api/v1/mcp/... — длиннее и связан с версионированием API, которого у
коннектора нет.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.mcp.config import PROTOCOL_VERSION, mcp_settings
from app.mcp.protocol import PARSE_ERROR, dispatch, negotiate_version

log = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Ответ на неверный секрет. 404, а не 401: защищённый эндпоинт приглашает
# подбирать, несуществующий — нет. Тело совпадает с обычным ответом FastAPI на
# отсутствующий маршрут, чтобы сканер не отличил один от другого.
_NOT_FOUND = JSONResponse(status_code=404, content={"detail": "Not Found"})


def _authorized(secret: str) -> bool:
    expected = mcp_settings.secret
    if not mcp_settings.configured or not expected:
        return False
    return hmac.compare_digest(secret, expected)


@router.post("/{secret}")
async def mcp_endpoint(secret: str, request: Request) -> Response:
    if not _authorized(secret):
        # Пишем факт попытки, но никогда — сам секрет: лог Railway читают люди,
        # у которых доступа к отчётности может и не быть.
        log.warning("MCP: попытка обращения с неверным секретом (%s)", request.client.host if request.client else "?")
        return _NOT_FOUND

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — тело пришло не JSON
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Тело запроса не JSON"}},
        )

    version = PROTOCOL_VERSION
    if isinstance(payload, dict) and payload.get("method") == "initialize":
        version = negotiate_version((payload.get("params") or {}).get("protocolVersion"))
    else:
        header = request.headers.get("mcp-protocol-version")
        if header:
            version = negotiate_version(header)
    headers = {"MCP-Protocol-Version": version}

    # Пакет сообщений — клиент вправе прислать массив.
    if isinstance(payload, list):
        replies = [reply for message in payload if (reply := dispatch(message)) is not None]
        if not replies:
            return Response(status_code=202, headers=headers)
        return JSONResponse(content=replies, headers=headers)

    reply = dispatch(payload)
    if reply is None:  # уведомление — ответа по протоколу быть не должно
        return Response(status_code=202, headers=headers)
    return JSONResponse(content=reply, headers=headers)


@router.get("/{secret}")
async def mcp_stream(secret: str) -> Response:
    """Серверного стрима нет — все ответы отдаются прямо на POST.

    Спецификация это разрешает: сервер, не поддерживающий SSE-канал, обязан
    вернуть 405, и клиент просто не открывает его.
    """
    if not _authorized(secret):
        return _NOT_FOUND
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/{secret}")
async def mcp_close(secret: str) -> Response:
    """Закрытие сессии. Сервер stateless, закрывать нечего — отвечаем успехом."""
    if not _authorized(secret):
        return _NOT_FOUND
    return Response(status_code=204)
