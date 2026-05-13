"""
MCP CLI entry point — запускает MCP сервер через STDIO транспорт.

Использование:
  python -m app.mcp_cli

Для подключения к AI ассистенту (Claude, etc.) через MCP:
  Укажите команду: python -m app.mcp_cli
"""
import asyncio
import logging

from app.database import engine, Base
from app.mcp_server import run_mcp_stdio


async def main():
    logging.basicConfig(level=logging.WARNING)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_mcp_stdio()


if __name__ == "__main__":
    asyncio.run(main())
