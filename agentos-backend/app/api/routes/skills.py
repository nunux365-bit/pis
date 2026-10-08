"""Skill registry — categories and skills from database (seeded at bootstrap)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_roles
from app.db.models import CatalogSkill, CatalogSkillCategory, User, UserRole
from app.db.session import get_db

router = APIRouter()

_skills_access = require_roles(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)


@router.get("/")
async def list_skills(
    user: Annotated[User, Depends(_skills_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _ = user
    r = await db.execute(
        select(CatalogSkillCategory)
        .options(selectinload(CatalogSkillCategory.skills))
        .order_by(CatalogSkillCategory.sort_order, CatalogSkillCategory.name)
    )
    cats = r.scalars().unique().all()
    categories = []
    for c in cats:
        skills = sorted(c.skills, key=lambda s: (s.sort_order, s.title))
        categories.append(
            {
                "name": c.name,
                "count": len(skills),
                "skills": [
                    {"id": s.slug, "title": s.title, "version": s.version}
                    for s in skills
                ],
            }
        )
    return {"categories": categories, "source": "database"}


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    user: Annotated[User, Depends(_skills_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _ = user
    r = await db.execute(
        select(CatalogSkill)
        .options(selectinload(CatalogSkill.category))
        .where(CatalogSkill.slug == skill_id)
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return {
        "skill_id": skill_id,
        "title": s.title,
        "version": s.version,
        "category": s.category.name,
        "owner_agents": [],
        "source": "database",
    }
