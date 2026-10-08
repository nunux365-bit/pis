"""Gmail API v1 — list messages, get raw, send (sync client in thread)."""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from typing import Any
from uuid import UUID

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.google_credentials import get_valid_google_credentials


def _build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


async def list_messages(
    db: AsyncSession,
    user_id: UUID,
    *,
    max_results: int = 20,
    query: str | None = None,
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected", "messages": []}

    def _run():
        svc = _build_service(creds)
        req = svc.users().messages().list(
            userId="me",
            maxResults=min(max_results, 100),
            q=query or None,
        )
        return req.execute()

    return await asyncio.to_thread(_run)


async def get_message(
    db: AsyncSession, user_id: UUID, message_id: str
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected"}

    def _run():
        svc = _build_service(creds)
        return (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    return await asyncio.to_thread(_run)


async def send_text_email(
    db: AsyncSession,
    user_id: UUID,
    *,
    to: str,
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected"}

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def _run():
        svc = _build_service(creds)
        return (
            svc.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )

    return await asyncio.to_thread(_run)
