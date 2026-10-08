"""Payroll workflow transition email notifications — Gmail API (same SA + DWD).

Uses :func:`app.email_automation.gmail_sa.send_email` with
``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` and ``EMAIL_AUTOMATION_IMPERSONATED_USER``.
No SMTP or app passwords. ``From`` is the impersonated mailbox (Gmail-enforced).

Gated by ``settings.payroll_email_enabled`` — when False, all sends are silently
skipped (logged at DEBUG level). This lets you keep the integration wired up in
code but disable actual email dispatch during dev / testing.

This module is **self-contained** and does NOT import or modify any procurement,
outreach, or email_automation engine code. It only calls the shared
``gmail_sa.send_email()`` transport.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.config.settings import settings

log = logging.getLogger(__name__)

# ── Template engine ──────────────────────────────────────────────────────────

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
)


def _render_template(template_name: str, **ctx) -> str:
    """Render a Jinja2 HTML template from the payroll templates directory."""
    tmpl = _jinja_env.get_template(template_name)
    return tmpl.render(**ctx)


# ── Public API ───────────────────────────────────────────────────────────────

async def send_payroll_email(
    *,
    event: str,
    to_email: str,
    cc_emails: list[str] | None = None,
    **template_vars,
) -> None:
    """Send a payroll workflow email notification.

    Args:
        event: Template event name (maps to ``templates/{event}.html.j2``).
        to_email: Primary recipient email address.
        cc_emails: Optional CC list.
        **template_vars: Variables passed to the Jinja2 template AND used for
            subject line rendering.

    The function is a no-op when ``settings.payroll_email_enabled`` is False,
    when ``to_email`` is empty/NA, or when the Gmail SA is not configured.
    """
    if not settings.payroll_email_enabled:
        log.debug("payroll email skipped (payroll_email_enabled=false) event=%s to=%s", event, to_email)
        return

    to = (to_email or "").strip()
    if not to or to.upper() == "NA":
        log.warning("payroll email skipped (empty/NA to_email) event=%s", event)
        return

    # Render subject and body from template
    try:
        subject = _get_subject(event, **template_vars)
        body_html = _render_template(f"{event}.html.j2", **template_vars)
    except Exception as e:
        log.warning("payroll email template render failed event=%s: %s", event, e, exc_info=True)
        return

    # Send via Gmail SA client
    try:
        cc_list = [c.strip() for c in (cc_emails or []) if c and c.strip()]
        import asyncio
        from app.email_automation import gmail_sa
        mid = await asyncio.to_thread(
            gmail_sa.send_email,
            to=[to],
            cc=cc_list or None,
            bcc=None,
            subject=subject,
            body_html=body_html,
            body_text=None,
            reply_to=None,
            headers=None,
        )
    except FileNotFoundError as e:
        log.warning("payroll email skipped (Gmail SA JSON file not found): %s", e)
        return
    except RuntimeError as e:
        log.warning("payroll email skipped (Gmail impersonation not configured): %s", e)
        return
    except Exception as e:
        log.warning("payroll email failed event=%s to=%s: %s", event, to, e, exc_info=True)
        return

    log.info("payroll email sent event=%s to=%s gmail_id=%s", event, to, mid)


# ── Subject lines per event ─────────────────────────────────────────────────

_SUBJECTS: dict[str, str] = {
    "maker_to_hrbp":    "[PIS] {module} — New sheet submitted for HRBP review",
    "maker_to_hod":     "[PIS] {module} — New sheet submitted for HOD approval",
    "hrbp_to_hod":      "[PIS] {module} — HRBP approved, pending HOD approval",
    "hrbp_reject":      "[PIS] {module} — Sheet returned by HRBP for correction",
    "hod_approve":      "[PIS] {module} — HOD approved, pending Payroll processing",
    "hod_approve_maker": "[PIS] {module} — HOD approved sheet, forwarded to Payroll",
    "hod_return_maker": "[PIS] {module} — Sheet returned by HOD for correction",
    "hod_return_hrbp":  "[PIS] {module} — Sheet returned by HOD for HRBP re-review",
    "payroll_return_maker": "[PIS] {module} — Sheet returned by Payroll for correction",
}


def _get_subject(event: str, **ctx) -> str:
    """Return a formatted subject line for the given event."""
    template = _SUBJECTS.get(event)
    if not template:
        return f"[PIS] Payroll Workflow Notification — {event}"
    return template.format(**{k: v for k, v in ctx.items() if isinstance(v, str)})
