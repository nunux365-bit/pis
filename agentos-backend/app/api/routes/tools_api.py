"""AgentOS tools — async HTTP + Google Workspace (per connected user)."""

import base64
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.tools import rest_api
from app.tools.google import drive, gmail, sheets

router = APIRouter()


class HttpToolBody(BaseModel):
    method: str = Field(..., pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD)$")
    url: str = Field(..., min_length=8, max_length=4000)
    headers: dict[str, str] | None = None
    json_body: Any | None = Field(None, description="JSON request body for POST/PATCH/PUT")
    params: dict[str, str] | None = None
    timeout_seconds: float = Field(30.0, ge=1.0, le=120.0)


@router.post("/http")
async def tool_http_request(
    body: HttpToolBody,
    user: Annotated[User, Depends(get_current_user)],
):
    """Server-side async HTTP with SSRF blocks and optional hostname allowlist."""
    _ = user
    try:
        status_code, rh, raw = await rest_api.async_http_request(
            body.method,
            body.url,
            headers=rest_api.json_safe_headers(body.headers),
            json_body=body.json_body,
            params=body.params,
            timeout_seconds=body.timeout_seconds,
        )
    except rest_api.RestApiError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    text: str | None = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = base64.standard_b64encode(raw).decode("ascii")
        rh = {**rh, "x-body-encoding": "base64"}
    return {
        "status_code": status_code,
        "headers": rh,
        "body": text,
    }


@router.get("/google/gmail/messages")
async def tool_gmail_list(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    max_results: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=500),
):
    data = await gmail.list_messages(db, user.id, max_results=max_results, query=q)
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google in Integrations first")
    return data


@router.get("/google/gmail/messages/{message_id}")
async def tool_gmail_get(
    message_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    data = await gmail.get_message(db, user.id, message_id)
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google first")
    return data


class GmailSendBody(BaseModel):
    to: str = Field(..., min_length=3, max_length=500)
    subject: str = Field("", max_length=500)
    body_text: str = Field(..., min_length=1, max_length=1_000_000)


@router.post("/google/gmail/send")
async def tool_gmail_send(
    body: GmailSendBody,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    data = await gmail.send_text_email(
        db, user.id, to=body.to, subject=body.subject, body_text=body.body_text
    )
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google first")
    return data


@router.get("/google/drive/files")
async def tool_drive_list(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page_size: int = Query(25, ge=1, le=100),
    q: str | None = Query(None, max_length=2000),
):
    data = await drive.list_files(db, user.id, page_size=page_size, query=q)
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google first")
    return data


@router.get("/google/drive/files/{file_id}/content")
async def tool_drive_content(
    file_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    export_mime_type: str | None = Query(
        None,
        description="For Google Docs/Sheets, set export MIME (e.g. application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)",
    ),
):
    data = await drive.get_file_bytes(db, user.id, file_id, mime_type=export_mime_type)
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google first")
    return data


@router.get("/google/sheets/{spreadsheet_id}/values")
async def tool_sheets_values(
    spreadsheet_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    range_a1: str = Query(..., alias="range", min_length=1, max_length=500),
):
    data = await sheets.get_values(db, user.id, spreadsheet_id, range_a1)
    if data.get("error") == "google_not_connected":
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Connect Google first")
    return data
