"""Weekly outreach dispatch engine."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutreachLead
from app.email_automation import gmail_sa
from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.gsheet_client import OutreachSheetClient, parse_address_list

log = logging.getLogger(__name__)

_TEMPLATES_BASE = Path(__file__).parent / "templates"

CHW_OUTREACH_PROMPT_VERSION = "chw_outreach_reply_v1"

_MAX_LEADS_PER_RUN = 300  # safety cap per weekly run


def _jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATES_BASE)), autoescape=True)


def render_email(*, cfg: CampaignConfig, spoc_name: str) -> tuple[str, str]:
    """Return (subject, body_html) for one lead."""
    env = _jinja_env()
    tmpl = env.get_template(cfg.body_template_path)
    html = tmpl.render(spoc_name=spoc_name or "")
    return cfg.subject, html


def _parse_recipients(
    email_id: str | None,
    bd_lead_email: str | None,
    bd_head_email: str | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (to, cc, bcc) address lists."""
    to = parse_address_list(email_id)
    cc = parse_address_list(bd_lead_email)
    bcc = parse_address_list(bd_head_email)
    return to, cc, bcc


def _load_attachment(cfg: CampaignConfig) -> tuple[bytes, str, str] | None:
    """Load attachment bytes if configured. Returns (data, mime_type, filename)."""
    if not cfg.attachment_path:
        return None
    path = Path(__file__).parent / "attachments" / cfg.attachment_path
    if not path.exists():
        log.warning("outreach: attachment not found: %s", path)
        return None
    return path.read_bytes(), "application/pdf", path.name


async def dispatch_campaign(
    db: AsyncSession,
    cfg: CampaignConfig,
) -> tuple[dict[str, int], list[tuple[str, int, str, str]]]:
    """Claim and send pending leads for one campaign.

    Returns:
        (stats_dict, writeback_updates) where writeback_updates is a list of
        (tab_name, row_idx, status, ts) tuples ready for writeback_dispatch_status.
    """
    stmt = (
        select(OutreachLead)
        .where(
            OutreachLead.campaign_name == cfg.campaign_name,
            OutreachLead.status == "pending",
            OutreachLead.issues == [],
        )
        .limit(_MAX_LEADS_PER_RUN)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    leads = result.scalars().all()

    if not leads:
        return {"sent": 0, "failed": 0, "skipped": 0}, []

    attachment = _load_attachment(cfg)
    attach_list = [attachment] if attachment else None

    sent, failed, skipped = 0, 0, 0
    writeback_updates: list[tuple[str, int, str, str]] = []  # (tab_name, row_idx, status, ts)
    now = datetime.now(timezone.utc)

    for lead in leads:
        to, cc, bcc = _parse_recipients(lead.email_id, lead.bd_lead_email, lead.bd_head_email)
        cc = list(dict.fromkeys(cc + cfg.cc_emails))  # merge, preserve order, dedupe
        if not to:
            lead.status = "skipped"
            lead.skipped_reason = "no_valid_to_address"
            skipped += 1
            continue

        subject, body_html = render_email(cfg=cfg, spoc_name=lead.spoc or "")
        lead.send_attempt_count = (lead.send_attempt_count or 0) + 1

        from app.config.settings import settings
        if settings.outreach_test_mode:
            redirect = (settings.outreach_test_redirect_to or "").strip()
            if not redirect:
                raise RuntimeError(
                    "OUTREACH_TEST_MODE=true but OUTREACH_TEST_REDIRECT_TO is empty"
                )
            log.info(
                "[outreach][TEST_MODE] redirect=%r real_to=%s real_cc=%s real_bcc=%s",
                redirect, to, cc, bcc,
            )
            subject = f"[TEST] {subject}"
            body_html = (
                f'<div style="background:#fff3cd;border:1px solid #ffc107;padding:8px;margin-bottom:12px;font-size:12px;">'
                f'<b>TEST MODE</b> — would have been sent to '
                f'<b>TO:</b> {", ".join(to) or "(none)"} '
                f'<b>CC:</b> {", ".join(cc) or "(none)"} '
                f'<b>BCC:</b> {", ".join(bcc) or "(none)"}'
                f'</div>'
            ) + body_html
            allowed_cc = {
                a.strip().lower()
                for a in (settings.outreach_test_allowed_cc or "").split(",")
                if a.strip()
            }
            to, cc, bcc = [redirect], [a for a in cc if a.lower() in allowed_cc], []

        try:
            gmail_message_id = await asyncio.to_thread(
                gmail_sa.send_email,
                to=to,
                cc=cc or None,
                bcc=bcc or None,
                subject=subject,
                body_html=body_html,
                body_text=None,
                from_addr=cfg.sender_email,
                reply_to=cfg.sender_email,
                attachments=attach_list,
            )
            thread_id = await asyncio.to_thread(
                gmail_sa.fetch_message_thread_id, gmail_message_id
            )
            lead.status = "sent"
            lead.sent_at = now
            lead.subject_sent = subject
            lead.gmail_message_id = gmail_message_id
            lead.gmail_thread_id = thread_id
            lead.error = None
            sent += 1
            ts_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
            writeback_updates.append((lead.tab_name or "", lead.gsheet_row_index, "sent", ts_str))
        except Exception as exc:
            log.exception("outreach dispatch: send failed for lead %d", lead.id)
            lead.status = "failed"
            lead.error = {"message": str(exc), "type": type(exc).__name__}
            failed += 1
            writeback_updates.append((lead.tab_name or "", lead.gsheet_row_index, "not sent", ""))

    await db.commit()

    log.info(
        "outreach dispatch [%s]: sent=%d failed=%d skipped=%d",
        cfg.campaign_name, sent, failed, skipped,
    )
    return {"sent": sent, "failed": failed, "skipped": skipped}, writeback_updates


async def writeback_dispatch_status(
    cfg: CampaignConfig,
    writeback_updates: list[tuple[str, int, str, str]],
) -> None:
    """Best-effort GSheet writeback for dispatch results.

    Groups writeback_updates by tab_name and calls OutreachSheetClient.write_back_status
    for each tab. Postgres is authoritative — GSheet failures are logged and swallowed.

    Args:
        cfg: Campaign config (used for gsheet_id).
        writeback_updates: List of (tab_name, row_idx, status, ts) tuples from dispatch_campaign.
    """
    if not writeback_updates:
        return

    from collections import defaultdict
    by_tab: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for tab, row_idx, status_val, ts_val in writeback_updates:
        by_tab[tab].append((row_idx, status_val, ts_val))

    for tab_name, updates in by_tab.items():
        if not tab_name:
            continue
        try:
            client = OutreachSheetClient(
                spreadsheet_id=cfg.gsheet_id,
                tab_name=tab_name,
            )
            await asyncio.to_thread(client.write_back_status, updates)
        except Exception:
            log.warning("outreach: gsheet writeback failed for tab %r — Postgres is authoritative", tab_name)
