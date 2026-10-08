"""Dispatch threading: weekly RFC cache + inbound fallback (unit + structural).

Full dual-week Postgres E2E is optional here; contract is pinned by
:data:`resolve_thread_parent_raw_message_id` and existing single-tick dispatch E2E.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.email_automation.pipeline import dispatch as _dispatch


def test_resolve_customer_prefers_prior_cached_rfc_over_inbound():
    sid = uuid.uuid4()
    key = ("PAYMENT_REMINDER_WEEKLY", "chw", "1000011608")
    prior = {key: "<prior-week@mail.gmail.com>"}
    inbound = {sid: "<inbound@trigger.com>"}
    out = _dispatch.resolve_thread_parent_raw_message_id(
        was_skipped=False,
        workflow_type=key[0],
        variant=key[1],
        business_key=key[2],
        source_message_id=sid,
        prior_rfc_by_key=prior,
        raw_mid_by_source=inbound,
    )
    assert out == "<prior-week@mail.gmail.com>"


def test_resolve_customer_falls_back_to_inbound_when_no_prior():
    sid = uuid.uuid4()
    key = ("PAYMENT_REMINDER_WEEKLY", "chw", "1000011608")
    inbound = {sid: "<inbound@trigger.com>"}
    out = _dispatch.resolve_thread_parent_raw_message_id(
        was_skipped=False,
        workflow_type=key[0],
        variant=key[1],
        business_key=key[2],
        source_message_id=sid,
        prior_rfc_by_key={key: None},
        raw_mid_by_source=inbound,
    )
    assert out == "<inbound@trigger.com>"


def test_resolve_customer_no_source_uses_prior_only():
    key = ("PAYMENT_REMINDER_WEEKLY", "chw", "DMPL4921")
    prior = {key: "<chain@mail.gmail.com>"}
    out = _dispatch.resolve_thread_parent_raw_message_id(
        was_skipped=False,
        workflow_type=key[0],
        variant=key[1],
        business_key=key[2],
        source_message_id=None,
        prior_rfc_by_key=prior,
        raw_mid_by_source={},
    )
    assert out == "<chain@mail.gmail.com>"


def test_resolve_skipped_ignores_prior_rfc_uses_inbound_only():
    sid = uuid.uuid4()
    key = ("PAYMENT_REMINDER_WEEKLY", "chw", "1000011608")
    prior = {key: "<customer-thread@mail.gmail.com>"}
    inbound = {sid: "<inbound@trigger.com>"}
    out = _dispatch.resolve_thread_parent_raw_message_id(
        was_skipped=True,
        workflow_type=key[0],
        variant=key[1],
        business_key=key[2],
        source_message_id=sid,
        prior_rfc_by_key=prior,
        raw_mid_by_source=inbound,
    )
    assert out == "<inbound@trigger.com>"


def test_resolve_skipped_no_inbound_returns_none():
    key = ("PAYMENT_REMINDER_WEEKLY", "chw", "x")
    out = _dispatch.resolve_thread_parent_raw_message_id(
        was_skipped=True,
        workflow_type=key[0],
        variant=key[1],
        business_key=key[2],
        source_message_id=None,
        prior_rfc_by_key={key: "<prior@x.com>"},
        raw_mid_by_source={},
    )
    assert out is None


def test_dispatch_claim_contains_prior_lookup_and_batch_exclusion():
    """Structural guard: regressions drop weekly chain or claimed-id exclusion."""

    src = inspect.getsource(_dispatch.dispatch_claim_and_send)
    assert "provider_rfc_message_id.isnot(None)" in src
    assert "EmailAutomationSend.id.notin_(claimed_ids)" in src
    assert "resolve_thread_parent_raw_message_id" in src
    assert "_store_outbound_rfc_message_id" in src


@pytest.mark.asyncio
async def test_store_outbound_rfc_message_id_swallows_gmail_errors(monkeypatch):
    def boom(_mid: str) -> str:
        raise RuntimeError("gmail metadata down")

    monkeypatch.setattr(
        "app.email_automation.pipeline.dispatch.gmail_sa.fetch_message_rfc_message_id",
        boom,
    )
    assert await _dispatch._store_outbound_rfc_message_id("any-id") is None
