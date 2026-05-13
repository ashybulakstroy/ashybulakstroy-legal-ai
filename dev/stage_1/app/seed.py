import asyncio
import logging

from sqlalchemy import select

from app.database import engine, Base, async_session_factory
from app.models.legislation import Category, CategoryType, Law, LawStatus, Article
from app.parsers.adilet_parser import AdiletParser

logger = logging.getLogger(__name__)

# Mapping of adilet doc IDs to categories
CATEGORY_MAP = [
    {
        "name": "Конституция",
        "slug": "konstitutsiya",
        "description": "Конституция Республики Казахстан — основной закон страны",
        "type": CategoryType.CONSTITUTION,
        "sort_order": 1,
        "doc_ids": ["K950001000_"],
    },
    {
        "name": "Гражданский кодекс (Общая часть)",
        "slug": "grazhdanskiy-kodeks-obshchaya",
        "description": "Гражданский кодекс Республики Казахстан (Общая часть)",
        "type": CategoryType.CODEX,
        "sort_order": 2,
        "doc_ids": ["K940001000_"],
    },
    {
        "name": "Гражданский кодекс (Особенная часть)",
        "slug": "grazhdanskiy-kodeks-osobennaya",
        "description": "Гражданский кодекс Республики Казахстан (Особенная часть)",
        "type": CategoryType.CODEX,
        "sort_order": 3,
        "doc_ids": ["K990000409_"],
    },
    {
        "name": "Трудовой кодекс",
        "slug": "trudovoy-kodeks",
        "description": "Трудовой кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 4,
        "doc_ids": ["K1500000414"],
    },
    {
        "name": "Кодекс о браке и семье",
        "slug": "kodeks-o-brake-i-seme",
        "description": "Кодекс Республики Казахстан о браке (супружестве) и семье",
        "type": CategoryType.CODEX,
        "sort_order": 5,
        "doc_ids": [],
    },
    {
        "name": "Кодекс об административных правонарушениях",
        "slug": "koap",
        "description": "Кодекс Республики Казахстан об административных правонарушениях",
        "type": CategoryType.CODEX,
        "sort_order": 6,
        "doc_ids": ["K1400000235"],
    },
    {
        "name": "Уголовный кодекс",
        "slug": "ugolovnyy-kodeks",
        "description": "Уголовный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 7,
        "doc_ids": ["K1400000226"],
    },
    {
        "name": "Уголовно-процессуальный кодекс",
        "slug": "ugolovno-protsessualnyy-kodeks",
        "description": "Уголовно-процессуальный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 8,
        "doc_ids": ["K1400000231"],
    },
    {
        "name": "Уголовно-исполнительный кодекс",
        "slug": "ugolovno-ispolnitelnyy-kodeks",
        "description": "Уголовно-исполнительный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 9,
        "doc_ids": [],
    },
    {
        "name": "Гражданский процессуальный кодекс",
        "slug": "grazhdanskiy-protsessualnyy-kodeks",
        "description": "Гражданский процессуальный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 10,
        "doc_ids": ["K1500000377"],
    },
    {
        "name": "Социальный кодекс",
        "slug": "sotsialnyy-kodeks",
        "description": "Социальный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 11,
        "doc_ids": [],
    },
    {
        "name": "Налоговый кодекс",
        "slug": "nalogovyy-kodeks",
        "description": "Кодекс Республики Казахстан о налогах и других обязательных платежах в бюджет (Налоговый кодекс)",
        "type": CategoryType.CODEX,
        "sort_order": 13,
        "doc_ids": ["K1700000120"],
    },
    {
        "name": "Предпринимательский кодекс",
        "slug": "predprinimatelskiy-kodeks",
        "description": "Предпринимательский кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 14,
        "doc_ids": ["K1500000375"],
    },
    {
        "name": "Административный процедурно-процессуальный кодекс",
        "slug": "appk",
        "description": "Административный процедурно-процессуальный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 15,
        "doc_ids": ["K2000000350"],
    },
    {
        "name": "Земельный кодекс",
        "slug": "zemelnyy-kodeks",
        "description": "Земельный кодекс Республики Казахстан",
        "type": CategoryType.CODEX,
        "sort_order": 16,
        "doc_ids": [],
    },
    {
        "name": "Законы РК",
        "slug": "zakony-rk",
        "description": "Законы Республики Казахстан",
        "type": CategoryType.LAW,
        "sort_order": 12,
        "doc_ids": [],
    },
]


def _scrape_all_docs() -> dict[str, dict]:
    """Scrape all documents from adilet.zan.kz (runs in thread pool)."""
    parser = AdiletParser()
    try:
        results = {}
        for cat_info in CATEGORY_MAP:
            for doc_id in cat_info["doc_ids"]:
                if doc_id in results:
                    continue
                url = f"https://adilet.zan.kz/rus/docs/{doc_id}"
                logger.info(f"Scraping {doc_id}...")
                doc = parser.parse_document_page(url)
                if doc:
                    results[doc_id] = doc
                    logger.info(f"  OK: {doc['title'][:60]}, {len(doc['articles'])} articles")
                else:
                    logger.warning(f"  FAILED: {doc_id}")
        return results
    finally:
        parser.close()


async def seed(scrape_from_internet: bool = True):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        existing = await session.get(Category, 1)
        if existing:
            logger.info("Database already seeded, skipping")
            return

        # Create categories first
        category_objects = {}
        for cat_info in CATEGORY_MAP:
            cat = Category(
                name=cat_info["name"],
                slug=cat_info["slug"],
                description=cat_info["description"],
                type=cat_info["type"],
                sort_order=cat_info["sort_order"],
            )
            session.add(cat)
            category_objects[cat_info["slug"]] = cat

        await session.flush()

        if scrape_from_internet:
            logger.info("Scraping documents from adilet.zan.kz...")
            docs = await asyncio.to_thread(_scrape_all_docs)

            for cat_info in CATEGORY_MAP:
                cat = category_objects[cat_info["slug"]]
                for doc_id in cat_info["doc_ids"]:
                    doc_data = docs.get(doc_id)
                    if not doc_data:
                        continue

                    law = Law(
                        category=cat,
                        title=doc_data["title"],
                        number=doc_data["number"],
                        date_adopted=doc_data["date_adopted"],
                        status=doc_data["status"],
                        summary=doc_data["summary"],
                        full_text=doc_data["full_text"],
                    )
                    session.add(law)
                    await session.flush()

                    for art_data in doc_data["articles"]:
                        article = Article(
                            law=law,
                            number=art_data["number"],
                            title=art_data["title"],
                            content=art_data["content"],
                            sort_order=art_data["sort_order"],
                        )
                        session.add(article)

                    logger.info(f"  Saved: {doc_data['title'][:60]} ({len(doc_data['articles'])} articles)")
        else:
            logger.info("No laws scraped (scrape_from_internet=False)")

        await session.commit()
        logger.info("Database seeded successfully!")


async def drop_all():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("All tables dropped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(seed(scrape_from_internet=True))
