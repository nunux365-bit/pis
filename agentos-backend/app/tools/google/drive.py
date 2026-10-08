"""Google Drive API v3 — list files, download/export bytes."""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any
from uuid import UUID

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.google_credentials import get_valid_google_credentials


def _build_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def list_files(
    db: AsyncSession,
    user_id: UUID,
    *,
    page_size: int = 25,
    query: str | None = None,
    fields: str = "nextPageToken, files(id, name, mimeType, modifiedTime, size)",
) -> dict[str, Any]:
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected", "files": []}

    def _run():
        svc = _build_service(creds)
        return (
            svc.files()
            .list(
                pageSize=min(page_size, 100),
                q=query,
                fields=fields,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    return await asyncio.to_thread(_run)


async def get_file_bytes(
    db: AsyncSession, user_id: UUID, file_id: str, *, mime_type: str | None = None
) -> dict[str, Any]:
    """Download file. For Google Docs/Sheets, pass export mime_type (e.g. Excel)."""
    creds = await get_valid_google_credentials(db, user_id)
    if not creds:
        return {"error": "google_not_connected"}

    def _run() -> bytes:
        svc = _build_service(creds)
        if mime_type:
            req = svc.files().export_media(fileId=file_id, mimeType=mime_type)
        else:
            req = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    data: bytes = await asyncio.to_thread(_run)
    return {
        "content_base64": base64.standard_b64encode(data).decode("ascii"),
        "size": len(data),
    }
