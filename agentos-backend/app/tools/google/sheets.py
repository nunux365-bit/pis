"""Google Sheets API v4 — read ranges (values)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.google_credentials import get_valid_google_credentials


def _build_service(creds: Credentials):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


async def get_values(
    db: AsyncSession,
    user_id: UUID,
    spreadsheet_id: str,
    range_a1: str,
    *,
    major_dimension: str = "ROWS",
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected"}

    def _run():
        svc = _build_service(creds)
        return (
            svc.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                majorDimension=major_dimension,
            )
            .execute()
        )

    return await asyncio.to_thread(_run)


async def batch_get(
    db: AsyncSession,
    user_id: UUID,
    spreadsheet_id: str,
    ranges: list[str],
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected"}

    def _run():
        svc = _build_service(creds)
        return (
            svc.spreadsheets()
            .values()
            .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges)
            .execute()
        )

    return await asyncio.to_thread(_run)
