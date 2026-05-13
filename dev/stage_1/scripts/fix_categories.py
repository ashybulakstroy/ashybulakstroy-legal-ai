"""Phase 1: Fix miscategorized laws and create missing categories."""
import asyncio
import logging

from sqlalchemy import select, update

from app.database import engine, Base, async_session_factory
from app.models.legislation import Category, CategoryType, Law

logger = logging.getLogger(__name__)


NEW_CATEGORIES = [
    Category(
        name="Налоговый кодекс",
        slug="nalogovyy-kodeks",
        description="Кодекс Республики Казахстан о налогах и других обязательных платежах в бюджет (Налоговый кодекс)",
        type=CategoryType.CODEX,
        sort_order=13,
    ),
    Category(
        name="Предпринимательский кодекс",
        slug="predprinimatelskiy-kodeks",
        description="Предпринимательский кодекс Республики Казахстан",
        type=CategoryType.CODEX,
        sort_order=14,
    ),
    Category(
        name="Административный процедурно-процессуальный кодекс",
        slug="appk",
        description="Административный процедурно-процессуальный кодекс Республики Казахстан",
        type=CategoryType.CODEX,
        sort_order=15,
    ),
    Category(
        name="Земельный кодекс",
        slug="zemelnyy-kodeks",
        description="Земельный кодекс Республики Казахстан",
        type=CategoryType.CODEX,
        sort_order=16,
    ),
]


async def fix_categories():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        # Get all existing category slugs to avoid duplicates
        result = await session.execute(select(Category.slug))
        existing_slugs = {row[0] for row in result}

        # Create new categories
        created_cats = {}
        for cat in NEW_CATEGORIES:
            if cat.slug in existing_slugs:
                logger.info(f"Category '{cat.name}' already exists, skipping")
                result = await session.execute(
                    select(Category).where(Category.slug == cat.slug)
                )
                created_cats[cat.slug] = result.scalar_one()
            else:
                session.add(cat)
                await session.flush()
                created_cats[cat.slug] = cat
                logger.info(f"Created category: {cat.name} (id={cat.id})")

        # Map law numbers to target category slugs
        # Law number -> target category slug
        moves = {
            "120-VI": "nalogovyy-kodeks",      # Налоговый кодекс (currently in cat 5)
            "231-V": None,                       # УПК -> move to category 8 (УПК)
            "375-V": "predprinimatelskiy-kodeks",# Предпринимательский кодекс (currently in cat 8)
            "377-V": None,                       # ГПК -> move to category 10 (ГПК)
            "226-V": None,                       # УК -> move to category 7 (УК)
            "350-VI": "appk",                    # АППК (currently in cat 11)
        }

        # Get target category IDs for direct swaps
        cats_result = await session.execute(
            select(Category).where(Category.slug.in_([
                "ugolovnyy-kodeks",         # cat 7
                "ugolovno-protsessualnyy-kodeks",  # cat 8
                "ugolovno-ispolnitelnyy-kodeks",   # cat 9
                "grazhdanskiy-protsessualnyy-kodeks", # cat 10
                "sotsialnyy-kodeks",        # cat 11
            ]))
        )
        slug_to_cat = {cat.slug: cat.id for cat in cats_result.scalars()}

        # Map: law.number -> target category_id
        number_to_cat = {
            "231-V": slug_to_cat["ugolovno-protsessualnyy-kodeks"],      # УПК -> cat 8
            "377-V": slug_to_cat["grazhdanskiy-protsessualnyy-kodeks"],  # ГПК -> cat 10
            "226-V": slug_to_cat["ugolovnyy-kodeks"],                    # УК -> cat 7
        }

        # Process moves
        laws_result = await session.execute(
            select(Law).where(Law.number.in_(list(moves.keys())))
        )
        for law in laws_result.scalars():
            target_slug = moves.get(law.number)
            if target_slug:
                # Move to a new category
                target_cat = created_cats[target_slug]
                old_cat_id = law.category_id
                law.category_id = target_cat.id
                logger.info(
                    f"Moved '{law.title[:50]}...' (number={law.number}) "
                    f"from cat {old_cat_id} to cat {target_cat.id} ({target_cat.name})"
                )
            elif law.number in number_to_cat:
                # Move to an existing category (swap)
                target_cat_id = number_to_cat[law.number]
                old_cat_id = law.category_id
                law.category_id = target_cat_id
                logger.info(
                    f"Moved '{law.title[:50]}...' (number={law.number}) "
                    f"from cat {old_cat_id} to cat {target_cat_id}"
                )

        await session.commit()
        logger.info("Categories fixed successfully!")

        # Show final state
        print("\n=== Final category state ===")
        result = await session.execute(
            select(Category).order_by(Category.sort_order)
        )
        for cat in result.scalars():
            count_result = await session.execute(
                select(Law).where(Law.category_id == cat.id)
            )
            laws_list = count_result.scalars().all()
            print(f"  [{cat.id}] {cat.name} ({cat.slug}) - {len(laws_list)} laws")
            for law in laws_list:
                print(f"    - {law.title[:60]} ({law.number})")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    await fix_categories()


if __name__ == "__main__":
    asyncio.run(main())
