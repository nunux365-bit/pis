"""Load / refresh Google OAuth credentials from user_oauth_tokens."""

from __future__ import annotations

import json
import logging
from uuid import UUID

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserOAuthToken

log = logging.getLogger(__name__)

GOOGLE_PROVIDER = "google"


async def get_google_token_row(
    db: AsyncSession, user_id: UUID
) -> UserOAuthToken | None:
    r = await db.execute(
        select(UserOAuthToken).where(
            UserOAuthToken.user_id == user_id,
            UserOAuthToken.provider == GOOGLE_PROVIDER,
        )
    )
    return r.scalar_one_or_none()


def credentials_from_row(row: UserOAuthToken) -> Credentials:
    info = dict(row.credential_json)
    return Credentials.from_authorized_user_info(info)


async def get_valid_google_credentials(
    db: AsyncSession, user_id: UUID
) -> Credentials | None:
    row = await get_google_token_row(db, user_id)
    if not row:
        return None
    creds = credentials_from_row(row)
    if creds.expired and creds.refresh_token:

        def _refresh() -> None:
            creds.refresh(Request())

        import asyncio

        await asyncio.to_thread(_refresh)
        row.credential_json = json.loads(creds.to_json())
        await db.flush()
    if not creds.valid:
        log.warning("Google credentials invalid for user_id=%s", user_id)
        return None
    return creds
