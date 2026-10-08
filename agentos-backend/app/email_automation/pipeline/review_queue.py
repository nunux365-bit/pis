"""Optional HITL transitions — approve / reject / retry a single send row.

These endpoints remain usable when
:attr:`settings.email_automation_require_approval` is ``True`` (opt-in manual
gate) and for ad-hoc ops actions (``/retry`` on a ``failed`` row). With the
default ``require_approval=False`` they act only on ``rendered`` /
``skipped`` / ``failed`` rows — the legacy ``needs_review`` status is no
longer produced by the pipeline.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationSend

from ._shared import jsonable, now_utc


async def mark_approved(
    db: AsyncSession,
    send_id: UUID,
    approver_id: UUID | None,
    *,
    note: str | None = None,
) -> bool:
    """Move a ``rendered`` (or legacy ``needs_review``) row to ``approved``."""

    row = (
        await db.execute(
            select(EmailAutomationSend)
            .where(
                EmailAutomationSend.id == send_id,
                EmailAutomationSend.status == "rendered",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    reasons = list(row.review_reasons or [])
    reasons.append(
        {
            "code": "approved",
            "by": str(approver_id) if approver_id else "",
            "at": now_utc().isoformat(),
            "note": note or "",
        }
    )
    row.review_reasons = jsonable(reasons)
    row.status = "approved"
    row.approved_by = approver_id
    row.approved_at = now_utc()
    return True


async def mark_rejected(
    db: AsyncSession,
    send_id: UUID,
    reason: str,
    *,
    actor_id: UUID | None = None,
) -> bool:
    """Move a ``rendered`` or ``approved`` row to ``skipped`` with an audit note."""

    row = (
        await db.execute(
            select(EmailAutomationSend)
            .where(
                EmailAutomationSend.id == send_id,
                EmailAutomationSend.status.in_(("rendered", "approved")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    reasons = list(row.review_reasons or [])
    reasons.append(
        {
            "code": "rejected",
            "by": str(actor_id) if actor_id else "",
            "at": now_utc().isoformat(),
            "detail": reason,
        }
    )
    row.review_reasons = jsonable(reasons)
    row.status = "skipped"
    return True


async def mark_retry_failed(
    db: AsyncSession, send_id: UUID, actor_id: UUID | None
) -> bool:
    """Move a ``failed`` row back to ``approved`` so the next dispatch retries.

    Refuses to reset rows that already burned their attempt budget (F3) — ops
    must inspect manually and flip ``send_attempt_count`` back to zero if
    they want further auto-retry.
    """

    row = (
        await db.execute(
            select(EmailAutomationSend)
            .where(
                EmailAutomationSend.id == send_id,
                EmailAutomationSend.status == "failed",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    max_attempts = int(settings.email_automation_send_max_attempts or 5)
    attempts = row.send_attempt_count or 0
    if attempts >= max_attempts:
        reasons = list(row.review_reasons or [])
        reasons.append(
            {
                "code": "retry_refused_attempt_cap",
                "by": str(actor_id) if actor_id else "",
                "at": now_utc().isoformat(),
                "detail": (
                    f"attempts={attempts} ≥ cap={max_attempts}; reset "
                    "send_attempt_count to 0 before retrying"
                ),
                "human_message": (
                    "This send has already failed the maximum number of times "
                    "allowed by the retry budget."
                ),
                "suggested_action": (
                    "Check the underlying error, fix the root cause, then "
                    "manually zero ``send_attempt_count`` via SQL before retrying."
                ),
            }
        )
        row.review_reasons = jsonable(reasons)
        return False

    reasons = list(row.review_reasons or [])
    reasons.append(
        {
            "code": "retry_requested",
            "by": str(actor_id) if actor_id else "",
            "at": now_utc().isoformat(),
            "attempts_so_far": attempts,
        }
    )
    row.review_reasons = jsonable(reasons)
    row.status = "approved"
    return True


__all__ = ["mark_approved", "mark_rejected", "mark_retry_failed"]
