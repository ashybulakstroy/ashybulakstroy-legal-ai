from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.session import get_db
from app.models.case import Case

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("")
async def get_stats(db: AsyncSession = Depends(get_db)):
    total_result = await db.execute(select(func.count(Case.id)))
    total_processed = total_result.scalar() or 0

    consultations_result = await db.execute(
        select(func.count(Case.id)).where(Case.status == "closed")
    )
    consultations = consultations_result.scalar() or 0

    searches_result = await db.execute(
        select(func.count(Case.id)).where(Case.status == "in_progress")
    )
    searches = searches_result.scalar() or 0

    return {
        "total_processed": total_processed,
        "consultations": consultations,
        "searches": searches,
        "detail": [
            {"label": "Всего обработано", "value": total_processed, "key": "total"},
            {"label": "Консультаций", "value": consultations, "key": "consultations"},
            {"label": "Поисков норм", "value": searches, "key": "searches"},
        ],
    }
