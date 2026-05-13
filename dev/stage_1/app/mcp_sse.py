"""
MCP HTTP SSE сервер — запускает MCP сервер через SSE транспорт.
Позволяет AI ассистентам подключаться через HTTP.

Использование:
  python -m app.mcp_sse

Сервер будет доступен по адресу: http://localhost:8001/mcp
"""
import asyncio
import logging
import json
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.database import engine, Base
from app.config import settings
from app.mcp_server import server

logger = logging.getLogger(__name__)

mcp_app = FastAPI(title="MCP SSE Transport")

sessions: dict[str, asyncio.Queue] = {}


@mcp_app.get("/mcp")
async def mcp_sse(request: Request):
    session_id = str(uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_generator():
        try:
            yield {"event": "endpoint", "data": f"/mcp/message?session_id={session_id}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30)
                    yield message
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            sessions.pop(session_id, None)

    return EventSourceResponse(event_generator())


@mcp_app.post("/mcp/message")
async def mcp_message(request: Request):
    session_id = request.query_params.get("session_id")
    if not session_id or session_id not in sessions:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    body = await request.json()
    queue = sessions[session_id]

    async def handle_message():
        result = await server.handle_message(body)
        return result

    # Placeholder for actual MCP message handling
    # In production, use server.handle_message properly
    response = {"result": "ok", "session_id": session_id}
    return JSONResponse(response)


@mcp_app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(mcp_app, host="0.0.0.0", port=8001)
