"""Procurement transactional email — Gmail API (same SA + DWD as email automation).

Uses :func:`app.email_automation.gmail_sa.send_email` with
``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` and ``EMAIL_AUTOMATION_IMPERSONATED_USER``.
No SMTP or app passwords. ``From`` is the impersonated mailbox (Gmail-enforced).
"""

from __future__ import annotations

import html
import logging

from app.email_automation import gmail_sa

log = logging.getLogger(__name__)


def send_mail(*, to_email: str, subject: str, body_text: str) -> None:
    """Send a plain-text alert via the shared Gmail SA client."""

    to = (to_email or "").strip()
    if not to:
        log.warning("procurement email skipped (empty to_email)")
        return

    safe = html.escape(body_text or "", quote=True)
    body_html = (
        "<html><body>"
        f'<pre style="white-space:pre-wrap;font-family:system-ui,sans-serif">{safe}</pre>'
        "</body></html>"
    )

    try:
        mid = gmail_sa.send_email(
            to=[to],
            cc=None,
            bcc=None,
            subject=subject,
            body_html=body_html,
            body_text=body_text or None,
            reply_to=None,
            headers=None,
        )
    except FileNotFoundError as e:
        log.warning("procurement email skipped (Gmail SA not configured): %s", e)
        return
    except RuntimeError as e:
        log.warning("procurement email skipped (Gmail impersonation not configured): %s", e)
        return
    except Exception as e:
        log.warning("procurement email failed: %s", e, exc_info=True)
        return

    log.info("procurement email sent to=%s subject=%s gmail_id=%s", to, subject, mid)
