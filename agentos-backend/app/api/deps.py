"""FastAPI dependencies — DB session, current user, RBAC."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserRole
from app.db.session import get_db
from app.config.settings import settings
from app.infra.rate_limit import check_rate_limit
from app.security.tokens import verify_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    # Prefer HttpOnly cookie (browser) over Authorization header so stale client tokens
    # do not override a fresh server-issued session.
    token = request.cookies.get("access_token")
    if not token and creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = verify_access_token(token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from None
    result = await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not await check_rate_limit(
        f"api:{user.id}",
        settings.rate_limit_api_per_user,
        60,
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="API rate limit exceeded",
        )
    return user


async def get_current_user_detached(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Authenticate without holding ``get_db`` for the rest of the request.

    Use on handlers that must release the pool before slow I/O (chat LLM).
    """
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        user = await get_current_user(request, creds, db)
        await db.commit()
        db.expunge(user)
        return user


def require_roles(*allowed: UserRole | str):
    allowed_values = frozenset(
        a.value if isinstance(a, UserRole) else str(a) for a in allowed
    )

    async def _inner(user: Annotated[User, Depends(get_current_user)]) -> User:
        # system_admin (is_admin) bypasses non-payroll roles, but is strictly gated on payroll-specific roles
        is_payroll_endpoint = any(role in {"maker", "hrbp", "hod", "payroll", "payroll_admin"} for role in allowed_values)
        if user.is_admin and not is_payroll_endpoint:
            return user
        if user.role_set & {"payroll_admin", "payroll"}:
            return user
        if not user.role_set:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid role")
        if user.role_set & allowed_values:
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    return _inner


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


# system_admin passes via ``require_roles`` (special-case).
require_compliance_dashboard_access = require_roles(UserRole.GLP_COMPLIANCE_REVIEWER)
require_responder_eval_dashboard_access = require_roles(UserRole.RESPONDER_EVAL_REVIEWER)


def same_user_or_admin(actor: User, target_user_id: UUID) -> bool:
    if actor.is_admin:
        return True
    return actor.id == target_user_id


def can_read_department(actor: User, department: str) -> bool:
    if actor.is_admin:
        return True
    return actor.has_role(UserRole.DEPT_HEAD) and actor.department == department
