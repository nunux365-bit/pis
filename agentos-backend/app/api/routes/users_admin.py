"""Admin user provisioning."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import User, UserRole
from app.db.session import get_db
from app.schemas.auth import UserPublic
from app.security.passwords import hash_password
from app.security.rbac import VALID_USER_ROLES
from app.services.audit import write_audit

router = APIRouter()


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=10, max_length=128)
    full_name: str = Field(..., max_length=200)
    department: str = Field(default="General", max_length=120)
    roles: list[str] = Field(
        default_factory=lambda: [UserRole.EMPLOYEE.value],
        min_length=1,
        max_length=8,
    )

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: list[str]) -> list[str]:
        stripped = [x.strip().lower() for x in v if isinstance(x, str) and x.strip()]
        if not stripped:
            raise ValueError("At least one role is required")
        if len(stripped) != len(set(stripped)):
            raise ValueError("Duplicate roles")
        for r in stripped:
            if r not in VALID_USER_ROLES:
                raise ValueError(f"Invalid role: {r}")
        return stripped


@router.post("", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: AdminUserCreate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    u = User(
        email=body.email.lower().strip(),
        hashed_password=hash_password(body.password),
        full_name=body.full_name.strip(),
        department=body.department.strip() or "General",
        roles=body.roles,
    )
    db.add(u)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email exists") from None

    await write_audit(
        db,
        actor_user_id=admin.id,
        action="admin.user_create",
        resource_type="user",
        resource_id=str(u.id),
    )
    return u


@router.get("", response_model=list[UserPublic])
async def list_users(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(r.scalars().all())


@router.patch("/{user_id}/deactivate", response_model=UserPublic)
async def deactivate_user(
    user_id: UUID,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot deactivate self")
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    u.is_active = False
    await write_audit(
        db,
        actor_user_id=admin.id,
        action="admin.user_deactivate",
        resource_type="user",
        resource_id=str(u.id),
    )
    return u
