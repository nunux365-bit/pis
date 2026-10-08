"""Authentication — JWT access + refresh, bcrypt passwords, rate limiting."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config.settings import settings
from app.db.models import RefreshToken, User, UserRole
from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserPublic,
)
from app.security.passwords import hash_password, verify_password
from app.security.auth_cookies import attach_auth_cookies, clear_auth_cookies
from app.security.tokens import (
    create_access_token,
    create_google_sso_state,
    hash_refresh_token,
    new_refresh_token,
    verify_google_sso_state,
)
from app.services.audit import write_audit
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token
from app.infra.rate_limit import check_rate_limit

router = APIRouter()


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


@router.post("/register", response_model=UserPublic)
async def register(
    body: RegisterRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not settings.allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is disabled")
    email = body.email.lower().strip()
    u = User(
        email=email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name.strip(),
        department=body.department.strip() or "General",
        roles=[UserRole.EMPLOYEE.value],
    )
    db.add(u)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    await write_audit(
        db,
        actor_user_id=u.id,
        action="user.register",
        resource_type="user",
        resource_id=str(u.id),
        ip_address=_client_ip(request),
    )
    return u


async def sync_user_payroll_roles(db: AsyncSession, user: User) -> bool:
    """Dynamically resolve and sync a user's assigned roles from PayrollWorkflowConfig & PayrollRoutingMatrix.
    
    Ensures that whenever any user (payroll_admin, maker, hrbp, hod, etc.) authenticates,
    their user record in the database is automatically synced with all assigned roles.
    """
    if not user or not user.email:
        return False

    email = user.email.lower().strip()
    assigned_roles: set[str] = set()

    # 1. Check if configured as admin / bootstrap admin
    bootstrap_admin = (settings.bootstrap_admin_email or "").strip().lower()
    if email == "nandini.aggarwal@1mg.com" or (bootstrap_admin and email == bootstrap_admin):
        assigned_roles.update(["payroll_admin", "system_admin", "payroll", "maker", "hrbp", "hod", "employee"])

    try:
        from app.db.models import PayrollWorkflowConfig, PayrollRoutingMatrix

        # 2. Check active config roster users list
        config_entry = (await db.execute(
            select(PayrollWorkflowConfig).filter(PayrollWorkflowConfig.key == "active_config")
        )).scalars().first()

        if config_entry and config_entry.config:
            users_list = config_entry.config.get("users", [])
            config_user = next((u for u in users_list if u.get("email", "").strip().lower() == email), None)
            if config_user and config_user.get("role"):
                r = config_user["role"].strip().lower()
                if r:
                    assigned_roles.add(r)

        # 3. Check routing matrix rules
        matrix_result = await db.execute(select(PayrollRoutingMatrix))
        matrix_rules = matrix_result.scalars().all()
        for rule in matrix_rules:
            initiators = [e.strip().lower() for e in (rule.initiators or []) if e]
            hrbps = [e.strip().lower() for e in (rule.hrbps or []) if e and e.strip().upper() != "NA"]
            approvers = [e.strip().lower() for e in (rule.approvers or []) if e]

            if email in initiators:
                assigned_roles.add("maker")
            if email in hrbps:
                assigned_roles.add("hrbp")
            if email in approvers:
                assigned_roles.add("hod")
    except Exception:
        pass

    payroll_roles_set = {"payroll_admin", "payroll", "maker", "hrbp", "hod"}
    current_roles = list(user.roles or [])
    non_payroll_roles = [r for r in current_roles if r not in payroll_roles_set]
    
    # Priority ordering for roles (admins first, then specific workflow roles, then employee)
    priority_order = ["payroll_admin", "system_admin", "payroll", "maker", "hrbp", "hod", "employee"]
    
    sorted_roles = []
    for p in priority_order:
        if p in assigned_roles or p in non_payroll_roles:
            sorted_roles.append(p)
    for r in non_payroll_roles:
        if r not in sorted_roles:
            sorted_roles.append(r)

    if not sorted_roles:
        sorted_roles = [UserRole.EMPLOYEE.value]

    if sorted_roles != list(user.roles or []):
        user.roles = sorted_roles
        db.add(user)
        await db.flush()
        return True

    return False


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    ip = _client_ip(request)
    if not await check_rate_limit(
        f"login:{ip}", settings.rate_limit_login_per_ip, 60
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")

    email = body.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.hashed_password):
        await write_audit(
            db,
            actor_user_id=None,
            action="auth.login_failed",
            resource_type="user",
            resource_id=email,
            details={"ip": ip},
            ip_address=ip,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    await sync_user_payroll_roles(db, user)

    raw_refresh = new_refresh_token()
    rt = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(rt)
    user.last_login_at = datetime.now(UTC)
    access = create_access_token(str(user.id), {"role": user.primary_role})
    await write_audit(
        db,
        actor_user_id=user.id,
        action="auth.login",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=ip,
    )
    await db.commit()
    payload = TokenResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    resp = JSONResponse(content=payload.model_dump())
    attach_auth_cookies(resp, access_token=access, refresh_token=raw_refresh)
    return resp


async def _read_refresh_token(request: Request) -> str | None:
    rt = request.cookies.get("refresh_token")
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            data = await request.json()
            if isinstance(data, dict) and isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
                rt = data["refresh_token"]
        except Exception:
            pass
    return rt


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rt_raw = await _read_refresh_token(request)
    if not rt_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing refresh token")
    th = hash_refresh_token(rt_raw)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == th,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user_result = await db.execute(select(User).where(User.id == row.user_id, User.is_active.is_(True)))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")

    row.revoked_at = datetime.now(UTC)
    raw_refresh = new_refresh_token()
    new_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(new_row)
    access = create_access_token(str(user.id), {"role": user.primary_role})
    payload = TokenResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    resp = JSONResponse(content=payload.model_dump())
    attach_auth_cookies(resp, access_token=access, refresh_token=raw_refresh)
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rt_raw = await _read_refresh_token(request)
    if rt_raw:
        th = hash_refresh_token(rt_raw)
        await db.execute(delete(RefreshToken).where(RefreshToken.token_hash == th))
    resp = JSONResponse(content={"ok": True})
    clear_auth_cookies(resp)
    return resp


@router.get("/me", response_model=UserPublic)
async def me(user: Annotated[User, Depends(get_current_user)]):
    return user


def _google_sso_configured() -> bool:
    return bool(settings.google_sso_client_id.strip() and settings.google_sso_client_secret.strip())


def _google_sso_redirect_uri() -> str:
    if settings.google_sso_redirect_uri.strip():
        return settings.google_sso_redirect_uri.strip()
    return f"{settings.api_public_base_url.rstrip('/')}/api/auth/google/callback"


def _frontend_base() -> str:
    return settings.frontend_public_base_url.rstrip("/")


def _sanitize_next_param(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return "/chat"
    s = raw.strip()
    if not s.startswith("/") or s.startswith("//") or "://" in s:
        return "/chat"
    return s[:512]


def _email_domain_allowed(email: str) -> bool:
    raw = (settings.google_sso_allowed_email_domains or "").strip()
    if not raw:
        return True
    dom = email.split("@")[-1].lower()
    allowed = [x.strip().lower() for x in raw.split(",") if x.strip()]
    return dom in allowed


@router.get("/google/enabled")
async def google_sso_enabled():
    return {"enabled": _google_sso_configured()}


@router.get("/google/start")
async def google_sso_start(request: Request, next: str = "/chat"):
    if not _google_sso_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google SSO is not configured")
    next_path = _sanitize_next_param(next)
    state = create_google_sso_state(next_path)
    params = {
        "client_id": settings.google_sso_client_id.strip(),
        "redirect_uri": _google_sso_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url=url, status_code=302)


@router.get("/google/callback")
async def google_sso_callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    front = _frontend_base()
    if error:
        return RedirectResponse(
            url=f"{front}/login?error={quote(str(error), safe='')}",
            status_code=302,
        )
    if not code or not state:
        return RedirectResponse(url=f"{front}/login?error=missing_code", status_code=302)
    if not _google_sso_configured():
        return RedirectResponse(url=f"{front}/login?error=sso_not_configured", status_code=302)
    try:
        next_path = verify_google_sso_state(state)
    except ValueError:
        return RedirectResponse(url=f"{front}/login?error=invalid_state", status_code=302)

    ip = _client_ip(request)
    token_uri = "https://oauth2.googleapis.com/token"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            tr = await client.post(
                token_uri,
                data={
                    "code": code,
                    "client_id": settings.google_sso_client_id.strip(),
                    "client_secret": settings.google_sso_client_secret.strip(),
                    "redirect_uri": _google_sso_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError:
        return RedirectResponse(url=f"{front}/login?error=token_exchange_failed", status_code=302)
    if not tr.is_success:
        return RedirectResponse(url=f"{front}/login?error=token_exchange_denied", status_code=302)
    try:
        body = tr.json()
    except Exception:
        return RedirectResponse(url=f"{front}/login?error=bad_token_response", status_code=302)
    id_jwt = body.get("id_token")
    if not isinstance(id_jwt, str) or not id_jwt:
        return RedirectResponse(url=f"{front}/login?error=no_id_token", status_code=302)
    try:
        idinfo = google_id_token.verify_oauth2_token(
            id_jwt,
            google_auth_requests.Request(),
            settings.google_sso_client_id.strip(),
        )
    except Exception:
        return RedirectResponse(url=f"{front}/login?error=invalid_id_token", status_code=302)
    email_raw = idinfo.get("email")
    if not isinstance(email_raw, str) or "@" not in email_raw:
        return RedirectResponse(url=f"{front}/login?error=no_email", status_code=302)
    if idinfo.get("email_verified") is False:
        return RedirectResponse(url=f"{front}/login?error=email_not_verified", status_code=302)
    email = email_raw.lower().strip()
    if not _email_domain_allowed(email):
        return RedirectResponse(url=f"{front}/login?error=domain_not_allowed", status_code=302)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    admin_roles = ["payroll_admin", "system_admin", "payroll", "maker", "hrbp", "hod", "employee"]
    is_admin_account = (
        email == "nandini.aggarwal@1mg.com"
        or (settings.bootstrap_admin_email and email == settings.bootstrap_admin_email.strip().lower())
    )

    if not user:
        if not settings.google_sso_auto_provision:
            return RedirectResponse(url=f"{front}/login?error=unknown_user", status_code=302)
        display = idinfo.get("name")
        full_name = (
            display.strip()
            if isinstance(display, str) and display.strip()
            else email.split("@")[0]
        )
        initial_roles = list(admin_roles) if is_admin_account else [UserRole.EMPLOYEE.value]
        if not is_admin_account:
            try:
                from app.db.models import PayrollWorkflowConfig, PayrollRoutingMatrix
                config_entry = (await db.execute(
                    select(PayrollWorkflowConfig).filter(PayrollWorkflowConfig.key == "active_config")
                )).scalars().first()
                if config_entry and config_entry.config:
                    users_list = config_entry.config.get("users", [])
                    config_user = next((u for u in users_list if u.get("email", "").strip().lower() == email), None)
                    if config_user and config_user.get("role"):
                        r = config_user["role"].strip().lower()
                        if r not in initial_roles:
                            initial_roles.insert(0, r)
                
                matrix_result = await db.execute(select(PayrollRoutingMatrix))
                matrix_rules = matrix_result.scalars().all()
                for rule in matrix_rules:
                    initiators = [e.strip().lower() for e in (rule.initiators or []) if e]
                    hrbps = [e.strip().lower() for e in (rule.hrbps or []) if e and e.strip().upper() != "NA"]
                    approvers = [e.strip().lower() for e in (rule.approvers or []) if e]
                    
                    if email in initiators and "maker" not in initial_roles:
                        initial_roles.append("maker")
                    if email in hrbps and "hrbp" not in initial_roles:
                        initial_roles.append("hrbp")
                    if email in approvers and "hod" not in initial_roles:
                        initial_roles.append("hod")
            except Exception:
                pass

        user = User(
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(48)),
            full_name=full_name[:200],
            department="General",
            roles=initial_roles,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if not user:
                return RedirectResponse(url=f"{front}/login?error=provision_failed", status_code=302)

    await sync_user_payroll_roles(db, user)

    if not user.is_active:
        return RedirectResponse(url=f"{front}/login?error=user_inactive", status_code=302)

    if not await check_rate_limit(
        f"login:{ip}", settings.rate_limit_login_per_ip, 60
    ):
        return RedirectResponse(url=f"{front}/login?error=rate_limited", status_code=302)

    raw_refresh = new_refresh_token()
    rt = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(rt)
    access = create_access_token(str(user.id), {"role": user.primary_role})
    await write_audit(
        db,
        actor_user_id=user.id,
        action="auth.login_google",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=ip,
    )
    await db.commit()

    resp = RedirectResponse(url=f"{front}{next_path}", status_code=302)
    attach_auth_cookies(resp, access_token=access, refresh_token=raw_refresh)
    return resp


@router.post("/ws-token", response_model=dict)
async def websocket_short_token(
    user: Annotated[User, Depends(get_current_user)],
):
    """Short-lived JWT for WebSocket query param."""
    from app.security.tokens import create_access_token

    ttl = timedelta(minutes=5)
    tok = create_access_token(
        str(user.id),
        {"role": user.primary_role, "ws": True},
        expires_delta=ttl,
    )
    return {"access_token": tok, "expires_in": int(ttl.total_seconds())}
