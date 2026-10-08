"""Agent registry — rows from `catalog_agents` (seeded at bootstrap, editable via DB)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.db.models import CatalogAgent, User, UserRole
from app.db.session import get_db

router = APIRouter()

_agents_access = require_roles(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-")


@router.get("/")
async def list_agents(
    user: Annotated[User, Depends(_agents_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _ = user
    r = await db.execute(
        select(CatalogAgent).order_by(CatalogAgent.sort_order, CatalogAgent.name)
    )
    rows = r.scalars().all()
    return {
        "agents": [
            {
                "name": a.name,
                "status": a.status,
                "ring": a.ring,
                "description": a.description or "",
                "tasks_today": a.tasks_today,
                "uptime_pct": float(a.uptime_pct) if a.uptime_pct is not None else None,
            }
            for a in rows
        ],
        "source": "database",
    }


@router.get("/{agent_name}/health")
async def agent_health(
    agent_name: str,
    user: Annotated[User, Depends(_agents_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _ = user
    target = _slugify(agent_name)
    r = await db.execute(select(CatalogAgent))
    for a in r.scalars().all():
        if a.slug == target or _slugify(a.name) == target:
            up = float(a.uptime_pct) if a.uptime_pct is not None else None
            return {
                "agent": a.name,
                "status": "healthy" if a.status == "active" else "idle",
                "uptime": f"{up}%" if up is not None else "—",
                "source": "database",
            }
    return {"agent": agent_name, "status": "unknown", "uptime": "—", "source": "database"}
