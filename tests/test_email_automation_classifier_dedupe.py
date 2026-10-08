"""Classifier + dedupe key helpers."""

from __future__ import annotations

from app.email_automation.engine.classifier import (
    ClassifierRule,
    InboundClassifier,
)
from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage
from app.email_automation.pipeline import dedupe_key
from app.email_automation.pipeline._shared import _iso_week_key


def _msg(sender: str | None, subject: str | None, attachments):
    return FetchedMessage(
        id="m1",
        thread_id=None,
        sender=sender,
        subject=subject,
        received_at_ms=None,
        headers={},
        attachments=tuple(attachments),
    )


def _rule():
    return ClassifierRule(
        workflow_type="PAYMENT_REMINDER_WEEKLY",
        sender_allowlist=("bhawna.gandhi@1mg.com",),
        subject_regex=r"receivable",
        attachment_regex=r"(?i)receivable.*\.xlsx$",
    )


def test_classifier_accepts_match():
    clf = InboundClassifier([_rule()])
    att = AttachmentStub("Receivable Mar.xlsx", "application/vnd...", 2048, "att1")
    res = clf.classify(
        _msg("Bhawna Gandhi <bhawna.gandhi@1mg.com>", "Receivables — AR", [att])
    )
    assert res is not None
    assert res.workflow_type == "PAYMENT_REMINDER_WEEKLY"
    assert res.attachment.filename == "Receivable Mar.xlsx"


def test_classifier_rejects_wrong_sender():
    clf = InboundClassifier([_rule()])
    att = AttachmentStub("Receivable Mar.xlsx", "x", 2048, "a")
    assert clf.classify(
        _msg("someone@evil.com", "Receivables — AR", [att])
    ) is None


def test_classifier_rejects_small_attachment():
    att = AttachmentStub("Receivable Mar.xlsx", "x", 100, "a")
    # rule default min_attachment_size=1 — override
    rule = ClassifierRule(
        workflow_type="W",
        sender_allowlist=("a@b.com",),
        subject_regex=r".",
        attachment_regex=r".*",
        min_attachment_size=500,
    )
    clf2 = InboundClassifier([rule])
    assert clf2.classify(_msg("a@b.com", "x", [att])) is None


def test_classifier_missing_attachment_is_none():
    clf = InboundClassifier([_rule()])
    assert clf.classify(_msg("bhawna.gandhi@1mg.com", "Receivables", [])) is None


def test_dedupe_key_is_stable_and_unique_per_business_period():
    k1 = dedupe_key("W", "epharma", "H001", "2026-W15")
    k2 = dedupe_key("W", "epharma", "H001", "2026-W15")
    k3 = dedupe_key("W", "epharma", "H001", "2026-W16")
    k4 = dedupe_key("W", "chw", "H001", "2026-W15")
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert len(k1) == 64  # sha256 hex


def test_iso_week_key_produces_stable_iso_year_week():
    import datetime as dt
    d = dt.datetime(2026, 4, 13, 9, 0, 0, tzinfo=dt.timezone.utc)  # Monday W16
    assert _iso_week_key(d) == "2026-W16"
    d2 = dt.datetime(2021, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)  # ISO W53 of 2020
    assert _iso_week_key(d2) == "2020-W53"
