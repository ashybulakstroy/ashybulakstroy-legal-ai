"""Load laws from adilet.zan.kz into the database and fill metadata gaps."""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select

from app.database import engine, Base, async_session_factory
from app.models.legislation import Category, Law, Article, LawStatus
from app.parsers.adilet_parser import AdiletParser

logger = logging.getLogger(__name__)

# All known adilet doc IDs → category mapping
# Newly discovered IDs fill metadata (number, date, full_text) for kodeksy-sourced laws
ADILET_LAWS = [
    # --- Already loaded (original) ---
    {"doc_id": "Z980000220_", "category_slug": "zakony-rk"},        # О товариществах
    {"doc_id": "Z1500000434", "category_slug": "zakony-rk"},        # О госзакупках
    {"doc_id": "Z2200000166", "category_slug": "zakony-rk"},        # О геодезии

    # --- Newly discovered: replace kodeksy data with official adilet data ---
    {"doc_id": "K080000095_", "category_slug": "zakony-rk"},        # Бюджетный кодекс РК
    {"doc_id": "K030000442_", "category_slug": "zemelnyy-kodeks"},  # Земельный кодекс РК
    {"doc_id": "Z100000274_", "category_slug": "zakony-rk"},        # О защите прав потребителей
    {"doc_id": "Z080000112_", "category_slug": "zakony-rk"},        # О конкуренции (repealed)
    {"doc_id": "Z070000234_", "category_slug": "zakony-rk"},        # О бухгалтерском учете
    {"doc_id": "Z070000310_", "category_slug": "zakony-rk"},        # О госрегистрации прав на недвижимость
    {"doc_id": "Z100000261_", "category_slug": "zakony-rk"},        # Об исполнительном производстве
]

# Laws still only available from kodeksy-kz.com (slug → category_slug)
KODEKSY_LAWS = [
    # Codes
    "ugolovno-ispolnitelnyj_kodeks",    # УИК РК
    "sotsialnyj_kodeks_rk",              # Социальный кодекс РК
    "o_brake_i_seme",                     # Кодекс о браке и семье
    # Repealed
    "o_tamozhennom_dele",                # О таможенном деле
    "o_poryadke_rassmotreniya_obrawenij", # О порядке рассмотрения обращений
    "ob_administrativnyh_protsedurah",    # Об административных процедурах
    # Active laws
    "o_mediatsii",                        # О медиации
    "o_tovarnyh_znakah",                  # О товарных знаках
    "ob_advokatskoj_deyatelnosti_i_yuridicheskoj_pomowi",  # Об адвокатской деятельности
    "ob_operativno-rozysknoj_deyatelnosti", # Об оперативно-розыскной деятельности
    "o_gosudarstvennyh_uslugah",          # О государственных услугах
    "ob_organah_vnutrennih_del",          # Об органах внутренних дел
    "ob_ohrannoj_deyatel_nosti",          # Об охранной деятельности
    # Already migrated to adilet (kept to avoid re-import):
    # o_buhgalterskom_uchete, zemelnyj_kodeks, byudzhetnyj_kodeks,
    # o_zawite_prav_potrebitelej, o_konkurentsii,
    # ob_ispolnitelnom_proizvodstve, o_registratsii_prav_na_nedvizhimost
]

KODEKSY_CATEGORY_OVERRIDE = {
    "o_brake_i_seme": "kodeks-o-brake-i-seme",
    "ugolovno-ispolnitelnyj_kodeks": "ugolovno-ispolnitelnyy-kodeks",
    "sotsialnyj_kodeks_rk": "sotsialnyy-kodeks",
}


async def get_category(slug: str) -> Optional[Category]:
    async with async_session_factory() as session:
        return await session.scalar(
            select(Category).where(Category.slug == slug)
        )


async def load_adilet_law(
    doc_id: str, category: Category, parser: AdiletParser
) -> Optional[Law]:
    """Load a law from adilet. If it already exists (matched by title prefix),
    update its metadata and replace articles instead of skipping."""
    url = f"https://adilet.zan.kz/rus/docs/{doc_id}"
    doc_data = await asyncio.to_thread(parser.parse_document_page, url)
    if not doc_data:
        logger.warning(f"Failed to parse {doc_id}")
        return None

    adilet_title = doc_data["title"]
    title_prefix = adilet_title[:50].lower()

    async with async_session_factory() as session:
        # Try to find existing law by title prefix (handles кodeksy vs adilet title differences)
        result = await session.execute(
            select(Law).where(Law.title.ilike(f"{title_prefix}%"))
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update metadata for existing kodeksy-sourced law
            old_title = existing.title[:60]
            existing.title = adilet_title
            existing.number = doc_data["number"]
            existing.date_adopted = doc_data["date_adopted"]
            existing.status = doc_data["status"]
            existing.full_text = doc_data["full_text"]
            existing.summary = doc_data["summary"]
            existing.category_id = category.id

            # Replace articles: delete old, add new
            existing.articles.clear()
            await session.flush()

            for art_data in doc_data["articles"]:
                existing.articles.append(Article(
                    law_id=existing.id,
                    number=art_data["number"],
                    title=art_data["title"],
                    content=art_data["content"],
                    sort_order=art_data["sort_order"],
                ))

            await session.commit()
            logger.info(f"Updated (adilet): {old_title} → №{doc_data['number'] or '-'} ({len(doc_data['articles'])} st)")
            return existing
        else:
            # New law
            law = Law(
                category_id=category.id,
                title=adilet_title,
                number=doc_data["number"],
                date_adopted=doc_data["date_adopted"],
                status=doc_data["status"],
                summary=doc_data["summary"],
                full_text=doc_data["full_text"],
            )
            session.add(law)
            await session.flush()

            for art_data in doc_data["articles"]:
                session.add(Article(
                    law_id=law.id,
                    number=art_data["number"],
                    title=art_data["title"],
                    content=art_data["content"],
                    sort_order=art_data["sort_order"],
                ))

            await session.commit()
            logger.info(f"Loaded (adilet): {adilet_title[:60]} ({len(doc_data['articles'])} st)")
            return law


async def load_laws():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Phase A: Load/update from adilet
    logger.info("=== Loading/updating from adilet.zan.kz ===")
    adilet_parser = AdiletParser()
    try:
        for law_info in ADILET_LAWS:
            category = await get_category(law_info["category_slug"])
            if category:
                await load_adilet_law(law_info["doc_id"], category, adilet_parser)
    finally:
        adilet_parser.close()

    # Phase B: Load remaining from kodeksy-kz.com (for laws not on adilet)
    if KODEKSY_LAWS:
        from app.parsers.kodeksy_parser import KodeksyParser
        logger.info("=== Loading remaining from kodeksy-kz.com ===")
        kodeksy_parser = KodeksyParser()
        try:
            for slug in KODEKSY_LAWS:
                cat_slug = KODEKSY_CATEGORY_OVERRIDE.get(slug, "zakony-rk")
                category = await get_category(cat_slug)
                if not category:
                    logger.warning(f"Category '{cat_slug}' not found, skipping {slug}")
                    continue
                # Check if law already exists (from adilet update)
                async with async_session_factory() as session:
                    existing = await session.execute(
                        select(Law).where(Law.category_id == category.id)
                    )
                    # Simple title-based check per law
                    slug_guess = slug.replace("_", " ").replace("-", " ")
                    already_loaded = False
                    for law in existing.scalars():
                        if slug_guess[:20].lower() in (law.title or "").lower():
                            already_loaded = True
                            break
                    if already_loaded:
                        logger.info(f"Already loaded (from adilet), skipping kodeksy: {slug}")
                        continue

                doc_data = await kodeksy_parser.get_full_law(slug)
                if not doc_data:
                    logger.warning(f"Failed to parse kodeksy law: {slug}")
                    continue

                async with async_session_factory() as session:
                    existing = await session.execute(
                        select(Law).where(Law.title == doc_data["title"])
                    )
                    if existing.scalar_one_or_none():
                        logger.info(f"Already exists, skipping: {doc_data['title'][:60]}")
                        continue

                    law = Law(
                        category_id=category.id,
                        title=doc_data["title"],
                        number=doc_data["number"],
                        status=doc_data["status"],
                        summary=doc_data["summary"],
                    )
                    session.add(law)
                    await session.flush()

                    for art_data in doc_data["articles"]:
                        session.add(Article(
                            law_id=law.id,
                            number=art_data["number"],
                            title=art_data["title"],
                            content=art_data["content"],
                            sort_order=art_data["sort_order"],
                        ))

                    await session.commit()
                    logger.info(f"Loaded (kodeksy): {doc_data['title'][:60]} ({len(doc_data['articles'])} st)")
        finally:
            await kodeksy_parser.close()

    # Show final state
    print("\n=== Final DB state ===")
    async with async_session_factory() as session:
        result = await session.execute(
            select(Category).order_by(Category.sort_order)
        )
        for cat in result.scalars():
            law_rows = await session.execute(
                select(Law).where(Law.category_id == cat.id)
            )
            law_list = law_rows.scalars().all()
            print(f"  [{cat.id}] {cat.name} ({cat.slug}): {len(law_list)} law(s)")
            for law in law_list:
                art_count = (await session.execute(
                    select(Article).where(Article.law_id == law.id)
                )).scalars().all()
                has_meta = "✓" if (law.number and law.date_adopted) else "✗"
                print(f"    [{has_meta}] {law.title[:60]} (№{law.number or '-'}) - {len(art_count)} st, status={law.status.value}")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    await load_laws()


if __name__ == "__main__":
    asyncio.run(main())
