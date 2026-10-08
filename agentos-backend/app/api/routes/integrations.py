"""Integration health — list/patch aligned with nav (dept heads + admins); write admin-only."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin, require_roles
from app.db.models import IntegrationHealth, User, UserRole
from app.db.session import get_db

router = APIRouter()

_integrations_access = require_roles(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)


class IntegrationOut(BaseModel):
    name: str
    type: str
    status: str
    health: float
    modules: str | None
    last_sync: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, r: IntegrationHealth, *, include_diagnostics: bool) -> "IntegrationOut":
        return cls(
            name=r.name,
            type=r.system_type,
            status=r.status,
            health=float(r.health_pct),
            modules=r.modules,
            last_sync=r.last_sync_at.isoformat() if r.last_sync_at else None,
            last_error=r.last_error if include_diagnostics else None,
        )


class IntegrationPatch(BaseModel):
    status: str | None = Field(None, max_length=32)
    health_pct: float | None = Field(None, ge=0, le=100)
    last_error: str | None = None


def _can_see_integration_diagnostics(user: User) -> bool:
    """Errors / internal module strings can leak infra details — admins only."""
    return user.has_any_role(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)


@router.get("", response_model=dict)
async def list_integrations(
    user: Annotated[User, Depends(_integrations_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(IntegrationHealth).order_by(IntegrationHealth.name))
    rows = result.scalars().all()
    diag = _can_see_integration_diagnostics(user)
    return {
        "integrations": [
            IntegrationOut.from_row(r, include_diagnostics=diag).model_dump() for r in rows
        ],
    }


@router.patch("/{name}", response_model=dict)
async def patch_integration(
    name: str,
    body: IntegrationPatch,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(IntegrationHealth).where(IntegrationHealth.name == name))
    row = r.scalar_one_or_none()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown integration")
    if body.status is not None:
        row.status = body.status
    if body.health_pct is not None:
        row.health_pct = Decimal(str(body.health_pct))
    if body.last_error is not None:
        row.last_error = body.last_error
    row.last_sync_at = datetime.now(UTC)
    return IntegrationOut.from_row(row, include_diagnostics=True).model_dump()
