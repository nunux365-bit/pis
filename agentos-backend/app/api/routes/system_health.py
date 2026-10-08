"""Extended health — DB + Redis + kernel counters (no auth)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.kernel.health import readiness_snapshot

router = APIRouter()


@router.get("/ready")
async def health_ready(db: Annotated[AsyncSession, Depends(get_db)]):
    return await readiness_snapshot(db)
