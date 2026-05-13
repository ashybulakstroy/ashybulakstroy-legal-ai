import sys
import json
import logging
from typing import Any

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent, CallToolResult

from app.config import settings
from app.database import async_session_factory
from app.services.legislation_service import LegislationService
from app.services.ai_service import generate_legal_analysis

logger = logging.getLogger(__name__)

server = Server(settings.mcp_server_name)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_laws",
            description="Поиск законов и нормативно-правовых актов РК по текстовому запросу",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос (ключевые слова)"},
                    "limit": {"type": "integer", "description": "Максимальное количество результатов", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_law_detail",
            description="Получить полный текст закона/НПА РК по ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "law_id": {"type": "integer", "description": "ID закона"},
                },
                "required": ["law_id"],
            },
        ),
        Tool(
            name="get_categories",
            description="Получить список категорий нормативно-правовых актов РК",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="legal_advice",
            description="Проанализировать ситуацию и найти применимые законы РК",
            inputSchema={
                "type": "object",
                "properties": {
                    "situation": {"type": "string", "description": "Описание юридической ситуации"},
                },
                "required": ["situation"],
            },
        ),
        Tool(
            name="search_articles",
            description="Поиск статей законов РК по текстовому запросу",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "limit": {"type": "integer", "description": "Максимальное количество результатов", "default": 20},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    async with async_session_factory() as db:
        service = LegislationService(db)
        try:
            if name == "search_laws":
                laws = await service.search_laws(
                    arguments["query"],
                    arguments.get("limit", 20),
                )
                results = [
                    {
                        "id": law.id,
                        "title": law.title,
                        "number": law.number,
                        "category": law.category.name if law.category else None,
                        "status": law.status.value if hasattr(law.status, 'value') else str(law.status),
                        "summary": law.summary[:500] if law.summary else None,
                        "date_adopted": str(law.date_adopted) if law.date_adopted else None,
                    }
                    for law in laws
                ]
                return TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))

            elif name == "get_law_detail":
                law = await service.get_law(arguments["law_id"])
                if not law:
                    return TextContent(type="text", text=json.dumps(
                        {"error": "Закон не найден"}, ensure_ascii=False
                    ))
                articles = [
                    {
                        "number": a.number,
                        "title": a.title,
                        "content": a.content,
                    }
                    for a in (law.articles or [])
                ]
                result = {
                    "id": law.id,
                    "title": law.title,
                    "number": law.number,
                    "category": law.category.name if law.category else None,
                    "status": law.status.value if hasattr(law.status, 'value') else str(law.status),
                    "date_adopted": str(law.date_adopted) if law.date_adopted else None,
                    "date_effective": str(law.date_effective) if law.date_effective else None,
                    "summary": law.summary,
                    "full_text": law.full_text,
                    "articles": articles,
                }
                return TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))

            elif name == "get_categories":
                cats = await service.get_categories()
                results = [
                    {
                        "id": c.id,
                        "name": c.name,
                        "slug": c.slug,
                        "type": c.type.value if hasattr(c.type, 'value') else str(c.type),
                        "description": c.description,
                        "children": [
                            {"id": ch.id, "name": ch.name, "slug": ch.slug}
                            for ch in (c.children or [])
                        ],
                    }
                    for c in cats
                ]
                return TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))

            elif name == "legal_advice":
                pairs = await service.find_relevant_articles(arguments["situation"])
                if not pairs:
                    return TextContent(type="text", text=json.dumps(
                        {
                            "situation": arguments["situation"],
                            "analysis": "По данной ситуации не найдено применимых законов. Рекомендуется обратиться к юристу.",
                            "relevant_laws": [],
                            "relevant_articles": [],
                        },
                        ensure_ascii=False, indent=2,
                    ))
                relevant_laws_json = []
                relevant_articles_json = []
                for law, articles in pairs:
                    relevant_laws_json.append({
                        "id": law.id,
                        "title": law.title,
                        "number": law.number,
                        "category": law.category.name if law.category else None,
                        "summary": law.summary[:300] if law.summary else None,
                    })
                    for art in articles:
                        relevant_articles_json.append({
                            "id": art.id,
                            "number": art.number,
                            "title": art.title,
                            "content": art.content[:1000] if art.content else None,
                            "law_id": law.id,
                            "law_title": law.title,
                        })
                analysis = await generate_legal_analysis(arguments["situation"], pairs)
                return TextContent(type="text", text=json.dumps(
                    {
                        "situation": arguments["situation"],
                        "analysis": analysis,
                        "relevant_laws": relevant_laws_json,
                        "relevant_articles": relevant_articles_json,
                    },
                    ensure_ascii=False, indent=2,
                ))

            elif name == "search_articles":
                articles = await service.search_articles(
                    arguments["query"],
                    arguments.get("limit", 20),
                )
                results = [
                    {
                        "id": a.id,
                        "number": a.number,
                        "title": a.title,
                        "content": a.content[:500] if a.content else None,
                        "law_id": a.law_id,
                        "law_title": a.law.title if a.law else None,
                    }
                    for a in articles
                ]
                return TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))

            else:
                return TextContent(type="text", text=json.dumps(
                    {"error": f"Неизвестный инструмент: {name}"}, ensure_ascii=False
                ))

        except Exception as e:
            logger.exception("Error in MCP tool call")
            return TextContent(type="text", text=json.dumps(
                {"error": str(e)}, ensure_ascii=False
            ))


async def run_mcp_stdio():
    async with server:
        await server.run_stdio()
