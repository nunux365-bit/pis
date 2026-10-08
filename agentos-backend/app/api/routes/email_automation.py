"""HTTP surface for the email automation review queue.

Review + triage only. Scan (inbox poll) and dispatch (outbound send) are
driven **exclusively** by the scheduler (see :mod:`jobs.email_automation_scan`)
and the unified LangGraph (see :mod:`app.agents.email_automation.graph`).
We deliberately do NOT expose ``POST /scan`` or ``POST /dispatch`` HTTP
triggers — keeping the cron the single execution path avoids split-brain
between a manual HTTP tick and a concurrent cron tick (both compete for
the same advisory lock, but the HTTP path skipped the graph's mode
router and observability seams).

All mutating endpoints require ``system_admin``, ``dept_head``, or
``email_agent_access`` — email dispatch is a high-trust action that bypasses human
review on a per-run basis, so we keep the RBAC strict. Read-only listings (messages,
sends) are available to any authenticated user; **metrics** and **receivable-dashboard**
are ``system_admin`` or ``email_agent_access`` only.

Every mutating route also enforces the global kill switch
(:attr:`settings.email_automation_enabled`) so toggling the feature off in the
environment immediately blocks approvals and retries \u2014 even through the API.
Without that check, the API would silently bypass the kill switch the
scheduler honors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Any, Literal
from uuid import UUID

log = logging.getLogger(__name__)

import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.api.deps import get_current_user, require_roles
from app.config.settings import settings
from app.db.models import (
    EmailAutomationMessage,
    EmailAutomationSend,
    GmailIntelligence,
    ReceivableDashboardSnapshot,
    User,
    UserRole,
)
from app.db.session import get_db
from app.email_automation import pipeline
from app.services.receivable_dashboard import kpi_key_is_due_ageing
from app.email_automation.intelligence_pagination import (
    ThreadCursorError,
    decode_thread_cursor,
    encode_thread_cursor,
)
from app.email_automation.pipeline.intelligence_metrics import (
    compute_intelligence_metrics,
    intelligence_window_start,
)
from app.email_automation.pipeline.kam_metrics import aggregate_kam_reply_metrics
from app.email_automation.schemas import (
    ApproveRequest,
    CategoryBreakdownRow,
    EmailThreadMessage,
    EmailThreadResponse,
    IntelligenceMetricsResponse,
    IntelligencePriorPeriodRead,
    IntelligenceReceivableRow,
    IntelligenceReceivablesResponse,
    IntelligenceSummaryResponse,
    IntelligenceThreadRow,
    IntelligenceThreadsPage,
    IntelligenceTimelinePoint,
    KamDashboardResponse,
    KamDashboardRow,
    MessageRead,
    MetricsAttention,
    MetricsHealth,
    MetricsResponse,
    MetricsTrendPoint,
    RejectRequest,
    RetryFailedRequest,
    SendRead,
    SimpleResponse,
)

router = APIRouter()

EmailAutomationMutateAccess = require_roles(
    UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD, UserRole.EMAIL_AGENT_ACCESS
)
EmailAutomationMetricsAccess = require_roles(
    UserRole.SYSTEM_ADMIN, UserRole.EMAIL_AGENT_ACCESS
)


# Reply Tracker engagement window.  The Reply Tracker offers 7 / 14 / 30-day views
# (default 7).  All windowed metrics are **send-anchored**: a party counts for the
# window when the email we sent it (status='sent') landed inside the window, and a
# reply only counts if *its* email was sent inside the window — a reply this week to
# an email sent before the window does not count.  Total Overdue (Book) and the
# "Export All Data" sheet are never windowed.
REPLY_TRACKER_WINDOW_DAYS = 7
REPLY_TRACKER_WINDOW_CHOICES = (7, 14, 30)


def reply_tracker_window_start(days: int = REPLY_TRACKER_WINDOW_DAYS) -> datetime:
    """Lower bound (UTC) for the trailing ``days``-day Reply-Tracker window.

    Anchored to **midnight** (00:00 UTC) ``days`` days ago, not a rolling timestamp,
    so the window covers whole calendar days.  This makes the cards line up exactly
    with a calendar-date filter on the exported sheet's ``Email Sent At`` column
    (which stores dates, not times) — a send dated N days ago is either fully in or
    fully out, never split by the current time-of-day.
    """

    midnight_today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight_today - timedelta(days=days)


def normalize_window_days(days: int | None) -> int:
    """Clamp a requested window to an allowed choice, defaulting to 7."""

    if days in REPLY_TRACKER_WINDOW_CHOICES:
        return int(days)
    return REPLY_TRACKER_WINDOW_DAYS


def _ts_within(ts: datetime | None, *, since: datetime) -> bool:
    """True when ``ts`` is set and falls within the window ``[since, now)``."""

    return ts is not None and ts >= since


def _send_in_window(send: Any | None, *, since: datetime) -> bool:
    """Send-anchored base: True when the party's latest 'sent' email is in-window.

    Everything the Reply Tracker windows keys off this — "Emails Delivered", the
    in-campaign overdue total, and (gated on the same send) "Replies Received" and
    the reply breakdown.  A reply is only counted when its party's send is in-window,
    so a recent reply to an email sent before the window does not count.
    """

    return send is not None and _ts_within(send.sent_at, since=since)


# ---------------------------------------------------------------------------
# Shared receivables context — used by both /intelligence/receivables and
# /intelligence/summary to avoid duplicating the three DB round-trips.
# ---------------------------------------------------------------------------

@dataclass
class _ReceivablesCtx:
    """Merged snapshot + DB correlation data for one BU scope (or All)."""

    party_map: dict[str, dict[str, Any]]        # hana_code → {name, overdue, bu}
    bu_options: list[str]                        # dropdown options including "All"
    latest_intel: dict[str, GmailIntelligence]  # hana_code → latest intel row
    latest_send: dict[str, EmailAutomationSend] # hana_code → latest sent row
    snapshot_created_at: datetime | None
    # Timestamp of the snapshot immediately before this one.  Used as the lower
    # bound for engagement queries so only emails/replies from the current
    # campaign cycle are counted.  None when this is the first snapshot (no
    # lower bound — show all engagement).
    prev_snapshot_created_at: datetime | None
    # Pre-computed KPI column sums from the snapshot (all Excel rows, no code-
    # matching required).  Use kpi_key_is_due_ageing() to filter to overdue-only.
    kpi_lakh_all: dict[str, float]              # whole-book KPI column → lakh
    kpi_lakh_by_bu: dict[str, dict[str, float]] # BU name → {column → lakh}


_CTX_CACHE_TTL = 300  # seconds — well within the weekly snapshot lifecycle
_CTX_CACHE_VERSION = "v2"


def _ctx_cache_key(snapshot_id: Any, bu: str | None, kind: str) -> str:
    return f"receivables_ctx:{_CTX_CACHE_VERSION}:{snapshot_id}:{bu or 'All'}:{kind}"


def _ctx_to_json(
    ctx: _ReceivablesCtx,
    prev_created_at: datetime | None,
) -> str:
    def _dt(d: datetime | None) -> str | None:
        return d.isoformat() if d else None

    return json.dumps({
        "party_map": ctx.party_map,
        "bu_options": ctx.bu_options,
        "latest_intel": {
            k: {
                "business_key": v.business_key,
                "category": v.category,
                "classified_at": _dt(v.classified_at),
                "gmail_thread_id": v.gmail_thread_id,
            }
            for k, v in ctx.latest_intel.items()
        },
        "latest_send": {
            k: {
                "business_key": v.business_key,
                "sent_at": _dt(v.sent_at),
                "gmail_thread_id": v.gmail_thread_id,
                "rendered_subject": v.rendered_subject,
            }
            for k, v in ctx.latest_send.items()
        },
        "snapshot_created_at": _dt(ctx.snapshot_created_at),
        "prev_snapshot_created_at": _dt(prev_created_at),
        "kpi_lakh_all": ctx.kpi_lakh_all,
        "kpi_lakh_by_bu": ctx.kpi_lakh_by_bu,
    })


def _ctx_from_json(raw: str) -> _ReceivablesCtx:
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    d = json.loads(raw)
    return _ReceivablesCtx(
        party_map=d["party_map"],
        bu_options=d["bu_options"],
        latest_intel={
            k: SimpleNamespace(**{**v, "classified_at": _dt(v["classified_at"])})
            for k, v in d["latest_intel"].items()
        },
        latest_send={
            k: SimpleNamespace(**{**v, "sent_at": _dt(v["sent_at"])})
            for k, v in d["latest_send"].items()
        },
        snapshot_created_at=_dt(d["snapshot_created_at"]),
        prev_snapshot_created_at=_dt(d["prev_snapshot_created_at"]),
        kpi_lakh_all=d["kpi_lakh_all"],
        kpi_lakh_by_bu=d["kpi_lakh_by_bu"],
    )


async def _load_receivables_ctx(
    db: AsyncSession,
    *,
    business_unit: str | None,
    kind: str,
) -> _ReceivablesCtx | None:
    """Fetch snapshot + correlate intel/sends for the given BU.

    None / "All" business_unit means whole-book (no BU filter).
    Returns None when no snapshot exists yet.

    Results are cached in Redis (TTL=5 min) keyed by snapshot ID so repeated
    calls — e.g. debounced search keystrokes — skip the two heavy IN queries.
    Falls back to DB-only path if Redis is unavailable.
    """
    # ── 1. Lightweight snapshot-ID query (always needed for cache key) ────────
    recent_meta = (
        await db.execute(
            select(
                ReceivableDashboardSnapshot.id,
                ReceivableDashboardSnapshot.created_at,
            )
            .order_by(desc(ReceivableDashboardSnapshot.created_at))
            .limit(2)
        )
    ).all()
    if not recent_meta:
        return None
    snapshot_id = recent_meta[0].id
    prev_snapshot_created_at: datetime | None = recent_meta[1].created_at if len(recent_meta) > 1 else None

    # ── 2. Try Redis cache ────────────────────────────────────────────────────
    cache_key = _ctx_cache_key(snapshot_id, business_unit, kind)
    try:
        from app.infra.redis_client import get_redis
        redis = get_redis()
        cached = await redis.get(cache_key)
        if cached:
            return _ctx_from_json(cached)
    except Exception as exc:
        log.debug("receivables_ctx cache miss (redis unavailable): %s", exc)

    # ── 3. Cache miss — full DB load ──────────────────────────────────────────
    recent = (
        await db.execute(
            select(ReceivableDashboardSnapshot)
            .order_by(desc(ReceivableDashboardSnapshot.created_at))
            .limit(2)
        )
    ).scalars().all()
    snapshot = recent[0]

    payload = snapshot.payload
    bu_options: list[str] = payload.get("business_units", [])
    party_root = payload.get("all_parties") or payload.get("top_parties", {})

    # kpi_lakh is pre-computed from all Excel rows during ingest — authoritative
    # per-BU and whole-book overdue totals with no party-code matching required.
    kpi_root = payload.get("kpi_lakh", {})
    kpi_lakh_all: dict[str, float] = kpi_root.get("all", {}) if isinstance(kpi_root, dict) else {}
    kpi_lakh_by_bu: dict[str, dict[str, float]] = kpi_root.get("by_business_unit", {}) if isinstance(kpi_root, dict) else {}

    # Treat None / "All" identically — return the whole book.
    if business_unit and business_unit != "All":
        parties_source: list = party_root.get("by_business_unit", {}).get(business_unit, [])
    else:
        parties_source = party_root.get("all", [])

    party_map: dict[str, dict[str, Any]] = {}
    for p in parties_source:
        code = p.get("code")
        if not code:
            continue
        overdue = float(p.get("net_due_lakh", 0.0))
        if code in party_map:
            # Same HANA code appeared under a different (name, BU) group in the
            # snapshot — sum the overdue amounts so the dashboard matches what the
            # email engine sees after aggregating all rows by HANA code.
            party_map[code]["overdue"] += overdue
        else:
            party_map[code] = {
                "name": p.get("name", ""),
                "overdue": overdue,
                "bu": p.get("business_unit", ""),
            }

    if not party_map:
        return _ReceivablesCtx(
            party_map={},
            bu_options=bu_options,
            latest_intel={},
            latest_send={},
            snapshot_created_at=snapshot.created_at,
            prev_snapshot_created_at=prev_snapshot_created_at,
            kpi_lakh_all=kpi_lakh_all,
            kpi_lakh_by_bu=kpi_lakh_by_bu,
        )

    keys = list(party_map.keys())

    # Latest GmailIntelligence per business_key via window function — avoids
    # loading all historical rows into Python.
    # No temporal lower bound: row_number() rn==1 already returns only the most
    # recent reply per party, so there's no double-counting risk.  Bounding by
    # prev_snapshot_created_at caused reply counts to drop to zero when the sheet
    # is re-ingested (e.g. for a data-quality fix) without a new campaign cycle.
    intel_where = [
        GmailIntelligence.kind == kind,
        GmailIntelligence.business_key.in_(keys),
    ]
    intel_rn_subq = (
        select(
            GmailIntelligence.id,
            func.row_number()
            .over(
                partition_by=GmailIntelligence.business_key,
                order_by=desc(GmailIntelligence.classified_at),
            )
            .label("rn"),
        )
        .where(*intel_where)
        .subquery()
    )
    intel_rows = (
        await db.execute(
            select(GmailIntelligence)
            .join(intel_rn_subq, GmailIntelligence.id == intel_rn_subq.c.id)
            .where(intel_rn_subq.c.rn == 1)
        )
    ).scalars().all()

    # Latest sent EmailAutomationSend per business_key via window function.
    # No lower bound — we want every party ever emailed, not just those emailed
    # in the current cycle.  The row_number() window function already returns
    # only the most-recent send per party, so historical sends don't double-count.
    # (Scoping this to prev_snapshot_created_at caused total_overdue_in_campaign
    # to exclude parties emailed in prior cycles who replied in the current one,
    # making replies_lakh > in_campaign_lakh for BUs with older send histories.)
    send_where = [
        EmailAutomationSend.business_key.in_(keys),
        EmailAutomationSend.status == "sent",
    ]
    send_rn_subq = (
        select(
            EmailAutomationSend.id,
            func.row_number()
            .over(
                partition_by=EmailAutomationSend.business_key,
                order_by=desc(EmailAutomationSend.sent_at),
            )
            .label("rn"),
        )
        .where(*send_where)
        .subquery()
    )
    send_rows = (
        await db.execute(
            select(EmailAutomationSend)
            .options(load_only(
                EmailAutomationSend.id,
                EmailAutomationSend.business_key,
                EmailAutomationSend.sent_at,
                EmailAutomationSend.rendered_subject,
                EmailAutomationSend.gmail_thread_id,
            ))
            .join(send_rn_subq, EmailAutomationSend.id == send_rn_subq.c.id)
            .where(send_rn_subq.c.rn == 1)
        )
    ).scalars().all()

    # Overdue amounts come exclusively from the snapshot (TDS-adjusted net_due_lakh).
    # The previous _net_pending override from aggregated_data was TDS-inclusive and
    # caused dashboard totals to exceed the TDS-adjusted book total for some BUs.
    # Note: amounts shown here will differ from what was quoted in sent reminder
    # emails (which used TDS-inclusive _net_pending) — this is intentional.

    ctx = _ReceivablesCtx(
        party_map=party_map,
        bu_options=bu_options,
        latest_intel={r.business_key: r for r in intel_rows},
        latest_send={s.business_key: s for s in send_rows},
        snapshot_created_at=snapshot.created_at,
        prev_snapshot_created_at=prev_snapshot_created_at,
        kpi_lakh_all=kpi_lakh_all,
        kpi_lakh_by_bu=kpi_lakh_by_bu,
    )

    # ── 4. Populate cache ─────────────────────────────────────────────────────
    try:
        from app.infra.redis_client import get_redis
        redis = get_redis()
        await redis.set(cache_key, _ctx_to_json(ctx, prev_snapshot_created_at), ex=_CTX_CACHE_TTL)
    except Exception as exc:
        log.debug("receivables_ctx cache write failed: %s", exc)

    return ctx


def _windowed_receivable_rows(
    ctx: _ReceivablesCtx, *, since: datetime
) -> list[IntelligenceReceivableRow]:
    """Send-anchored party rows for a window, shared by the detail table and the
    current-view export so the two never diverge.

    ``Email Sent At`` is set only when the party's latest ``status='sent'`` send is
    in-window; ``Reply Received At`` / ``Reply Category`` only when that in-window
    email also got a reply (a reply to an email sent before the window is dropped).
    ``gmail_thread_id`` is left unwindowed so the conversation stays openable.
    Sorted by overdue ₹ descending.
    """

    rows: list[IntelligenceReceivableRow] = []
    for code, info in ctx.party_map.items():
        send = ctx.latest_send.get(code)
        intel = ctx.latest_intel.get(code)
        in_window = _send_in_window(send, since=since)
        reply = intel if (in_window and intel is not None) else None
        rows.append(
            IntelligenceReceivableRow(
                hana_code=code,
                party_name=info["name"],
                business_unit=info["bu"],
                total_overdue_lakh=info["overdue"],
                email_sent_at=send.sent_at if in_window else None,
                reply_received_at=reply.classified_at if reply is not None else None,
                reply_category=reply.category if reply is not None else None,
                gmail_thread_id=(
                    send.gmail_thread_id if send is not None
                    else intel.gmail_thread_id if intel is not None
                    else None
                ),
                email_subject=send.rendered_subject if in_window else None,
            )
        )
    rows.sort(key=lambda x: x.total_overdue_lakh, reverse=True)
    return rows


def _ensure_feature_enabled() -> None:
    """Reject mutating calls when the feature is globally disabled."""

    if not settings.email_automation_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email automation is disabled (EMAIL_AUTOMATION_ENABLED=false)",
        )


@router.get("/messages", response_model=list[MessageRead])
async def list_messages(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[
        Literal["received", "classified", "processed", "skipped", "failed", "all"],
        Query(alias="status"),
    ] = "all",
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MessageRead]:
    stmt = (
        select(EmailAutomationMessage)
        .order_by(desc(EmailAutomationMessage.created_at))
        .limit(limit)
    )
    if status_filter != "all":
        stmt = stmt.where(EmailAutomationMessage.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [MessageRead.model_validate(r, from_attributes=True) for r in rows]


@router.get("/sends", response_model=list[SendRead])
async def list_sends(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[
        Literal[
            "rendered", "approved", "sending", "sent",
            "failed", "skipped", "all",
        ],
        Query(alias="status"),
    ] = "all",
    workflow_type: str | None = None,
    variant: str | None = None,
    period_key: str | None = None,
    business_key: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SendRead]:
    stmt = (
        select(EmailAutomationSend)
        .order_by(desc(EmailAutomationSend.created_at))
        .limit(limit)
    )
    if status_filter != "all":
        stmt = stmt.where(EmailAutomationSend.status == status_filter)
    if workflow_type:
        stmt = stmt.where(EmailAutomationSend.workflow_type == workflow_type)
    if variant:
        stmt = stmt.where(EmailAutomationSend.variant == variant)
    if period_key:
        stmt = stmt.where(EmailAutomationSend.period_key == period_key)
    if business_key:
        stmt = stmt.where(EmailAutomationSend.business_key == business_key)
    rows = (await db.execute(stmt)).scalars().all()
    return [SendRead.model_validate(r, from_attributes=True) for r in rows]


@router.get("/sends/{send_id}", response_model=SendRead)
async def get_send(
    send_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> SendRead:
    row = (
        await db.execute(
            select(EmailAutomationSend).where(EmailAutomationSend.id == send_id)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Send not found")
    return SendRead.model_validate(row, from_attributes=True)


@router.post("/sends/{send_id}/approve", response_model=SimpleResponse)
async def approve_send(
    send_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(EmailAutomationMutateAccess)],
    body: ApproveRequest = ApproveRequest(),
) -> SimpleResponse:
    _ensure_feature_enabled()
    ok = await pipeline.mark_approved(db, send_id, user.id, note=body.note)
    await db.commit()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Send is not approvable (already sent, failed, or does not exist)",
        )
    return SimpleResponse(ok=True, detail="approved")


@router.post("/sends/{send_id}/reject", response_model=SimpleResponse)
async def reject_send(
    send_id: UUID,
    body: RejectRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(EmailAutomationMutateAccess)],
) -> SimpleResponse:
    _ensure_feature_enabled()
    ok = await pipeline.mark_rejected(db, send_id, body.reason, actor_id=user.id)
    await db.commit()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Send is not rejectable (already sent or does not exist)",
        )
    return SimpleResponse(ok=True, detail="rejected")


@router.post("/sends/{send_id}/retry", response_model=SimpleResponse)
async def retry_send(
    send_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(EmailAutomationMutateAccess)],
    _body: RetryFailedRequest = RetryFailedRequest(),
) -> SimpleResponse:
    """Reset a ``failed`` send back to ``approved`` so the next dispatch retries it."""

    _ensure_feature_enabled()
    ok = await pipeline.mark_retry_failed(db, send_id, user.id)
    await db.commit()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Send is not retryable (only 'failed' rows can be retried)",
        )
    return SimpleResponse(ok=True, detail="retry queued")


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    window: Annotated[
        Literal["24h", "7d", "30d"],
        Query(description="Aggregation window. 24h=hourly buckets, 7d/30d=daily."),
    ] = "24h",
) -> MetricsResponse:
    """Dashboard aggregations over messages + sends.

    Single call, server-side ``GROUP BY``, no row-level PII on the wire.
    Same gate as :func:`get_receivable_dashboard` (``system_admin`` or
    ``email_agent_access``). Mutating actions (approve / reject / retry) use
    :data:`EmailAutomationMutateAccess`.

    ``EMAIL_AUTOMATION_TEST_MODE`` is reflected only in ``health.test_mode``;
    counts and trends are the same aggregates as in production (same tables).
    """

    metrics = await pipeline.compute_metrics(db, window=window)
    return MetricsResponse(
        window=metrics.window,
        generated_at=metrics.generated_at,
        bucket_granularity=metrics.bucket_granularity,
        health=MetricsHealth(
            enabled=metrics.health.enabled,
            test_mode=metrics.health.test_mode,
            last_message_received_at=metrics.health.last_message_received_at,
            last_send_sent_at=metrics.health.last_send_sent_at,
        ),
        messages_by_status=metrics.messages_by_status,
        sends_by_status=metrics.sends_by_status,
        sends_by_variant=metrics.sends_by_variant,
        sends_skipped_by_reason=metrics.sends_skipped_by_reason,
        sends_failed_by_reason=metrics.sends_failed_by_reason,
        trend=[
            MetricsTrendPoint(
                bucket=p.bucket, sent=p.sent, failed=p.failed, ingested=p.ingested,
            )
            for p in metrics.trend
        ],
        attention=MetricsAttention(
            sends_at_attempt_cap=metrics.attention.sends_at_attempt_cap,
            messages_processed_with_errors_open=(
                metrics.attention.messages_processed_with_errors_open
            ),
            sends_stuck_sending=metrics.attention.sends_stuck_sending,
        ),
    )


@router.get("/receivable-dashboard")
async def get_receivable_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
) -> dict[str, object] | None:
    """Latest receivables workbook snapshot (ingested with weekly `receivables` .xlsx).

    ``all_parties`` is stripped from the response — it is only needed by the
    backend intelligence context and adds ~342 KB the browser never reads.
    The remaining payload is ~93 KB; a simple indexed Postgres query is fast
    enough without a cache layer.
    """
    row = (
        await db.execute(
            select(ReceivableDashboardSnapshot).order_by(
                desc(ReceivableDashboardSnapshot.created_at)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None

    # Strip all_parties — 342 KB the frontend never reads (used only by the
    # backend intelligence / stale-detection context via _ReceivablesCtx).
    payload_for_client = {
        k: v for k, v in (row.payload or {}).items() if k != "all_parties"
    }
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "source_message_id": str(row.source_message_id)
        if row.source_message_id
        else None,
        "payload": payload_for_client,
    }


@router.get(
    "/intelligence/metrics",
    response_model=IntelligenceMetricsResponse,
    deprecated=True,
)
async def get_intelligence_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    window: Annotated[
        Literal["24h", "7d", "30d"],
        Query(description="Rolling window for classified_at timestamps."),
    ] = "30d",
    kind: Annotated[str, Query(description="Intelligence kind slug.")] = "collections_reply",
) -> IntelligenceMetricsResponse:
    """Aggregates over ``gmail_intelligence`` — admin-only (same as pipeline metrics)."""

    m = await compute_intelligence_metrics(db, window=window, kind=kind)

    return IntelligenceMetricsResponse(
        window=m.window,
        generated_at=m.generated_at,
        kind=kind,
        bucket_granularity=m.bucket_granularity,
        total_threads=m.total_threads,
        by_category=m.by_category,
        by_confidence=m.by_confidence,
        low_confidence_count=m.low_confidence_count,
        timeline=[
            IntelligenceTimelinePoint(bucket=p.bucket, count=p.count) for p in m.timeline
        ],
        prior_period=IntelligencePriorPeriodRead(
            total_threads=m.prior_period.total_threads,
            by_category=m.prior_period.by_category,
            by_confidence=m.prior_period.by_confidence,
            low_confidence_count=m.prior_period.low_confidence_count,
        ),
    )


@router.get(
    "/intelligence/threads",
    response_model=IntelligenceThreadsPage,
    deprecated=True,
)
async def list_intelligence_threads(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    kind: Annotated[str, Query()] = "collections_reply",
    window: Annotated[
        Literal["24h", "7d", "30d"],
        Query(description="Rolling window on classified_at — matches intelligence metrics."),
    ] = "30d",
    category: str | None = None,
    search: Annotated[
        str | None,
        Query(
            description="Case-insensitive substring match on thread id, business key, workflow, or variant.",
            max_length=200,
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[
        str | None,
        Query(
            description="Opaque keyset cursor from the previous page's `next_cursor`.",
        ),
    ] = None,
) -> IntelligenceThreadsPage:
    page_size = min(limit, 200)
    fetch = page_size + 1

    classified_since = intelligence_window_start(window)
    stmt = select(GmailIntelligence).where(
        GmailIntelligence.kind == kind,
        GmailIntelligence.classified_at >= classified_since,
    )
    if category:
        stmt = stmt.where(GmailIntelligence.category == category)
    if search and (term := search.strip()):
        esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pat = f"%{esc}%"
        stmt = stmt.where(
            or_(
                GmailIntelligence.gmail_thread_id.ilike(pat, escape="\\"),
                GmailIntelligence.business_key.ilike(pat, escape="\\"),
                GmailIntelligence.workflow_type.ilike(pat, escape="\\"),
                GmailIntelligence.variant.ilike(pat, escape="\\"),
            )
        )
    if cursor:
        try:
            cur_ca, cur_id = decode_thread_cursor(cursor)
        except ThreadCursorError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e
        stmt = stmt.where(
            or_(
                GmailIntelligence.classified_at < cur_ca,
                and_(
                    GmailIntelligence.classified_at == cur_ca,
                    GmailIntelligence.id < cur_id,
                ),
            )
        )
    stmt = stmt.order_by(
        desc(GmailIntelligence.classified_at),
        desc(GmailIntelligence.id),
    ).limit(fetch)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_thread_cursor(last.classified_at, last.id)
    items = [
        IntelligenceThreadRow(
            id=str(r.id),
            gmail_thread_id=r.gmail_thread_id,
            kind=r.kind,
            category=r.category,
            confidence=r.confidence,
            business_key=r.business_key,
            workflow_type=r.workflow_type,
            variant=r.variant,
            classified_at=r.classified_at,
            trigger_message_id=r.trigger_message_id,
            anchor_send_id=str(r.anchor_send_id) if r.anchor_send_id else None,
        )
        for r in page_rows
    ]
    return IntelligenceThreadsPage(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/intelligence/receivables", response_model=IntelligenceReceivablesResponse)
async def get_intelligence_receivables(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    business_unit: str | None = Query(None, description="Filter by Business Unit name."),
    kind: str = Query("collections_reply", description="GmailIntelligence kind to match against."),
    reply_category: str | None = Query(None, description="Filter by reply category slug."),
    email_sent: str | None = Query(None, description="Filter by email sent status: 'yes', 'no', or omit for all."),
    search: str | None = Query(None, description="Case-insensitive substring match on party name or HANA code."),
    window_days: int = Query(
        REPLY_TRACKER_WINDOW_DAYS,
        description="Reply Tracker view window in days (7, 14 or 30; default 7).",
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Page size."),
    offset: int = Query(default=0, ge=0, description="Number of items to skip."),
) -> IntelligenceReceivablesResponse:
    """Merged view of latest receivables snapshot + AI intelligence results.

    Send-anchored windowing (``window_days`` = 7 / 14 / 30): ``Email Sent At`` is set
    only when the party's latest send is in-window, and ``Reply Received At`` /
    ``Reply Category`` only when that in-window email got a reply.  So "Email Sent =
    Yes" reconciles with the Emails Delivered card and a reply-category filter with
    the Reply Breakdown card.
    """

    window_days = normalize_window_days(window_days)
    ctx = await _load_receivables_ctx(db, business_unit=business_unit, kind=kind)
    if ctx is None:
        return IntelligenceReceivablesResponse(
            items=[], business_units=[], total=0, limit=limit, offset=offset, has_more=False
        )
    if not ctx.party_map:
        return IntelligenceReceivablesResponse(
            items=[], business_units=ctx.bu_options, total=0, limit=limit, offset=offset, has_more=False
        )

    since = reply_tracker_window_start(window_days)
    results = _windowed_receivable_rows(ctx, since=since)
    results.sort(key=lambda x: x.total_overdue_lakh, reverse=True)

    if search and (term := search.strip().lower()):
        results = [
            r for r in results
            if term in r.party_name.lower() or term in r.hana_code.lower()
        ]
    if reply_category:
        results = [r for r in results if r.reply_category == reply_category]
    if email_sent == "yes":
        results = [r for r in results if r.email_sent_at is not None]
    elif email_sent == "no":
        results = [r for r in results if r.email_sent_at is None]

    total = len(results)
    page = results[offset : offset + limit]
    return IntelligenceReceivablesResponse(
        items=page,
        business_units=ctx.bu_options,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.get("/intelligence/receivables/export")
async def export_intelligence_receivables(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    kind: str = Query("collections_reply", description="GmailIntelligence kind to match against."),
    window_days: int | None = Query(
        None,
        description=(
            "Omit for 'Export All Data' (all-time, all BUs).  Set to 7 / 14 / 30 for "
            "the 'current view' export — send-anchored to that window, scoped to "
            "``business_unit``."
        ),
    ),
    business_unit: str | None = Query(
        None, description="BU scope for the windowed current-view export (ignored when window_days is omitted)."
    ),
) -> StreamingResponse:
    """Export the party-level receivables dataset as an XLSX file.

    Two modes:
      * ``window_days`` omitted → **Export All Data**: all BUs, all-time
        ``Email Sent At`` / ``Reply Received At`` (unchanged behaviour).
      * ``window_days`` set → **current view**: send-anchored to the window and scoped
        to ``business_unit`` — the exact rows the party-level detail table shows.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    windowed = window_days is not None
    if windowed:
        window_days = normalize_window_days(window_days)

    ctx = await _load_receivables_ctx(
        db, business_unit=business_unit if windowed else None, kind=kind
    )

    # Total reminder emails sent per party (COUNT DISTINCT period_key, status in
    # approved/rendered/sent, non-test).  Queried separately because _ReceivablesCtx
    # does not carry this — adding it to the cached ctx would bloat the Redis key for
    # callers that never need send counts.
    from app.services.receivable_dashboard import (
        load_reminder_send_rollup_for_receivable_dashboard,
        _normalize_hana_key_for_send_lookup,
    )
    send_counts, _ = await load_reminder_send_rollup_for_receivable_dashboard(db)

    rows: list[IntelligenceReceivableRow] = []
    if ctx and ctx.party_map:
        if windowed:
            rows = _windowed_receivable_rows(
                ctx, since=reply_tracker_window_start(window_days)
            )
        else:
            rows = [
                IntelligenceReceivableRow(
                    hana_code=code,
                    party_name=info["name"],
                    business_unit=info["bu"],
                    total_overdue_lakh=info["overdue"],
                    email_sent_at=ctx.latest_send[code].sent_at if code in ctx.latest_send else None,
                    reply_received_at=ctx.latest_intel[code].classified_at if code in ctx.latest_intel else None,
                    reply_category=ctx.latest_intel[code].category if code in ctx.latest_intel else None,
                    gmail_thread_id=(
                        ctx.latest_send[code].gmail_thread_id if code in ctx.latest_send
                        else ctx.latest_intel[code].gmail_thread_id if code in ctx.latest_intel
                        else None
                    ),
                    email_subject=ctx.latest_send[code].rendered_subject if code in ctx.latest_send else None,
                )
                for code, info in ctx.party_map.items()
            ]
            rows.sort(key=lambda x: x.total_overdue_lakh, reverse=True)

    # ── Build workbook ────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Receivables"

    headers = [
        "HANA Code",
        "Party Name",
        "Business Unit",
        "Total Overdue (₹ L)",
        "Reminder Emails Sent",
        "Email Sent At",
        "Reply Received At",
        "Reply Category",
        "Gmail Thread ID",
        "Email Subject",
    ]

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="1B2A4A")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=False)

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    ws.freeze_panes = "A2"

    # Date cells must hold Python date objects (not strings) so Excel treats
    # them as native dates — this enables the "before / after" filter.
    # DD-MMM-YYYY (e.g. 07-May-2025) is unambiguous across locales.
    date_fmt = "DD-MMM-YYYY"
    num_fmt = "0.00"

    for row_idx, r in enumerate(rows, start=2):
        ws.cell(row=row_idx, column=1, value=r.hana_code)
        ws.cell(row=row_idx, column=2, value=r.party_name)
        ws.cell(row=row_idx, column=3, value=r.business_unit)
        amount_cell = ws.cell(row=row_idx, column=4, value=r.total_overdue_lakh)
        amount_cell.number_format = num_fmt

        norm_code = _normalize_hana_key_for_send_lookup(r.hana_code or "")
        ws.cell(row=row_idx, column=5, value=send_counts.get(norm_code, 0))

        sent_cell = ws.cell(
            row=row_idx, column=6,
            value=r.email_sent_at.date() if r.email_sent_at else None,
        )
        if r.email_sent_at:
            sent_cell.number_format = date_fmt

        reply_cell = ws.cell(
            row=row_idx, column=7,
            value=r.reply_received_at.date() if r.reply_received_at else None,
        )
        if r.reply_received_at:
            reply_cell.number_format = date_fmt

        ws.cell(row=row_idx, column=8, value=r.reply_category)
        ws.cell(row=row_idx, column=9, value=r.gmail_thread_id)
        ws.cell(row=row_idx, column=10, value=r.email_subject)

    # Auto-size columns (cap at 60)
    for col_idx, header in enumerate(headers, start=1):
        max_len = len(header)
        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 62)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    suffix = f"_{window_days}d" if windowed else ""
    filename = f"receivables{suffix}_{date.today().isoformat()}.xlsx"
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/intelligence/summary", response_model=IntelligenceSummaryResponse)
async def get_intelligence_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    business_unit: str | None = Query(
        None, description="Filter by Business Unit (None / 'All' = whole book)."
    ),
    kind: str = Query("collections_reply", description="GmailIntelligence kind slug."),
    window_days: int = Query(
        REPLY_TRACKER_WINDOW_DAYS,
        description="Reply Tracker view window in days (7, 14 or 30; default 7).",
    ),
) -> IntelligenceSummaryResponse:
    """Executive summary aggregates for the Collections Outreach dashboard.

    Send-anchored windowing: the ``window_days`` view (7 / 14 / 30) filters on the
    email **send** date.  "Emails Delivered" and "Total Overdue (In Campaign)" cover
    parties emailed in-window; "Replies Received", "Reply Breakdown" and "Total
    Overdue (Replies)" cover the in-window-emailed parties that replied — a reply to
    an email sent before the window is not counted.  Total Overdue (Book) is never
    windowed.  All sections derive from the same per-party ctx the party-level detail
    table / current-view export use, so they reconcile.
    """
    now = datetime.now(timezone.utc)
    window_days = normalize_window_days(window_days)
    empty = IntelligenceSummaryResponse(
        business_unit=business_unit,
        total_overdue_book_lakh=0.0,
        total_overdue_in_campaign_lakh=0.0,
        total_overdue_replies_lakh=0.0,
        emails_delivered=0,
        replies_received=0,
        reply_rate=0.0,
        category_breakdown=[],
        window_days=window_days,
        snapshot_created_at=None,
        generated_at=now,
    )

    ctx = await _load_receivables_ctx(db, business_unit=business_unit, kind=kind)
    if ctx is None:
        return empty

    pm = ctx.party_map  # hana_code → {name, overdue, bu} — may be empty

    # ── Total Overdue (Book) — kpi_lakh is the authoritative source ───────────
    # kpi_lakh is pre-computed during ingest from every Excel row, so it is:
    if business_unit and business_unit != "All":
        kpi = ctx.kpi_lakh_by_bu.get(business_unit, {})
    else:
        kpi = ctx.kpi_lakh_all
    total_overdue_book_lakh = round(
        sum(v for k, v in kpi.items() if kpi_key_is_due_ageing(k)), 1
    )
    # total_overdue_replies_lakh is derived from cat_totals (built below) so
    # that it equals the sum of the per-category amounts shown in the breakdown
    # table.  Computing it separately from pm keys produced a rounding divergence
    # (sum of rounded parts ≠ rounded sum).  Placeholder 0.0 is replaced after
    # cat_totals is populated.
    total_overdue_replies_lakh: float = 0.0

    # ── Engagement metrics (send-anchored, trailing ``window_days``) ──────────
    # Base = parties emailed in-window (latest status='sent' send.sent_at >= since).
    # "Emails Delivered" / "Total Overdue (In Campaign)" cover that base; replies /
    # breakdown cover the subset of that base which also has a classified reply —
    # a reply to an email sent before the window is not counted.  Derived from the
    # same per-party ctx as the party-level detail table, so "Emails Delivered"
    # equals the rows the table marks "Email Sent = Yes" and the breakdown equals a
    # category filter on that table.
    since = reply_tracker_window_start(window_days)

    in_window_codes = [
        code for code in pm if _send_in_window(ctx.latest_send.get(code), since=since)
    ]
    emails_delivered = len(in_window_codes)
    total_overdue_in_campaign_lakh = round(
        sum(pm[code]["overdue"] for code in in_window_codes), 1
    )

    # replies: in-window-emailed parties that also have a classified reply.
    latest_intel_rows = [
        intel
        for code in in_window_codes
        if (intel := ctx.latest_intel.get(code)) is not None
    ]

    replies_received = len(latest_intel_rows)
    reply_rate = round((replies_received / emails_delivered * 100), 1) if emails_delivered else 0.0

    # ── Category breakdown ────────────────────────────────────────────────────
    cat_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for intel in latest_intel_rows:
        cat = intel.category or "unknown"
        cat_totals[cat]["count"] += 1
        # Overdue amount: use snapshot value when the key matches, else 0.
        cat_totals[cat]["amount"] += pm.get(intel.business_key, {}).get("overdue", 0.0)

    # Round each category amount to 1 dp now so the stored value equals exactly
    # what the frontend displays (toFixed(1)).  Summing already-rounded values
    # for the total means the Total row will always equal the sum of the visible
    # rows — no "sum of rounded parts ≠ rounded sum" drift possible.
    category_breakdown = [
        CategoryBreakdownRow(
            category=cat,
            reply_count=v["count"],
            pct_of_replies=round(v["count"] / replies_received * 100, 1) if replies_received else 0.0,
            amount_due_lakh=round(v["amount"], 2),
        )
        for cat, v in sorted(cat_totals.items(), key=lambda x: -x[1]["count"])
    ]

    # Total is the sum of the already-rounded per-category values — guaranteed
    # to match what the frontend shows when it adds up the column.
    total_overdue_replies_lakh = sum(row.amount_due_lakh for row in category_breakdown)

    return IntelligenceSummaryResponse(
        business_unit=business_unit,
        total_overdue_book_lakh=total_overdue_book_lakh,
        total_overdue_in_campaign_lakh=total_overdue_in_campaign_lakh,
        total_overdue_replies_lakh=total_overdue_replies_lakh,
        emails_delivered=emails_delivered,
        replies_received=replies_received,
        reply_rate=reply_rate,
        category_breakdown=category_breakdown,
        window_days=window_days,
        snapshot_created_at=ctx.snapshot_created_at,
        generated_at=now,
    )


@router.get("/intelligence/kam-metrics", response_model=KamDashboardResponse)
async def get_kam_dashboard_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
    window: Annotated[
        Literal["7d"],
        Query(description="Rolling window on classified_at. KAM dashboard is L7D only."),
    ] = "7d",
) -> KamDashboardResponse:
    """Per-KAM reply-tracking metrics for the KAM Dashboard — admin-only.

    Reply rate / accuracy / avg reply time are aggregated from ``gmail_intelligence``
    rows of kind ``kam_reply_accuracy`` (written by Path K). ``total_overdue_lakh`` is the
    total overdue ₹ (lakh) across the accounts the KAM manages in the latest snapshot.
    """
    from app.email_automation.kam_directory import load_kam_directory

    now = datetime.now(timezone.utc)
    directory = await asyncio.to_thread(load_kam_directory)
    aggs = await aggregate_kam_reply_metrics(db, window=window)

    # Overdue ₹ (lakh, TDS-adjusted) per HANA code from the latest receivables snapshot.
    overdue_by_code: dict[str, float] = {}
    if not directory.is_empty:
        ctx = await _load_receivables_ctx(db, business_unit=None, kind="collections_reply")
        if ctx is not None:
            overdue_by_code = {
                code: float(info.get("overdue") or 0.0)
                for code, info in ctx.party_map.items()
            }

    # Union of KAMs in the directory and KAMs with scored activity, so leadership
    # sees everyone (zeros for inactive KAMs) and no orphaned agg is dropped.
    keys = set(directory.kams_by_key) | set(aggs)
    rows: list[KamDashboardRow] = []
    for key in keys:
        kam = directory.kams_by_key.get(key)
        agg = aggs.get(key)
        name = (kam.name if kam else "") or (agg.kam_name if agg else "") or key
        managed = directory.hanas_for_key(key)
        total_overdue_lakh = round(sum(overdue_by_code.get(h, 0.0) for h in managed), 2)
        rows.append(
            KamDashboardRow(
                kam_name=name,
                total_overdue_lakh=total_overdue_lakh,
                reply_rate=agg.reply_rate if agg else 0.0,
                accuracy_rate=agg.accuracy_rate if agg else 0.0,
                avg_reply_seconds=agg.avg_reply_seconds if agg else None,
                threads_scored=agg.threads_scored if agg else 0,
                client_replied_threads=agg.client_replied if agg else 0,
                no_client_reply_threads=agg.no_client_reply_threads if agg else 0,
            )
        )

    rows.sort(key=lambda r: (-r.total_overdue_lakh, r.kam_name.lower()))
    return KamDashboardResponse(window=window, generated_at=now, rows=rows)


@router.get(
    "/intelligence/thread/{thread_id}",
    response_model=EmailThreadResponse,
    summary="Fetch a Gmail thread by thread ID",
)
async def get_email_thread(
    thread_id: str,
    _user: Annotated[User, Depends(EmailAutomationMetricsAccess)],
) -> EmailThreadResponse:
    """Fetch all messages in a Gmail thread and return parsed sender/body content.

    Uses the service-account Gmail client already configured for email automation.
    The ``thread_id`` must be a Gmail ``threadId`` (stored in
    ``email_automation_sends.gmail_thread_id``).
    """
    from app.email_automation import gmail_sa

    try:
        raw = await asyncio.to_thread(gmail_sa.fetch_thread_full, thread_id)

        # Defensive completeness check: Gmail ``threads.get`` should return every
        # message in the thread, regardless of age, but for very old / very large
        # conversations we independently enumerate message ids and backfill any
        # missing resources so the UI never silently drops older messages.
        thread_messages = raw.get("messages") or []
        thread_message_ids = {
            str(msg.get("id") or "").strip()
            for msg in thread_messages
            if str(msg.get("id") or "").strip()
        }
        listed_ids = await asyncio.to_thread(gmail_sa.list_thread_message_ids, thread_id)
        if listed_ids:
            missing_ids = [mid for mid in listed_ids if mid not in thread_message_ids]
            if missing_ids:
                missing_messages = await asyncio.gather(
                    *(asyncio.to_thread(gmail_sa.fetch_message_full, mid) for mid in missing_ids)
                )
                raw["messages"] = [*thread_messages, *missing_messages]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Gmail API error — could not fetch thread") from exc

    messages: list[EmailThreadMessage] = []
    for msg in raw.get("messages") or []:
        payload = msg.get("payload") or {}
        headers = {
            h.get("name", ""): h.get("value", "")
            for h in payload.get("headers") or []
        }
        body = gmail_sa.message_resource_plain_text(msg)
        date_ms = gmail_sa.message_resource_internal_date_ms(msg)
        messages.append(
            EmailThreadMessage(
                message_id=msg.get("id", ""),
                from_addr=headers.get("From", ""),
                to_addr=headers.get("To", ""),
                cc_addr=headers.get("Cc", ""),
                subject=headers.get("Subject", ""),
                date_ms=date_ms,
                body=body,
            )
        )

    # Sort oldest-first so the conversation reads chronologically (top = oldest).
    messages.sort(key=lambda m: m.date_ms, reverse=False)

    return EmailThreadResponse(thread_id=thread_id, messages=messages)
