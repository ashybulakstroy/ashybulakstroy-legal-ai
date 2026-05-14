from fastapi import APIRouter

from app.api.v1.endpoints.stats import router as stats_router

router = APIRouter(prefix="/v1")
router.include_router(stats_router)
