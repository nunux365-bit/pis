"""REST API for the cold outreach framework."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.db.models import OutreachLead, OutreachReply, UserRole
from app.email_automation import gmail_sa
from app.email_automation.outreach.config import CampaignConfig

log = logging.getLogger(__name__)
router = APIRouter()


# ─── Schemas ────────────────────────────────────────────────────────────────

class CampaignRead(BaseModel):
    id: int
    campaign_name: str
    gsheet_id: str
    sender_email: str
    dashboard_display_name: str
    is_active: bool
    created_at: datetime


class LeadHealthEntry(BaseModel):
    id: int
    account_name: str | None
    spoc: str | None
    primary_email: str | None
    gsheet_row_index: int
    issues: list[str]
    status: str
    issues_acknowledged_at: datetime | None

    model_config = {"from_attributes": True}


class ThreadSummary(BaseModel):
    lead_id: int
    gmail_thread_id: str
    account_name: str | None
    spoc: str | None
    last_reply_category: str | None
    last_reply_at: datetime | None
    reply_count: int
    sent_at: datetime | None


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    _: Any = Depends(get_current_user),
):
    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    return [
        CampaignRead(
            id=idx + 1,
            campaign_name=cfg.campaign_name,
            gsheet_id=cfg.gsheet_id,
            sender_email=cfg.sender_email,
            dashboard_display_name=cfg.dashboard_display_name,
            is_active=True,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        for idx, cfg in enumerate(ACTIVE_CAMPAIGNS)
    ]


# ─── Sheet Health Dashboard ──────────────────────────────────────────────────

@router.get("/{campaign_name}/health", response_model=list[LeadHealthEntry])
async def get_sheet_health(
    campaign_name: str,
    include_acknowledged: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    stmt = select(OutreachLead).where(
        OutreachLead.campaign_name == campaign_name,
        OutreachLead.issues != [],
    )
    if not include_acknowledged:
        stmt = stmt.where(OutreachLead.issues_acknowledged_at.is_(None))
    rows = (await db.execute(stmt.order_by(OutreachLead.updated_at.desc()))).scalars().all()
    return rows


@router.post("/{campaign_name}/leads/{lead_id}/acknowledge", status_code=204)
async def acknowledge_lead_issues(
    campaign_name: str,
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    lead = (await db.execute(
        select(OutreachLead).where(
            OutreachLead.id == lead_id,
            OutreachLead.campaign_name == campaign_name,
        )
    )).scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead.issues_acknowledged_at = datetime.now(timezone.utc)
    await db.commit()


@router.post("/{campaign_name}/leads/{lead_id}/retry", status_code=204)
async def retry_failed_lead(
    campaign_name: str,
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(require_roles(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)),
):
    lead = (await db.execute(
        select(OutreachLead).where(
            OutreachLead.id == lead_id,
            OutreachLead.campaign_name == campaign_name,
            OutreachLead.status == "failed",
        )
    )).scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Failed lead not found")
    lead.status = "pending"
    lead.send_attempt_count = 0
    lead.error = None
    lead.issues = [i for i in (lead.issues or []) if i != "send_failed"]
    await db.commit()


# ─── Intelligence / Thread Preview ──────────────────────────────────────────

@router.get("/intelligence/{campaign_name}/threads", response_model=list[ThreadSummary])
async def list_intelligence_threads(
    campaign_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    offset = (page - 1) * page_size
    stmt = (
        select(OutreachLead)
        .where(
            OutreachLead.campaign_name == campaign_name,
            OutreachLead.gmail_thread_id.isnot(None),
        )
        .order_by(OutreachLead.last_reply_at.desc().nullslast())
        .offset(offset)
        .limit(page_size)
    )
    leads = (await db.execute(stmt)).scalars().all()
    return [
        ThreadSummary(
            lead_id=l.id,
            gmail_thread_id=l.gmail_thread_id,
            account_name=l.account_name,
            spoc=l.spoc,
            last_reply_category=l.last_reply_category,
            last_reply_at=l.last_reply_at,
            reply_count=l.reply_count,
            sent_at=l.sent_at,
        )
        for l in leads
    ]


@router.get("/intelligence/{campaign_name}/threads/{thread_id}")
async def get_thread_detail(
    campaign_name: str,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    lead = (await db.execute(
        select(OutreachLead).where(
            OutreachLead.campaign_name == campaign_name,
            OutreachLead.gmail_thread_id == thread_id,
        )
    )).scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Thread not found")

    replies = (await db.execute(
        select(OutreachReply)
        .where(OutreachReply.outreach_lead_id == lead.id)
        .order_by(OutreachReply.classified_at)
    )).scalars().all()

    classification_by_msg: dict[str, dict] = {
        r.gmail_message_id: {
            "category": r.category,
            "confidence": r.confidence,
            "justification": r.justification,
        }
        for r in replies
    }

    try:
        thread_full = await asyncio.to_thread(gmail_sa.fetch_thread_full, thread_id)
    except Exception as exc:
        log.warning("outreach thread detail: Gmail fetch failed: %s", exc)
        thread_full = {"messages": []}

    messages_out = []
    for msg in (thread_full.get("messages") or []):
        msg_id = msg.get("id", "")
        body = await asyncio.to_thread(gmail_sa.message_resource_plain_text, msg)
        messages_out.append({
            "message_id": msg_id,
            "from": _header_val(msg, "From"),
            "subject": _header_val(msg, "Subject"),
            "date": _header_val(msg, "Date"),
            "body": body,
            "classification": classification_by_msg.get(msg_id),
        })

    return {
        "lead": {
            "id": lead.id,
            "account_name": lead.account_name,
            "spoc": lead.spoc,
            "primary_email": lead.primary_email,
            "sent_at": lead.sent_at,
            "last_reply_category": lead.last_reply_category,
            "reply_count": lead.reply_count,
        },
        "messages": messages_out,
    }


def _header_val(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").strip().lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""


# ─── Campaign Summary + Lead List ────────────────────────────────────────────

class ReplyBreakdownEntry(BaseModel):
    category: str
    count: int


class LeadRead(BaseModel):
    id: int
    account_name: str | None
    spoc: str | None
    primary_email: str | None
    source: str | None
    status: str
    tab_name: str | None
    issues: list[str]
    sent_at: datetime | None
    subject_sent: str | None
    gmail_thread_id: str | None
    last_reply_category: str | None
    last_reply_at: datetime | None
    reply_count: int
    gsheet_synced_at: datetime | None
    model_config = {"from_attributes": True}


class CampaignSummary(BaseModel):
    campaign_name: str
    total_leads: int
    pending: int
    sent: int
    failed: int
    hold: int
    duplicate: int
    skipped: int
    removed: int
    reply_count: int
    reply_breakdown: list[ReplyBreakdownEntry]


@router.get("/{campaign_name}/summary", response_model=CampaignSummary)
async def get_campaign_summary(
    campaign_name: str,
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    from sqlalchemy import func, case
    stmt = select(
        func.count(OutreachLead.id).label("total"),
        func.sum(case((OutreachLead.status == "pending", 1), else_=0)).label("pending"),
        func.sum(case((OutreachLead.status == "sent", 1), else_=0)).label("sent"),
        func.sum(case((OutreachLead.status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((OutreachLead.status == "hold", 1), else_=0)).label("hold"),
        func.sum(case((OutreachLead.status == "duplicate", 1), else_=0)).label("duplicate"),
        func.sum(case((OutreachLead.status == "skipped", 1), else_=0)).label("skipped"),
        func.sum(case((OutreachLead.status == "removed_from_sheet", 1), else_=0)).label("removed"),
        func.sum(OutreachLead.reply_count).label("reply_count"),
    ).where(OutreachLead.campaign_name == campaign_name)
    row = (await db.execute(stmt)).one()

    # Reply breakdown by category
    cat_stmt = (
        select(OutreachLead.last_reply_category, func.count(OutreachLead.id))
        .where(
            OutreachLead.campaign_name == campaign_name,
            OutreachLead.last_reply_category.isnot(None),
        )
        .group_by(OutreachLead.last_reply_category)
    )
    cat_rows = (await db.execute(cat_stmt)).all()
    breakdown = [ReplyBreakdownEntry(category=c, count=n) for c, n in cat_rows]

    return CampaignSummary(
        campaign_name=campaign_name,
        total_leads=row.total or 0,
        pending=row.pending or 0,
        sent=row.sent or 0,
        failed=row.failed or 0,
        hold=row.hold or 0,
        duplicate=row.duplicate or 0,
        skipped=row.skipped or 0,
        removed=row.removed or 0,
        reply_count=row.reply_count or 0,
        reply_breakdown=breakdown,
    )


@router.get("/{campaign_name}/leads", response_model=dict)
async def list_leads(
    campaign_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: str | None = Query(None),
    reply_category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: Any = Depends(get_current_user),
):
    from sqlalchemy import func
    base = select(OutreachLead).where(OutreachLead.campaign_name == campaign_name)
    if status:
        base = base.where(OutreachLead.status == status)
    if reply_category:
        base = base.where(OutreachLead.last_reply_category == reply_category)

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    leads = (
        await db.execute(
            base.order_by(OutreachLead.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [LeadRead.model_validate(l) for l in leads],
    }
