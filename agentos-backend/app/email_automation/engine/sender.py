"""Outbound email wrapper with an auditable test-mode redirect.

The test-mode redirect is implemented **here**, not inside ``gmail_sa``, so it can
be reflected on the persisted ``EmailAutomationSend`` row (``resolved_to_addrs``
captures the business-resolved recipients, ``to_addrs`` captures what actually
went on the wire). This keeps redirection visible in the UI, the DB, and the
printed log — never hidden in the transport.

``From`` and ``Reply-To`` both use ``settings.email_automation_send_from`` when set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape as _h

from app.config.settings import settings
from app.email_automation import gmail_sa
from app.email_automation.engine.banners import banner as _banner

log = logging.getLogger(__name__)

# Practical cap keeps bogus / oversized header values off the wire (RFC 5322 limit is much higher).
_MAX_MESSAGE_ID_INNER_LEN = 250


def _normalize_parent_rfc_message_id(value: str | None) -> str | None:
    """Turn an inbound ``Message-ID`` header value into a safe ``<local@domain>`` token.

    Returns ``None`` if the value is missing or not suitable for ``In-Reply-To`` /
    ``References`` — :func:`send` then omits threading headers (same as legacy sends).
    """

    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if any(c in s for c in "\r\n\x00"):
        return None

    inner: str
    if s.startswith("<"):
        closing = s.rfind(">")
        if closing <= 1:
            return None
        inner = s[1:closing].strip()
    else:
        inner = s

    if not inner or len(inner) > _MAX_MESSAGE_ID_INNER_LEN:
        return None
    if any(c in inner for c in " \t<>"):
        return None
    parts = inner.split("@")
    if len(parts) != 2:
        return None
    local, domain = parts[0], parts[1]
    if not local or not domain:
        return None

    return f"<{inner}>"


def _redact_email(addr: str) -> str:
    """``ops@1mg.com`` → ``o**@1mg.com`` for safe INFO logging in production."""

    if not addr or "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    head = local[:1] if local else "*"
    return f"{head}{'*' * max(1, len(local) - 1)}@{domain}"


def _redact_list(addrs: tuple[str, ...]) -> list[str]:
    return [_redact_email(a) for a in addrs]


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    subject: str
    body_html: str
    body_text: str | None
    resolved_to: tuple[str, ...]
    resolved_cc: tuple[str, ...] = ()
    resolved_bcc: tuple[str, ...] = ()
    # Stable per-row idempotency token \u2014 the ``EmailAutomationSend.id`` UUID
    # of the row being sent. Emitted as the ``X-Agentos-Send-Id`` header on
    # every outbound message so ops can cross-reference a message in
    # Gmail Sent with its DB row, and \u2014 critically \u2014 so that after a
    # crash between Gmail accept and our local commit, ops can search
    # Sent for the token and decide whether the next tick's retry would
    # duplicate (``X-Agentos-Send-Id:<uuid>`` returning a hit means the
    # row already went out). We deliberately do NOT try to auto-recover
    # via Gmail search on the hot path \u2014 the attempt cap bounds the
    # worst case to 5 AR-desk dupes per row, which ops has signed off on
    # as acceptable for the skip-notify path.
    send_id: str | None = None
    # Raw ``Message-ID`` from :class:`~app.db.models.EmailAutomationMessage.raw_headers`
    # (dispatch extracts it). Normalized in :func:`send`; omitted when invalid.
    parent_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """Return value of :func:`send` — both the wire-level detail and the redirect trace."""

    provider_message_id: str
    wire_to: tuple[str, ...]
    wire_cc: tuple[str, ...]
    wire_bcc: tuple[str, ...]
    test_mode: bool


def send(email: OutboundEmail) -> SendOutcome:
    """Send ``email`` through the SA-impersonated Gmail mailbox.

    When ``settings.email_automation_test_mode`` is true, TO/CC/BCC are redirected
    to ``settings.email_automation_test_redirect_to`` and the original recipients
    are logged at INFO. The DB row retains the resolved real recipients separately.
    """

    real_to = tuple(email.resolved_to)
    real_cc = tuple(email.resolved_cc)
    real_bcc = tuple(email.resolved_bcc)

    if settings.email_automation_test_mode:
        redirect = (settings.email_automation_test_redirect_to or "").strip()
        if not redirect:
            raise RuntimeError(
                "EMAIL_AUTOMATION_TEST_MODE=true but EMAIL_AUTOMATION_TEST_REDIRECT_TO is empty"
            )
        # Audit-trail of the real recipients lives on the persisted ``EmailAutomationSend``
        # row (``resolved_to_addrs`` / ``resolved_cc_addrs``) and inside the in-body banner.
        # Logs only ever see the redacted form, regardless of log level — no DEBUG escape hatch.
        log.info(
            "[email_automation][TEST_MODE] redirect=%r real_to=%s real_cc=%s real_bcc=%s",
            redirect,
            _redact_list(real_to),
            _redact_list(real_cc),
            _redact_list(real_bcc),
        )
        wire_to = (redirect,)
        wire_cc: tuple[str, ...] = ()
        wire_bcc: tuple[str, ...] = ()
        subject = f"[TEST] {email.subject}"
        body_html = _banner(
            "warn",
            "<b>TEST MODE</b> — this email would have been sent to "
            f"<b>TO:</b> {_h(', '.join(real_to)) or '(none)'} "
            f"<b>CC:</b> {_h(', '.join(real_cc)) or '(none)'} "
            f"<b>BCC:</b> {_h(', '.join(real_bcc)) or '(none)'}",
        ) + email.body_html
        test_mode = True
    else:
        wire_to, wire_cc, wire_bcc = real_to, real_cc, real_bcc
        subject = email.subject
        body_html = email.body_html
        test_mode = False

    if not wire_to:
        raise ValueError("sender: no recipient after redirect; refusing to send")

    extra_headers: dict[str, str] = {}
    if email.send_id:
        # Custom audit header \u2014 surfaces in Gmail Sent folder search
        # (``has:userlabels`` / raw header search) so ops can answer
        # "did this row's message actually go out?" without log diving.
        extra_headers["X-Agentos-Send-Id"] = email.send_id

    parent_mid = _normalize_parent_rfc_message_id(email.parent_message_id)
    if parent_mid:
        extra_headers["In-Reply-To"] = parent_mid
        extra_headers["References"] = parent_mid

    outbound_id = (settings.email_automation_send_from or "").strip() or None

    provider_id = gmail_sa.send_email(
        to=list(wire_to),
        cc=list(wire_cc),
        bcc=list(wire_bcc),
        subject=subject,
        body_html=body_html,
        body_text=email.body_text,
        reply_to=outbound_id,
        headers=extra_headers or None,
        from_addr=outbound_id,
    )
    return SendOutcome(
        provider_message_id=provider_id,
        wire_to=wire_to,
        wire_cc=wire_cc,
        wire_bcc=wire_bcc,
        test_mode=test_mode,
    )
