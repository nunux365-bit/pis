"""Google Workspace OAuth — connect user account for Gmail / Drive / Sheets tools."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config.settings import settings
from app.db.models import User, UserOAuthToken
from app.db.session import get_db
from app.security.tokens import create_google_oauth_state, verify_google_oauth_state
from app.services.google_credentials import GOOGLE_PROVIDER, get_google_token_row

log = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _redirect_uri() -> str:
    if settings.google_oauth_redirect_uri.strip():
        return settings.google_oauth_redirect_uri.strip()
    base = settings.api_public_base_url.rstrip("/")
    return f"{base}/api/integrations/google/callback"


def _require_google_config() -> None:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google OAuth is not configured (set GOOGLE_OAUTH_CLIENT_ID / SECRET)",
        )


def _flow(state: str | None = None) -> Flow:
    redirect = _redirect_uri()
    client = {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect],
        }
    }
    return Flow.from_client_config(
        client,
        scopes=GOOGLE_SCOPES,
        state=state,
    )


@router.get("/authorize")
async def google_authorize(
    user: Annotated[User, Depends(get_current_user)],
):
    _require_google_config()
    flow = _flow()
    flow.redirect_uri = _redirect_uri()
    state = create_google_oauth_state(user.id)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return {"authorization_url": url, "state": state}


@router.get("/callback")
async def google_callback(
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_google_config()
    try:
        user_id = verify_google_oauth_state(state)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state")
    flow = _flow()
    flow.redirect_uri = _redirect_uri()
    try:
        await asyncio.to_thread(flow.fetch_token, code=code)
    except Exception as e:
        log.warning("Google token exchange failed: %s", e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token exchange failed") from e

    creds = flow.credentials
    email: str | None = None
    try:

        def _whoami():
            svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
            return svc.userinfo().get().execute()

        info = await asyncio.to_thread(_whoami)
        email = info.get("email")
    except Exception as e:
        log.debug("userinfo fetch skipped: %s", e)

    cred_dict = json.loads(creds.to_json())
    r = await db.execute(
        select(UserOAuthToken).where(
            UserOAuthToken.user_id == user_id,
            UserOAuthToken.provider == GOOGLE_PROVIDER,
        )
    )
    row = r.scalar_one_or_none()
    scope_str = " ".join(GOOGLE_SCOPES)
    if row:
        row.credential_json = cred_dict
        row.scopes = scope_str
        row.account_email = email
    else:
        db.add(
            UserOAuthToken(
                user_id=user_id,
                provider=GOOGLE_PROVIDER,
                credential_json=cred_dict,
                scopes=scope_str,
                account_email=email,
            )
        )
    await db.commit()
    return {
        "ok": True,
        "message": "Google account connected",
        "account_email": email,
    }


@router.get("/status")
async def google_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await get_google_token_row(db, user.id)
    if not row:
        return {"connected": False, "account_email": None}
    return {
        "connected": True,
        "account_email": row.account_email,
        "scopes": row.scopes,
    }


@router.delete("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def google_disconnect(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await get_google_token_row(db, user.id)
    if row:
        await db.execute(delete(UserOAuthToken).where(UserOAuthToken.id == row.id))
        await db.commit()
