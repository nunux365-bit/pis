"""Role-scoped approval + catalog agent aggregates for /api/analytics and /api/dashboard.

Single implementation avoids drift between the analytics page and the home dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus, CatalogAgent, User
from app.security.rbac import scope_approval_assignee


def _decided_statuses() -> list[str]:
    return [
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.REJECTED.value,
        ApprovalStatus.AUTO_APPROVED.value,
    ]


async def build_analytics_summary(db: AsyncSession, user: User) -> dict:
    since = datetime.now(UTC) - timedelta(days=30)

    pending_q = select(func.count()).select_from(Approval).where(
        Approval.status == ApprovalStatus.PENDING.value,
    )
    pending_q = scope_approval_assignee(pending_q, user)
    pending = int((await db.execute(pending_q)).scalar() or 0)

    async def scoped_count(*filters) -> int:
        q = select(func.count()).select_from(Approval).where(
            Approval.created_at >= since,
            *filters,
        )
        q = scope_approval_assignee(q, user)
        return int((await db.execute(q)).scalar() or 0)

    total_d = await scoped_count(Approval.status.in_(_decided_statuses()))
    auto_c = await scoped_count(Approval.status == ApprovalStatus.AUTO_APPROVED.value)
    automation_rate = (auto_c / total_d) if total_d > 0 else 0.0

    dept_rows: list[dict] = []
    scope = user.data_scope()
    if scope == "admin":
        q = (
            select(User.department, func.count(User.id))
            .group_by(User.department)
            .order_by(User.department)
        )
        for dept, _cnt in (await db.execute(q)).all():
            sub = select(User.id).where(User.department == dept)
            ap_total = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.assignee_user_id.in_(sub),
                            Approval.created_at >= since,
                        )
                    )
                ).scalar()
                or 0
            )
            auto_t = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.assignee_user_id.in_(sub),
                            Approval.created_at >= since,
                            Approval.status == ApprovalStatus.AUTO_APPROVED.value,
                        )
                    )
                ).scalar()
                or 0
            )
            pct = round(100 * auto_t / ap_total) if ap_total else 0
            dept_rows.append(
                {"dept": dept or "Unknown", "auto_pct": pct, "tasks": ap_total}
            )
    elif scope == "dept":
        sub = select(User.id).where(User.department == user.department)
        ap_total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.assignee_user_id.in_(sub),
                        Approval.created_at >= since,
                    )
                )
            ).scalar()
            or 0
        )
        auto_t = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.assignee_user_id.in_(sub),
                        Approval.created_at >= since,
                        Approval.status == ApprovalStatus.AUTO_APPROVED.value,
                    )
                )
            ).scalar()
            or 0
        )
        pct = round(100 * auto_t / ap_total) if ap_total else 0
        dept_rows.append(
            {"dept": user.department, "auto_pct": pct, "tasks": ap_total}
        )
    else:
        ap_total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.assignee_user_id == user.id,
                        Approval.created_at >= since,
                    )
                )
            ).scalar()
            or 0
        )
        auto_t = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.assignee_user_id == user.id,
                        Approval.created_at >= since,
                        Approval.status == ApprovalStatus.AUTO_APPROVED.value,
                    )
                )
            ).scalar()
            or 0
        )
        pct = round(100 * auto_t / ap_total) if ap_total else 0
        dept_rows.append({"dept": user.department, "auto_pct": pct, "tasks": ap_total})

    total_agents = int(
        (await db.execute(select(func.count()).select_from(CatalogAgent))).scalar() or 0
    )
    active_agents_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CatalogAgent)
                .where(CatalogAgent.status == "active")
            )
        ).scalar()
        or 0
    )
    active_agents_label = (
        f"{active_agents_n}/{total_agents}" if total_agents else "0/0"
    )

    return {
        "pending_approvals": pending,
        "automation_rate_30d": round(automation_rate, 4),
        "active_agents": active_agents_label,
        "department_automation": dept_rows,
    }
