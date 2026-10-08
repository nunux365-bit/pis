"""Classifier sender allowlist is derived from ``EMAIL_AUTOMATION_INBOX_QUERY``.

Design note: we deliberately do **not** expose a second env var. The inbox
query already carries ``from:<addr>`` — that's the Gmail-wire sender filter.
Parsing those tokens out and handing them to the classifier keeps one source
of truth: edit the inbox query, both the wire filter and the classifier's
allowlist move together. No drift, no "which variable wins?" confusion.

Fall-back: if the query has no ``from:`` clauses (someone broadens it to a
subject-only match), we keep the pack's hard-coded allowlist — a broad wire
filter should NOT silently broaden who classify trusts.
"""

from __future__ import annotations

import pytest


def _parse(query: str) -> tuple[str, ...]:
    from app.email_automation.pipeline.process import _senders_from_inbox_query

    return _senders_from_inbox_query(query)


def test_parser_extracts_single_sender() -> None:
    assert _parse('from:bhawna.gandhi@1mg.com subject:"Receivable" newer_than:14d') == (
        "bhawna.gandhi@1mg.com",
    )


def test_parser_is_case_insensitive_and_lowercases() -> None:
    # Gmail operators are case-insensitive; addresses normalize to lower.
    assert _parse("FROM:Alice@1mg.com") == ("alice@1mg.com",)


def test_parser_handles_grouped_or() -> None:
    # ``from:(a OR b)`` — Gmail's common multi-sender form.
    assert _parse("from:(alice@1mg.com OR bob@1mg.com) subject:Receivable") == (
        "alice@1mg.com",
        "bob@1mg.com",
    )


def test_parser_handles_multiple_from_clauses() -> None:
    assert _parse("from:a@1mg.com OR from:b@1mg.com") == ("a@1mg.com", "b@1mg.com")


def test_parser_tolerates_quoted_display_names() -> None:
    # ``from:"Name <addr>"`` — Gmail allows this; extract just the address.
    assert _parse('from:"Alice Analyst <alice@1mg.com>"') == ("alice@1mg.com",)


def test_parser_dedupes_preserving_order() -> None:
    assert _parse("from:a@1mg.com from:b@1mg.com from:A@1mg.com") == (
        "a@1mg.com",
        "b@1mg.com",
    )


def test_parser_returns_empty_when_no_from_clause() -> None:
    # Broad query — parser must NOT invent senders; caller falls back to
    # the pack's hard-coded default.
    assert _parse('subject:"Receivable" newer_than:30d') == ()


def test_rule_override_uses_inbox_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import settings
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        'from:newperson@1mg.com subject:"Receivable" newer_than:14d',
        raising=False,
    )

    rule = process._rule_with_overrides(
        REGISTRY["PAYMENT_REMINDER_WEEKLY"],
        inbox_query=settings.email_automation_inbox_query,
    )
    assert rule.sender_allowlist == ("newperson@1mg.com",)
    # Non-sender fields must be inherited unchanged from the pack.
    pack_rule = REGISTRY["PAYMENT_REMINDER_WEEKLY"].classifier_rule
    assert rule.subject_regex == pack_rule.subject_regex
    assert rule.attachment_regex == pack_rule.attachment_regex
    assert rule.min_attachment_size == pack_rule.min_attachment_size


def test_rule_override_falls_back_when_query_has_no_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config.settings import settings
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        'subject:"Receivable" newer_than:14d',
        raising=False,
    )

    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    rule = process._rule_with_overrides(
        pack, inbox_query=settings.email_automation_inbox_query
    )
    assert rule.sender_allowlist == pack.classifier_rule.sender_allowlist


def test_rule_override_flows_into_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a new ``from:`` in the inbox query is trusted by classify."""

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        'from:newperson@1mg.com subject:"Receivable" newer_than:14d',
        raising=False,
    )

    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier(
        [process._rule_with_overrides(pack, inbox_query=settings.email_automation_inbox_query)]
    )

    att = AttachmentStub(
        filename="Receivable-31st-Mar.xlsx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )

    def _msg(sender: str) -> FetchedMessage:
        return FetchedMessage(
            id="m1",
            thread_id="t1",
            sender=sender,
            subject="Receivable / Overdue as on Date 31 Mar 2026",
            received_at_ms=0,
            headers={},
            attachments=(att,),
        )

    assert clf.classify(_msg("New Person <newperson@1mg.com>")) is not None
    # The *previous* default is no longer in the query, so it's no longer trusted.
    assert clf.classify(_msg("Bhawna <bhawna.gandhi@1mg.com>")) is None


def test_caller_inbox_query_beats_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin: HTTP /scan and cron pass the *same* query they used at the wire.

    The classifier's sender allowlist must derive from **that** query, not
    from ``settings.email_automation_inbox_query`` — otherwise an operator
    narrowing the scan via the HTTP body could still process senders only
    trusted by the default settings. Regression guard for the drift bug
    identified in the process.py principal-engineer review.
    """

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    # Settings trusts alice; caller passes a query that trusts only bob.
    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        "from:alice@1mg.com newer_than:14d",
        raising=False,
    )
    caller_query = "from:bob@1mg.com newer_than:14d"

    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier([process._rule_with_overrides(pack, inbox_query=caller_query)])

    att = AttachmentStub(
        filename="Receivable-31st-Mar.xlsx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )

    def _msg(sender: str) -> FetchedMessage:
        return FetchedMessage(
            id="m1",
            thread_id="t1",
            sender=sender,
            subject="Receivable / Overdue as on Date 31 Mar 2026",
            received_at_ms=0,
            headers={},
            attachments=(att,),
        )

    # Caller-query wins: bob is trusted, alice (settings-only) is not.
    assert clf.classify(_msg("Bob <bob@1mg.com>")) is not None
    assert clf.classify(_msg("Alice <alice@1mg.com>")) is None


@pytest.mark.parametrize(
    "subject",
    [
        "Receivable / Overdue as on Date 31 Mar 2026",
        "Receivable/ Overdue as on 15 April",
        "RECEIVABLE/Overdue AS ON 15th April'2026",
        "Fwd: Receivable/ Overdue as on 15th April'2026",
        "Receivables / Overdue as on 15 April",
        "Receivable / Overdues as on 20 April",
        "Receivables/Overdues as on 1 May 2026",
        "Fwd: receivable as on 29th Apr'26",
        "RECEIVABLE AS ON 15 April 2026",
        "Receivable",
        "Fwd: Receivables — Apr'26",
    ],
)
def test_payment_reminder_subject_regex_matches_production_subjects(
    subject: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subjects must classify without requiring the literal word ``date``."""

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        "from:tester@1mg.com newer_than:14d",
        raising=False,
    )
    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier(
        [process._rule_with_overrides(pack, inbox_query=settings.email_automation_inbox_query)]
    )
    att = AttachmentStub(
        filename="Receivable-15th-Apr.xlsx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )
    msg = FetchedMessage(
        id="m1",
        thread_id="t1",
        sender="Tester <tester@1mg.com>",
        subject=subject,
        received_at_ms=0,
        headers={},
        attachments=(att,),
    )
    assert clf.classify(msg) is not None


def test_payment_reminder_subject_regex_rejects_non_receivable_hyphen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``non-receivable`` must not satisfy the standalone-token branch."""

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        "from:tester@1mg.com newer_than:14d",
        raising=False,
    )
    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier(
        [process._rule_with_overrides(pack, inbox_query=settings.email_automation_inbox_query)]
    )
    att = AttachmentStub(
        filename="Receivable-15th-Apr.xlsx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )
    msg = FetchedMessage(
        id="m1",
        thread_id="t1",
        sender="Tester <tester@1mg.com>",
        subject="non-receivable adjustments — FY26",
        received_at_ms=0,
        headers={},
        attachments=(att,),
    )
    assert clf.classify(msg) is None


@pytest.mark.parametrize(
    "filename",
    [
        "Receivable-15th-Apr.xlsx",
        "Receivables-15th Apr'26.xlsx",
        "Copy of Receivables Mar.xlsx",
        "Recevable-20.05.2026.xlsx",
        "Receivables-27-May-26.zip",
    ],
)
def test_payment_reminder_attachment_regex_accepts_receivable_names(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filenames must contain receivable(s) token; optional ``i`` typo tolerated."""

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        "from:tester@1mg.com newer_than:14d",
        raising=False,
    )
    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier(
        [process._rule_with_overrides(pack, inbox_query=settings.email_automation_inbox_query)]
    )
    att = AttachmentStub(
        filename=filename,
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )
    msg = FetchedMessage(
        id="m1",
        thread_id="t1",
        sender="Tester <tester@1mg.com>",
        subject="Receivable / Overdue as on 15 April",
        received_at_ms=0,
        headers={},
        attachments=(att,),
    )
    assert clf.classify(msg) is not None


@pytest.mark.parametrize(
    "filename",
    [
        "Monthly_AR_Report.xlsx",
        "Overdues-15th-Apr.xlsx",
    ],
)
def test_payment_reminder_attachment_rejects_xlsx_without_receivable_token(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attachment gate is receivable(s) in the filename only — not overdue(s)."""

    from app.config.settings import settings
    from app.email_automation.engine.classifier import InboundClassifier
    from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
    from app.email_automation.pipeline import process
    from app.email_automation.workflow_packs import REGISTRY

    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        "from:tester@1mg.com newer_than:14d",
        raising=False,
    )
    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    clf = InboundClassifier(
        [process._rule_with_overrides(pack, inbox_query=settings.email_automation_inbox_query)]
    )
    att = AttachmentStub(
        filename=filename,
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=20_000,
        gmail_attachment_id="att-1",
    )
    msg = FetchedMessage(
        id="m1",
        thread_id="t1",
        sender="Tester <tester@1mg.com>",
        subject="Receivable / Overdue as on 15 April",
        received_at_ms=0,
        headers={},
        attachments=(att,),
    )
    assert clf.classify(msg) is None
