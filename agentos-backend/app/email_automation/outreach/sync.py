"""Daily GSheet → Postgres sync for outreach leads."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutreachLead
from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.gsheet_client import OutreachSheetClient
from app.email_automation.outreach.issue_detector import (
    BLOCKING_ISSUES,
    IssueCode,
    detect_issues,
)

log = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[;&]")
_SYSTEM_COL_M = {"sent", "not sent", ""}


def _extract_primary_email(raw: str | None) -> str:
    if not raw:
        return ""
    first = _SPLIT_RE.split(raw)[0].strip()
    return first


def _lead_fields_changed(existing: OutreachLead, row: dict[str, Any]) -> bool:
    """True if any non-status sheet field differs from the stored lead."""
    checks = [
        ("spoc", "SPOC"),
        ("account_name", "Account name"),
        ("designation", "Designation"),
        ("email_id", "Email ID"),
        ("industry", "Industry"),
        ("target_service_1", "Target service 1"),
        ("target_service_2", "Target service 2"),
        ("bd_lead_email", "BD lead email ID"),
        ("bd_head_email", "BD head email ID"),
    ]
    for attr, col in checks:
        if str(getattr(existing, attr) or "") != str(row.get(col) or ""):
            return True
    return False


def reconcile_rows(
    *,
    sheet_rows: list[dict[str, Any]],
    existing_leads: dict[str, Any],  # keyed by primary_email
    campaign_name: str,
    tab_name: str,
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Pure reconciliation — returns {to_insert, to_update} dicts. No DB I/O."""
    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []

    # Detect duplicates within this sheet snapshot
    email_counts: dict[str, int] = {}
    for row in sheet_rows:
        pe = _extract_primary_email(row.get("Email ID"))
        if pe:
            email_counts[pe] = email_counts.get(pe, 0) + 1
    duplicate_emails = {e for e, c in email_counts.items() if c > 1}

    seen_emails: set[str] = set()

    for row in sheet_rows:
        primary_email = _extract_primary_email(row.get("Email ID"))
        col_m_raw = (row.get("Status") or "").strip()
        source = (row.get("Source of email ID") or row.get("Source") or "").strip() or None
        is_duplicate = primary_email in duplicate_emails

        issue_inputs = {
            "primary_email": primary_email,
            "email_id": row.get("Email ID") or "",
            "spoc": row.get("SPOC") or "",
            "bd_lead_email": row.get("BD lead email ID") or "",
            "bd_head_email": row.get("BD head email ID") or "",
            "col_m_raw": col_m_raw,
            "is_sent": False,
            "lead_data_changed": False,
            "is_removed_from_sheet": False,
            "is_duplicate_email": is_duplicate,
            "send_attempt_count": 0,
            "max_attempts": 5,
        }

        if primary_email not in existing_leads:
            # New row
            if is_duplicate:
                status = "duplicate"
            elif col_m_raw.lower() == "sent":
                status = "skipped"
            else:
                status = "hold"  # will be promoted to pending after issue detection if no blocking issues

            issue_inputs["is_sent"] = status == "sent"
            issues = [c.value for c in detect_issues(issue_inputs)]

            # promote hold→pending if no blocking issues
            if status == "hold":
                if not any(i in BLOCKING_ISSUES for i in issues):
                    status = "pending"

            to_insert.append({
                "campaign_name": campaign_name,
                "gsheet_row_index": row["_row_index"],
                "sr_no": _safe_int(row.get("Sr. No.")),
                "bd_lead_name": row.get("Name of BD lead"),
                "account_name": row.get("Account name"),
                "spoc": row.get("SPOC"),
                "designation": row.get("Designation"),
                "email_id": row.get("Email ID"),
                "primary_email": primary_email,
                "industry": row.get("Industry"),
                "company_size": _safe_int(row.get("Company size - employee count")),
                "target_service_1": row.get("Target service 1"),
                "target_service_2": row.get("Target service 2"),
                "bd_lead_email": row.get("BD lead email ID"),
                "bd_head_email": row.get("BD head email ID"),
                "source": source,
                "tab_name": tab_name,
                "status": status,
                "skipped_reason": "manually_handled" if col_m_raw.lower() == "sent" else None,
                "issues": issues,
                "gsheet_synced_at": now,
            })
        else:
            existing = existing_leads[primary_email]
            data_changed = _lead_fields_changed(existing, row)
            is_sent = existing.status == "sent"

            issue_inputs["is_sent"] = is_sent
            issue_inputs["lead_data_changed"] = data_changed
            issue_inputs["send_attempt_count"] = existing.send_attempt_count or 0
            issues = [c.value for c in detect_issues(issue_inputs)]

            update: dict[str, Any] = {
                "primary_email": primary_email,
                "gsheet_row_index": row["_row_index"],
                "sr_no": _safe_int(row.get("Sr. No.")),
                "bd_lead_name": row.get("Name of BD lead"),
                "account_name": row.get("Account name"),
                "spoc": row.get("SPOC"),
                "designation": row.get("Designation"),
                "email_id": row.get("Email ID"),
                "industry": row.get("Industry"),
                "company_size": _safe_int(row.get("Company size - employee count")),
                "target_service_1": row.get("Target service 1"),
                "target_service_2": row.get("Target service 2"),
                "bd_lead_email": row.get("BD lead email ID"),
                "bd_head_email": row.get("BD head email ID"),
                "source": source,
                "tab_name": tab_name,
                "issues": issues,
                "gsheet_synced_at": now,
            }

            # Status transitions — Postgres wins on send state
            if existing.status in ("sent", "failed", "skipped", "removed_from_sheet"):
                pass  # preserve terminal status
            elif is_duplicate:
                update["status"] = "duplicate"
            elif col_m_raw.lower() == "sent" and existing.status == "pending":
                update["status"] = "skipped"
                update["skipped_reason"] = "manually_handled"
            elif existing.status == "hold":
                if not any(i in BLOCKING_ISSUES for i in issues):
                    update["status"] = "pending"
            elif existing.status == "pending":
                if any(i in BLOCKING_ISSUES for i in issues):
                    update["status"] = "hold"

            # Clear acknowledgement if a new issue appeared
            if issues and existing.issues_acknowledged_at is not None:
                old_issues = set(existing.issues or [])
                new_issues = set(issues)
                if new_issues - old_issues:
                    update["issues_acknowledged_at"] = None

            to_update.append(update)
        seen_emails.add(primary_email)

    # Rows in DB but missing from sheet → removed_from_sheet
    # Only mark removed if not in a terminal send state (prevents false-positives on tab rotation)
    for email, existing in existing_leads.items():
        if email not in seen_emails and existing.status not in ("sent", "failed", "skipped", "removed_from_sheet"):
            to_update.append({
                "primary_email": email,
                "status": "removed_from_sheet",
                "issues": [IssueCode.REMOVED_FROM_SHEET.value],
                "gsheet_synced_at": now,
            })

    return {"to_insert": to_insert, "to_update": to_update}


async def ingest_campaign_sheet(cfg: CampaignConfig) -> dict[str, Any]:
    """Read prospect rows from the monthly GSheet tab.

    Returns a dict with keys: campaign, tab_name, rows, error.
    Never raises — errors are captured in the ``error`` field.
    """
    tab_name = cfg.tab_name or f"{cfg.campaign_name}_{datetime.now(timezone.utc).strftime('%Y_%m')}"
    client = OutreachSheetClient(
        spreadsheet_id=cfg.gsheet_id,
        tab_name=tab_name,
    )

    # Skip gracefully if tab doesn't exist yet
    try:
        existing_tabs = await asyncio.to_thread(client.list_tab_names)
        if tab_name not in existing_tabs:
            log.info("outreach ingest [%s]: tab %r not found, skipping", cfg.campaign_name, tab_name)
            return {"campaign": cfg.campaign_name, "tab_name": tab_name, "rows": [], "error": None}
    except Exception:
        log.warning("outreach ingest [%s]: could not list tabs, proceeding", cfg.campaign_name)

    try:
        rows = await asyncio.to_thread(client.read_prospect_rows)
    except Exception as exc:
        log.exception("outreach ingest [%s]: failed to read rows", cfg.campaign_name)
        return {"campaign": cfg.campaign_name, "tab_name": tab_name, "rows": [], "error": str(exc)}

    return {"campaign": cfg.campaign_name, "tab_name": tab_name, "rows": rows, "error": None}


async def reconcile_campaign_db(
    db: AsyncSession,
    cfg: CampaignConfig,
    ingest_data: dict[str, Any],
) -> dict[str, int]:
    """Reconcile ingest data with Postgres. Returns {inserted, updated}.

    Expects ``ingest_data`` as returned by :func:`ingest_campaign_sheet`.
    """
    if ingest_data.get("error") or not ingest_data.get("rows"):
        return {"inserted": 0, "updated": 0}

    tab_name: str = ingest_data["tab_name"]
    sheet_rows: list[dict[str, Any]] = ingest_data["rows"]

    stmt = select(OutreachLead).where(OutreachLead.campaign_name == cfg.campaign_name)
    result = await db.execute(stmt)
    existing_leads: dict[str, OutreachLead] = {
        lead.primary_email: lead
        for lead in result.scalars().all()
        if lead.primary_email
    }

    now = datetime.now(timezone.utc)
    plan = reconcile_rows(
        sheet_rows=sheet_rows,
        existing_leads=existing_leads,
        campaign_name=cfg.campaign_name,
        tab_name=tab_name,
        now=now,
    )

    inserted = 0
    for row_data in plan["to_insert"]:
        insert_stmt = insert(OutreachLead).values(**row_data)
        insert_stmt = insert_stmt.on_conflict_do_nothing()
        await db.execute(insert_stmt)
        inserted += 1

    updated = 0
    for update_data in plan["to_update"]:
        email = update_data.pop("primary_email")
        lead = existing_leads.get(email)
        if lead:
            for k, v in update_data.items():
                setattr(lead, k, v)
            updated += 1

    await db.commit()
    log.info(
        "outreach reconcile [%s]: inserted=%d updated=%d",
        cfg.campaign_name, inserted, updated,
    )
    return {"inserted": inserted, "updated": updated}


async def sync_campaign(db: AsyncSession, cfg: CampaignConfig) -> dict[str, int]:
    """Fetch GSheet, reconcile, upsert into Postgres. Returns stats dict."""
    ingest_data = await ingest_campaign_sheet(cfg)
    return await reconcile_campaign_db(db, cfg, ingest_data)


def _safe_int(val: Any) -> int | None:
    try:
        return int(float(str(val))) if val not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None
