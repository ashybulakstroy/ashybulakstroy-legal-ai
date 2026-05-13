import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import engine, Base, get_db
from app.api.routes import router as api_router
from app.admin.admin import setup_admin
from app.models.legislation import Category, CategoryType, Law, Article, SearchCache, QueryExpansionCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_jinja_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Law KZ MCP Server...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")
    yield
    await engine.dispose()
    logger.info("Server shutting down")


app = FastAPI(
    title="Законодательство РК — MCP Server",
    description="API и MCP сервер для работы с законодательством Республики Казахстан",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

admin = setup_admin(app)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.mcp_server_name}

@app.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    cats_count = await db.scalar(select(func.count(Category.id)))
    laws_count = await db.scalar(select(func.count(Law.id)))
    articles_count = await db.scalar(select(func.count(Article.id)))
    codes_count = await db.scalar(
        select(func.count(Category.id)).where(Category.type == CategoryType.CODEX)
    )
    return {
        "categories": cats_count or 0,
        "laws": laws_count or 0,
        "articles": articles_count or 0,
        "codes": codes_count or 0,
    }


@app.get("/category/{slug}", response_class=HTMLResponse)
async def category_page(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.legislation_service import LegislationService
    service = LegislationService(db)
    cat = await service.get_category_by_slug(slug)
    if not cat:
        return HTMLResponse("Категория не найдена", status_code=404)
    laws_with_articles = []
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.legislation import Law
    for law in cat.laws:
        result = await db.execute(
            select(Law).options(selectinload(Law.articles)).where(Law.id == law.id)
        )
        laws_with_articles.append(result.scalar_one())
    template = _jinja_env.get_template("category.html")
    html = template.render(category=cat, laws=laws_with_articles)
    return HTMLResponse(html)


@app.get("/law/{law_id}", response_class=HTMLResponse)
async def law_page(law_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.legislation_service import LegislationService
    import re
    from urllib.parse import unquote
    service = LegislationService(db)
    law = await service.get_law(law_id)
    if not law:
        return HTMLResponse("Закон не найден", status_code=404)

    q = unquote(request.query_params.get("q", "").strip())
    q_lower = q.lower()

    # Parse sections (Раздел/Глава) from articles
    sections = []
    current_section = None
    section_pattern = re.compile(
        r"^(Раздел\s+[\wIVXLCDM]+\.?\s*(.*))|^(Глава\s+[\d\-]+\.?\s*(.*))$",
        re.MULTILINE,
    )

    blocks = []

    if q:
        q_tokens = [t for t in re.findall(r'[а-яёa-z]+', q_lower) if len(t) > 2]

    for article in sorted(law.articles, key=lambda a: a.sort_order):
        content = article.content or ""
        title = article.title or ""

        found_section = None
        for text in (title, content):
            for m in section_pattern.finditer(text):
                if m.group(1):
                    raw = m.group(1).strip().rstrip(".")
                    sec_title = (m.group(2) or "").strip()
                else:
                    raw = m.group(3).strip().rstrip(".")
                    sec_title = (m.group(4) or "").strip()
                found_section = raw + (f". {sec_title}" if sec_title else "")
                break
            if found_section:
                break

        if found_section:
            current_section = {"name": found_section, "articles": []}
            sections.append(current_section)

        if current_section is None:
            current_section = {"name": "", "articles": []}
            sections.append(current_section)

        current_section["articles"].append(article)

        if q:
            paragraphs = re.split(r'\n\s*\n', content)
            matched_paras = []
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                para_lower = para.lower()
                if q_lower in para_lower or any(t in para_lower for t in q_tokens):
                    if q_lower in para_lower:
                        highlighted = re.sub(
                            f'({re.escape(q)})', r'<mark>\1</mark>', para,
                            flags=re.IGNORECASE
                        )
                    else:
                        highlighted = para
                        for t in q_tokens:
                            if t in para_lower:
                                highlighted = re.sub(
                                    f'({re.escape(t)})', r'<mark>\1</mark>', highlighted,
                                    flags=re.IGNORECASE
                                )
                    matched_paras.append(highlighted)
            if matched_paras or (title and q_lower in title.lower()):
                blocks.append({
                    "article_id": article.id,
                    "article_number": article.number,
                    "article_title": article.title,
                    "section_name": current_section["name"] if current_section else "",
                    "paragraphs": matched_paras,
                })

    if q and not blocks and law.full_text:
        paragraphs = re.split(r'\n\s*\n', law.full_text)
        matched_paras = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            para_lower = para.lower()
            if q_lower in para_lower or any(t in para_lower for t in q_tokens):
                highlighted = re.sub(
                    f'({re.escape(q)})', r'<mark>\1</mark>', para,
                    flags=re.IGNORECASE
                )
                matched_paras.append(highlighted)
        if matched_paras:
            blocks.append({
                "article_id": 0,
                "article_number": "",
                "article_title": "Полный текст закона",
                "section_name": "",
                "paragraphs": matched_paras,
            })

    template = _jinja_env.get_template("law.html")
    html = template.render(law=law, query=q, sections=sections, blocks=blocks)
    return HTMLResponse(html)


@app.get("/laws", response_class=HTMLResponse)
async def laws_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    type: str = "",
):
    from sqlalchemy.orm import selectinload, joinedload

    filters = []

    if type == "codex":
        filters.append(Category.type == CategoryType.CODEX)
        title = "Кодексы Республики Казахстан"
    elif type == "law":
        filters.append(Category.type == CategoryType.LAW)
        title = "Законы Республики Казахстан"
    elif type == "constitution":
        filters.append(Category.type == CategoryType.CONSTITUTION)
        title = "Конституция Республики Казахстан"
    else:
        title = "Все нормативные акты"

    stmt = select(Law).options(joinedload(Law.category), selectinload(Law.articles))
    if filters:
        stmt = stmt.join(Category).where(*filters)
    stmt = stmt.order_by(Law.title)

    result = await db.execute(stmt)
    laws = result.unique().scalars().all()

    for law in laws:
        law.category_name = law.category.name if law.category else ""

    template = _jinja_env.get_template("laws.html")
    html = template.render(laws=laws, title=title, filter_type=type or "")
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    cats_count = await db.scalar(select(func.count(Category.id)))
    laws_count = await db.scalar(select(func.count(Law.id)))
    articles_count = await db.scalar(select(func.count(Article.id)))
    codes_count = await db.scalar(
        select(func.count(Category.id)).where(Category.type == CategoryType.CODEX)
    )

    cats_result = await db.execute(
        select(Category).order_by(Category.sort_order)
    )
    categories = cats_result.scalars().all()

    template = _jinja_env.get_template("index.html")
    html = template.render(
        stats={
            "categories": cats_count or 0,
            "laws": laws_count or 0,
            "articles": articles_count or 0,
            "codes": codes_count or 0,
        },
        categories=categories,
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
