"""Path K structural helpers — sender role classification + reply timing.

Pure functions over Gmail message dicts; no DB / network. The LLM and Gmail fetch
are exercised only through these deterministic helpers.
"""

from __future__ import annotations

import pytest

from app.email_automation.kam_directory import KamDirectory, KamInfo
from app.email_automation.pipeline import kam_reply_intelligence as kri


def _msg(mid: str, ts_ms: int, frm: str, *, to: str | None = None, cc: str | None = None) -> dict:
    headers = [{"name": "From", "value": frm}]
    if to is not None:
        headers.append({"name": "To", "value": to})
    if cc is not None:
        headers.append({"name": "Cc", "value": cc})
    return {
        "id": mid,
        "internalDate": str(ts_ms),
        "payload": {"headers": headers},
    }


def _directory() -> KamDirectory:
    kam = KamInfo(name="Asha Rao", email="asha.rao@1mg.com")
    return KamDirectory(
        hana_to_kam={"H1": kam},
        kam_to_hanas={"asha.rao@1mg.com": ["H1"]},
        kams_by_key={"asha.rao@1mg.com": kam},
        _emails=frozenset({"asha.rao@1mg.com"}),
    )


@pytest.fixture(autouse=True)
def _central_mailbox(monkeypatch):
    # Central / automation mailbox that is never a KAM.
    from app.email_automation.pipeline import collections_intelligence as ci

    monkeypatch.setattr(ci.settings, "email_automation_impersonated_user", "invoices@1mg.com")
    monkeypatch.setattr(ci.settings, "email_automation_send_from", "invoices@1mg.com")


def test_sender_role_classification():
    d = _directory()
    assert kri._sender_role("invoices@1mg.com", d) == "central"
    assert kri._sender_role("Asha Rao <asha.rao@1mg.com>", d) == "kam"
    assert kri._sender_role("other.person@1mg.com", d) == "internal"
    assert kri._sender_role("ap@client.com", d) == "client"


def test_reply_timing_kam_followed_up():
    d = _directory()
    # client @ t=0s, KAM reply @ t=2h.
    msgs = [
        _msg("m1", 0, "ap@client.com"),
        _msg("m2", 2 * 3600 * 1000, "asha.rao@1mg.com"),
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is True
    assert out["kam_replied"] is True
    assert out["team_replied"] is True
    assert out["reply_seconds"] == 2 * 3600


def test_reply_timing_no_kam_followup():
    d = _directory()
    msgs = [
        _msg("m1", 0, "asha.rao@1mg.com"),  # earlier KAM msg does NOT count
        _msg("m2", 1000, "ap@client.com"),  # latest client ask unanswered
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is True
    assert out["kam_replied"] is False
    assert out["team_replied"] is False
    assert out["reply_seconds"] is None


def test_reply_timing_client_never_replied():
    d = _directory()
    msgs = [
        _msg("m1", 0, "invoices@1mg.com"),
        _msg("m2", 1000, "asha.rao@1mg.com"),
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is False
    assert out["kam_replied"] is False
    assert out["team_replied"] is False


def test_reply_timing_other_team_member_followup_counts_for_team_not_kam():
    # Finance/Central teammate (not the assigned KAM) replies to the client.
    d = _directory()
    msgs = [
        _msg("m1", 0, "ap@client.com", to="asha.rao@1mg.com"),
        _msg(
            "m2", 1000, "bhawna.gandhi@1mg.com",
            to="ap@client.com", cc="asha.rao@1mg.com",
        ),
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is True
    assert out["kam_replied"] is False       # not the assigned KAM personally
    assert out["team_replied"] is True       # but a 1mg teammate did reach the client


def test_reply_timing_central_mailbox_followup_does_not_count():
    # Only the automation mailbox "replies" (e.g. a scheduled nudge) -> not a real reply.
    d = _directory()
    msgs = [
        _msg("m1", 0, "ap@client.com", to="asha.rao@1mg.com"),
        _msg("m2", 1000, "invoices@1mg.com", to="ap@client.com"),
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is True
    assert out["kam_replied"] is False
    assert out["team_replied"] is False


def test_reply_timing_internal_only_reply_does_not_count():
    # KAM forwards to a colleague for clarification -> internal-only, not client-facing.
    d = _directory()
    msgs = [
        _msg("m1", 0, "ap@client.com", to="asha.rao@1mg.com"),
        _msg(
            "m2", 1000, "asha.rao@1mg.com",
            to="other.person@1mg.com",  # no external recipient at all
        ),
    ]
    out = kri._compute_reply_timing(msgs, d)
    assert out["client_responsive"] is True
    assert out["kam_replied"] is False
    assert out["team_replied"] is False


def test_directory_block_handles_missing_email():
    block = kri._kam_directory_block("H1", KamInfo(name="Asha Rao", email=""))
    assert "Asha Rao" in block and "no personal email" in block
