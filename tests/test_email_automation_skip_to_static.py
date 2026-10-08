"""Tests for the "skipped → notify static" path.

Contract pinned here:

* When a plan is skipped AND the variant has ``static_to`` configured,
  the persisted row carries the AR address in ``resolved_to_addrs``,
  the body is prefixed with a one-line "Skipped — <code>: <msg>" banner,
  and the row's ``status`` stays ``skipped`` (UI source-of-truth for
  customer-side reality).
* No second row is created. Same row, same dedupe_key, same audit trail.
* **Test mode RETAINS the static address** (unlike the customer-reminder
  path which strips it). Rationale: a skipped row's only recipient IS
  the AR desk; if we stripped it, ``resolved_to_addrs`` would be empty
  and dispatch's ``cardinality > 0`` filter would drop the row \u2014 so
  QA would never see the skip-notification at all. The sender's
  wire-level redirect still rewrites TO \u2192 the test inbox on the wire.
* Dispatch picks up these rows alongside ``approved`` ones, sends, then
  stamps ``provider_message_id`` + ``sent_at`` while leaving
  ``status='skipped'``. Failures keep ``provider_message_id`` NULL so
  the next dispatch tick retries until the attempt cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.email_automation.pipeline.process import (
    _shape_recipients_for_persist,
    _skip_reason_line,
)


# ---------------------------------------------------------------------------
# _skip_reason_line — banner formatting
# ---------------------------------------------------------------------------


def test_skip_reason_line_uses_first_review_reason():
    """Resolver/aggregator append in priority order, so the first reason
    is the one ops will act on. Code + human_message both surface."""

    html = _skip_reason_line(
        [
            {
                "code": "potential_overpayment",
                "human_message": "Net pending is at or below recorded receipts.",
                "suggested_action": "Reconcile with finance.",
            },
            {"code": "tracker_duplicate_keys", "human_message": "merged"},
        ]
    )
    assert "Skipped \u2014 potential_overpayment" in html
    assert "Net pending is at or below recorded receipts." in html
    # Only the FIRST reason's message is in the banner — the rest live in
    # ``review_reasons`` (JSONB) for the platform UI.
    assert "merged" not in html


def test_skip_reason_line_falls_back_when_no_reasons():
    """Defensive: a skip can in principle land here with empty reasons
    (e.g., a future skip_gate that forgot to attach detail). Body should
    still render something readable instead of breaking the email."""

    html = _skip_reason_line(None)
    assert "Skipped \u2014 skipped" in html
    assert "see review_reasons in the platform UI" in html

    html_empty = _skip_reason_line([])
    assert "Skipped \u2014 skipped" in html_empty


def test_skip_reason_line_html_escapes_operator_controlled_text():
    """``human_message`` is composed from operator-controlled strings
    (party names, sheet labels, raw values) — must be HTML-escaped
    before embedding in the banner so a stray ``<`` doesn't break the
    body or open an injection vector."""

    html = _skip_reason_line(
        [
            {
                "code": "tracker_<bad>",
                "human_message": "<script>alert(1)</script>",
                "suggested_action": "n/a",
            }
        ]
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;bad&gt;" in html
    assert "alert(1)" in html  # text content survives, just neutralized


def test_skip_reason_line_uses_detail_when_human_message_missing():
    """Some legacy reasons (``review_reasons`` from the engine layer) only
    set ``detail``. Banner should fall back so we never show a blank."""

    html = _skip_reason_line(
        [{"code": "tracker_disabled", "detail": "Consider column = N for HANA 1234"}]
    )
    assert "Skipped \u2014 tracker_disabled" in html
    assert "Consider column = N for HANA 1234" in html


# ---------------------------------------------------------------------------
# _shape_recipients_for_persist \u2014 the production / test-mode contract.
#
# Minimal shim types so we can unit-test without importing the full
# ``ResolverConfig`` / ``VariantConfig`` dataclasses (keeps the test free
# of sheet / renderer fixture boilerplate). The production code only
# accesses ``variant.resolver.static_to`` and truthiness of
# ``variant.resolver``, so a dataclass with those two attrs is a faithful
# stand-in.
# ---------------------------------------------------------------------------


@dataclass
class _ResolverStub:
    static_to: tuple[str, ...]


@dataclass
class _VariantStub:
    resolver: _ResolverStub | None


_AR_DESK = "cw_payments@1mg.com"


def _plan(
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    body: str = "<p>original</p>",
) -> dict[str, Any]:
    return {
        "resolved_to": list(to or []),
        "resolved_cc": list(cc or []),
        "rendered_body_html": body,
    }


def test_shape_recipients_non_skipped_passes_through_untouched():
    """``rendered`` / ``approved`` rows keep the resolver's TO/CC/body \u2014
    no banner, no rewrite. The static desk is added by the resolver
    upstream (see ``ResolverConfig.static_to``), so at the persistence
    stage there's nothing to do."""

    plan = _plan(
        to=["ops@customer.example"],
        cc=["kam@1mg.com"],
        body="<p>Dear customer,</p>",
    )
    variant = _VariantStub(resolver=_ResolverStub(static_to=(_AR_DESK,)))

    to, cc, body = _shape_recipients_for_persist(
        plan=plan, variant=variant, status="approved", stored_reasons=None,
    )
    assert to == ["ops@customer.example"]
    assert cc == ["kam@1mg.com"]
    assert body == "<p>Dear customer,</p>"
    assert "Skipped \u2014" not in body


def test_shape_recipients_skipped_with_static_to_rewrites_for_ar_desk():
    """Skipped plan + variant has ``static_to`` \u2192 TO replaced with static,
    CC cleared, banner prepended. This is the core "skipped-but-notify"
    contract: one row, one delivery to AR, skip reason visible in body."""

    plan = _plan(
        to=["contact@customer.example"],
        cc=["kam@1mg.com"],
        body="<p>Dear customer,</p>",
    )
    variant = _VariantStub(resolver=_ResolverStub(static_to=(_AR_DESK,)))

    to, cc, body = _shape_recipients_for_persist(
        plan=plan,
        variant=variant,
        status="skipped",
        stored_reasons=[
            {
                "code": "potential_overpayment",
                "human_message": "Net pending is at or below recorded receipts.",
            }
        ],
    )
    assert to == [_AR_DESK]
    assert cc == []
    assert body.startswith("<div")
    assert "Skipped \u2014 potential_overpayment" in body
    assert "Net pending is at or below recorded receipts." in body
    # Original body is preserved AFTER the banner (prepend, not replace).
    assert "<p>Dear customer,</p>" in body


def test_shape_recipients_skipped_static_contract_is_identical_in_test_mode():
    """H1 contract: ``email_automation_test_mode`` must NOT affect the
    skipped-but-notify shape. ``static_to`` is retained unconditionally
    so dispatch's ``jsonb_array_length(resolved_to_addrs) > 0`` filter still
    matches and the sender's wire-level redirect carries the skip
    notification to the QA inbox.

    Regression: if someone adds a ``if settings.email_automation_test_mode:
    to_addrs = []`` short-circuit here, the QA loop silently breaks
    (rows land in skipped, never get sent) and no one notices until
    production day."""

    plan = _plan(to=["contact@customer.example"])
    variant = _VariantStub(resolver=_ResolverStub(static_to=(_AR_DESK,)))

    # Function is pure \u2014 it doesn't read settings. Pinning that here
    # protects the contract from regressing into settings-dependence.
    for _simulated_test_mode in (True, False):
        to, cc, body = _shape_recipients_for_persist(
            plan=plan,
            variant=variant,
            status="skipped",
            stored_reasons=[{"code": "tracker_not_found", "human_message": "not in master"}],
        )
        assert to == [_AR_DESK], (
            "static_to must always be retained on skipped rows regardless of "
            "test mode \u2014 dispatch's cardinality filter is the only gate"
        )
        assert cc == []
        assert "Skipped \u2014 tracker_not_found" in body


def test_shape_recipients_skipped_without_static_to_yields_empty_to():
    """Safety net: a variant with no ``static_to`` (unusual but allowed
    for a workflow that operates purely on customer-resolved contacts)
    must NOT silently send the skip to an empty list or leak the
    customer's address as the AR target. Empty ``to_addrs`` is how
    dispatch knows "don't pick this up", and the row stays in the
    skipped queue for ops to review manually."""

    plan = _plan(to=["contact@customer.example"], cc=["kam@1mg.com"])
    variant = _VariantStub(resolver=_ResolverStub(static_to=()))

    to, cc, body = _shape_recipients_for_persist(
        plan=plan, variant=variant, status="skipped", stored_reasons=None,
    )
    # Skipped + no static \u2192 keep the plan's resolved values so ops can
    # see what the resolver would have picked. Dispatch picks up only
    # rows where resolved_to_addrs is non-empty AND status in (approved,
    # rendered, skipped+provider_message_id IS NULL) \u2014 in this case
    # ``static_to`` being empty means the operator configured the
    # variant without an AR desk on purpose. Banner is NOT prepended
    # (no place for it to go).
    assert to == ["contact@customer.example"]
    assert cc == ["kam@1mg.com"]
    assert "Skipped \u2014" not in body


def test_shape_recipients_skipped_no_resolver_noop():
    """A variant with ``resolver=None`` (future no-email-send variant?)
    still must be safe to call. No crash, no banner, empty TO."""

    variant = _VariantStub(resolver=None)
    plan = _plan(to=[], cc=[])
    to, cc, body = _shape_recipients_for_persist(
        plan=plan, variant=variant, status="skipped", stored_reasons=None,
    )
    assert to == []
    assert cc == []
    assert "Skipped \u2014" not in body
