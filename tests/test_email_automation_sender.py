"""Sender — test-mode recipient redirect (the critical safety rail).

This test monkey-patches the low-level Gmail send so no network is required. It
verifies the contract:

1. When ``EMAIL_AUTOMATION_TEST_MODE`` is true, the wire-level ``to`` becomes the
   configured redirect mailbox and the resolved real recipients are preserved on
   the outcome for audit.
2. When test-mode is disabled, resolved == wire (no redirection).
3. Subject gets a ``[TEST]`` prefix only in test mode.
4. Empty ``resolved_to`` raises — we never send to nobody even in test mode.
"""

from __future__ import annotations

from app.config.settings import settings
from app.email_automation.engine import sender as _sender


def _install_fake_gmail(monkeypatch):
    captured: dict[str, object] = {}

    def fake_send_email(**kwargs):
        captured.update(kwargs)
        return "gmail-id-42"

    monkeypatch.setattr("app.email_automation.gmail_sa.send_email", fake_send_email)
    return captured


def test_test_mode_redirects_recipients_and_preserves_real_ones(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(
        settings, "email_automation_test_redirect_to", "automation.agents@1mg.com"
    )

    email = _sender.OutboundEmail(
        subject="Payment Reminder — Party",
        body_html="<p>hi</p>",
        body_text=None,
        resolved_to=("alice@client.com", "bob@client.com"),
        resolved_cc=("kam@1mg.com",),
        resolved_bcc=(),
    )
    outcome = _sender.send(email)

    assert captured["to"] == ["automation.agents@1mg.com"]
    assert captured["cc"] == []
    assert captured["bcc"] == []
    assert captured["subject"].startswith("[TEST] ")
    # body_html carries a TEST banner with the real recipients for audit inside Gmail
    assert "TEST MODE" in captured["body_html"]
    assert "alice@client.com" in captured["body_html"]
    assert "bob@client.com" in captured["body_html"]
    assert "kam@1mg.com" in captured["body_html"]

    assert outcome.test_mode is True
    assert outcome.wire_to == ("automation.agents@1mg.com",)
    assert outcome.wire_cc == ()
    assert outcome.provider_message_id == "gmail-id-42"


def test_non_test_mode_sends_to_real_recipients(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    monkeypatch.setattr(settings, "email_automation_send_from", "")

    email = _sender.OutboundEmail(
        subject="Payment Reminder — Party",
        body_html="<p>hi</p>",
        body_text="hi",
        resolved_to=("alice@client.com",),
        resolved_cc=("kam@1mg.com",),
    )
    outcome = _sender.send(email)

    assert captured["to"] == ["alice@client.com"]
    assert captured["cc"] == ["kam@1mg.com"]
    assert captured["subject"] == "Payment Reminder — Party"
    assert "TEST MODE" not in captured["body_html"]
    assert outcome.test_mode is False
    assert outcome.wire_to == ("alice@client.com",)
    assert captured.get("reply_to") is None
    assert captured.get("from_addr") is None


def test_test_mode_requires_redirect_address(monkeypatch):
    _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(settings, "email_automation_test_redirect_to", "")
    email = _sender.OutboundEmail(
        subject="s", body_html="x", body_text=None,
        resolved_to=("a@b.com",),
    )
    try:
        _sender.send(email)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "TEST_MODE" in str(e).upper()


def test_no_recipients_raises(monkeypatch):
    _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    email = _sender.OutboundEmail(
        subject="s", body_html="x", body_text=None, resolved_to=(),
    )
    try:
        _sender.send(email)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_test_banner_html_escapes_recipients(monkeypatch):
    """A malicious / accidental ``<script>`` in a tracker email column must not survive
    into the test-mode banner — the banner is rendered raw HTML inside the body."""

    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(
        settings, "email_automation_test_redirect_to", "automation.agents@1mg.com"
    )
    email = _sender.OutboundEmail(
        subject="Reminder",
        body_html="<p>hi</p>",
        body_text=None,
        resolved_to=("<script>alert(1)</script>@x.com",),
        resolved_cc=("ok@x.com",),
    )
    _sender.send(email)
    body = captured["body_html"]
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)" in body


def test_send_id_threaded_as_x_agentos_send_id_header(monkeypatch):
    """M5 contract: when ``OutboundEmail.send_id`` is set, sender emits it
    as the ``X-Agentos-Send-Id`` header so ops can cross-reference a
    Gmail Sent message with its DB row. Header is absent when
    ``send_id`` is unset (backwards compatibility)."""

    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)

    email = _sender.OutboundEmail(
        subject="s",
        body_html="<p>x</p>",
        body_text=None,
        resolved_to=("alice@client.com",),
        send_id="11111111-2222-3333-4444-555555555555",
    )
    _sender.send(email)
    headers = captured.get("headers") or {}
    assert headers.get("X-Agentos-Send-Id") == "11111111-2222-3333-4444-555555555555"


def test_send_without_send_id_omits_header(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    monkeypatch.setattr(settings, "email_automation_send_from", "")
    email = _sender.OutboundEmail(
        subject="s", body_html="<p>x</p>", body_text=None,
        resolved_to=("alice@client.com",),
    )
    _sender.send(email)
    # sender passes ``headers=None`` when no custom headers are needed;
    # gmail_sa's ``send_email`` treats None and {} identically, so both
    # mean "no X-Agentos-Send-Id header on the wire".
    assert captured.get("headers") in (None, {})
    assert captured.get("from_addr") is None
    assert captured.get("reply_to") is None


def test_send_from_setting_threads_into_gmail_send(monkeypatch):
    """``EMAIL_AUTOMATION_SEND_FROM`` is the single outbound identity: ``From`` and ``Reply-To``."""
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    monkeypatch.setattr(settings, "email_automation_send_from", "invoices@1mg.com")
    email = _sender.OutboundEmail(
        subject="s", body_html="<p>x</p>", body_text=None,
        resolved_to=("alice@client.com",),
    )
    _sender.send(email)
    assert captured.get("from_addr") == "invoices@1mg.com"
    assert captured.get("reply_to") == "invoices@1mg.com"


def test_send_from_setting_strips_whitespace(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    monkeypatch.setattr(settings, "email_automation_send_from", "  invoices@1mg.com  ")
    email = _sender.OutboundEmail(
        subject="s", body_html="<p>x</p>", body_text=None,
        resolved_to=("alice@client.com",),
    )
    _sender.send(email)
    assert captured.get("from_addr") == "invoices@1mg.com"
    assert captured.get("reply_to") == "invoices@1mg.com"


def test_parent_message_id_sets_threading_headers(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    email = _sender.OutboundEmail(
        subject="s",
        body_html="<p>x</p>",
        body_text=None,
        resolved_to=("alice@client.com",),
        parent_message_id="<thread-root@mail.gmail.com>",
    )
    _sender.send(email)
    headers = captured.get("headers") or {}
    assert headers["In-Reply-To"] == "<thread-root@mail.gmail.com>"
    assert headers["References"] == "<thread-root@mail.gmail.com>"


def test_parent_message_id_without_brackets_normalized(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    email = _sender.OutboundEmail(
        subject="s",
        body_html="<p>x</p>",
        body_text=None,
        resolved_to=("alice@client.com",),
        parent_message_id="thread-root@mail.gmail.com",
    )
    _sender.send(email)
    headers = captured.get("headers") or {}
    assert headers["In-Reply-To"] == "<thread-root@mail.gmail.com>"


def test_invalid_parent_message_id_omits_threading_headers(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    bad_values = (
        "no-at-sign",
        "a\nb@c.com",
        "<only-open@x.com",
        "<a@b@c>",
        "<" + "a" * 246 + "@x.co>",  # inner segment length > 250
    )
    for bad in bad_values:
        captured.clear()
        email = _sender.OutboundEmail(
            subject="s",
            body_html="<p>x</p>",
            body_text=None,
            resolved_to=("alice@client.com",),
            parent_message_id=bad,
        )
        _sender.send(email)
        headers = captured.get("headers") or {}
        assert "In-Reply-To" not in headers
        assert "References" not in headers


def test_threading_headers_combine_with_send_id(monkeypatch):
    captured = _install_fake_gmail(monkeypatch)
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    email = _sender.OutboundEmail(
        subject="s",
        body_html="<p>x</p>",
        body_text=None,
        resolved_to=("alice@client.com",),
        send_id="11111111-2222-3333-4444-555555555555",
        parent_message_id="<p@c.com>",
    )
    _sender.send(email)
    headers = captured.get("headers") or {}
    assert headers["X-Agentos-Send-Id"] == "11111111-2222-3333-4444-555555555555"
    assert headers["In-Reply-To"] == "<p@c.com>"
    assert headers["References"] == "<p@c.com>"


def test_raw_message_id_from_headers_case_insensitive():
    from app.email_automation.pipeline.dispatch import _raw_message_id_from_headers

    assert _raw_message_id_from_headers({"Message-ID": " <x@y.com> "}) == "<x@y.com>"
    assert _raw_message_id_from_headers({"message-id": "a@b.co"}) == "a@b.co"
    assert _raw_message_id_from_headers({"Other": "z", "Message-Id": "m@n.o"}) == "m@n.o"
    assert _raw_message_id_from_headers(None) is None
    assert _raw_message_id_from_headers({}) is None


def test_redact_email_helper_masks_local_part():
    assert _sender._redact_email("alice@client.com") == "a****@client.com"
    assert _sender._redact_email("a@b.com") == "a*@b.com"
    assert _sender._redact_email("") == "***"
    assert _sender._redact_email("no-at-sign") == "***"
