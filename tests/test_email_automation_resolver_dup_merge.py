"""Duplicate-tracker-key MERGE contract.

When the master tracker has N rows for the same business key, the resolver
must:

1. Union TO and CC contacts across all matched rows (deduped, order
   preserved by first-seen).
2. Treat the customer as enabled if ANY duplicate marks them enabled
   (most permissive interpretation — the customer shouldn't suffer from
   sheet-hygiene gaps).
3. Surface ``tracker_duplicate_keys`` as a non-blocking notice — the row
   still ships, ops cleans the sheet later.
4. The pipeline's skip-gate must NOT route the row to ``skipped`` solely
   because of ``tracker_duplicate_keys``.

This is the new contract; the old "first match wins + skip" behaviour is
gone — see ``test_email_automation_resolver_renderer.py`` for the migrated
case.
"""

from __future__ import annotations

from app.email_automation.engine.resolver import ResolverConfig, resolve
from app.email_automation.pipeline.process import (
    _NON_BLOCKING_REVIEW_REASON_CODES,
)


def _cfg(**overrides) -> ResolverConfig:
    base = dict(
        key_columns=("BP Code",),
        to_columns=("Email 1", "Email 2"),
        cc_columns=("KAM Email",),
        static_to=("ar-desk@1mg.com",),
        static_cc=(),
    )
    base.update(overrides)
    return ResolverConfig(**base)


def test_dup_keys_merges_to_addresses_across_all_rows():
    """Three rows, three different TOs → all three flow into resolved_to,
    plus the static_to. Order is first-seen-wins."""

    tracker = [
        {"BP Code": "H001", "Email 1": "alice@c.com", "Email 2": None,
         "KAM Email": None},
        {"BP Code": "H001", "Email 1": "bob@c.com",   "Email 2": None,
         "KAM Email": None},
        {"BP Code": "H001", "Email 1": "carol@c.com", "Email 2": None,
         "KAM Email": None},
    ]
    res = resolve(tracker, business_key_parts=["H001"], config=_cfg())
    assert res.to == ("alice@c.com", "bob@c.com", "carol@c.com", "ar-desk@1mg.com")


def test_dup_keys_merges_cc_addresses_too():
    """CC contacts also merge — same dedupe + ordering rules as TO."""

    tracker = [
        {"BP Code": "H1", "Email 1": "to1@c.com", "KAM Email": "kam1@1mg.com"},
        {"BP Code": "H1", "Email 1": "to1@c.com", "KAM Email": "kam2@1mg.com"},
        {"BP Code": "H1", "Email 1": "to2@c.com", "KAM Email": None},
    ]
    res = resolve(tracker, business_key_parts=["H1"], config=_cfg())
    # TO: dedup + merge.
    assert res.to == ("to1@c.com", "to2@c.com", "ar-desk@1mg.com")
    # CC: both KAMs survive, none dropped.
    assert res.cc == ("kam1@1mg.com", "kam2@1mg.com")


def test_dup_keys_dedupes_identical_addresses():
    """If two duplicate rows happen to carry the same TO, it appears once
    in the final list (no double-emailing alice)."""

    tracker = [
        {"BP Code": "X", "Email 1": "alice@c.com", "Email 2": None,
         "KAM Email": "kam@1mg.com"},
        {"BP Code": "X", "Email 1": "alice@c.com", "Email 2": "bob@c.com",
         "KAM Email": "kam@1mg.com"},
    ]
    res = resolve(tracker, business_key_parts=["X"], config=_cfg())
    assert res.to.count("alice@c.com") == 1
    assert "bob@c.com" in res.to
    assert res.cc.count("kam@1mg.com") == 1


def test_any_enabled_wins_when_some_duplicates_inactive():
    """Customer marked Active in row #2 must NOT be considered disabled
    just because row #1 says Inactive — most-permissive interpretation."""

    tracker = [
        {"BP Code": "H1", "Email 1": "a@c.com", "Status": "Inactive"},
        {"BP Code": "H1", "Email 1": "b@c.com", "Status": "Active"},
    ]
    cfg = _cfg(enabled_column="Status", enabled_true_values=("Active",))
    res = resolve(tracker, business_key_parts=["H1"], config=cfg)
    codes = {r["code"] for r in res.review_reasons}
    # ``tracker_disabled`` is NOT raised because row #2 is active.
    assert "tracker_disabled" not in codes
    # And both contacts make it through.
    assert "a@c.com" in res.to
    assert "b@c.com" in res.to


def test_all_disabled_duplicates_still_block():
    """If EVERY duplicate row is disabled, the row IS disabled — the
    permissive rule doesn't override an unambiguous "no"."""

    tracker = [
        {"BP Code": "H1", "Email 1": "a@c.com", "Status": "Inactive"},
        {"BP Code": "H1", "Email 1": "b@c.com", "Status": "Paused"},
    ]
    cfg = _cfg(enabled_column="Status", enabled_true_values=("Active",))
    res = resolve(tracker, business_key_parts=["H1"], config=cfg)
    codes = {r["code"] for r in res.review_reasons}
    assert "tracker_disabled" in codes


def test_dup_keys_code_is_non_blocking():
    """Pipeline contract: ``tracker_duplicate_keys`` lives in the
    non-blocking set, so a duplicate-row customer is NOT skipped at the
    skip-gate. Adding/removing codes from this set is the explicit knob."""

    assert "tracker_duplicate_keys" in _NON_BLOCKING_REVIEW_REASON_CODES
    # Sanity: blocking codes must NOT leak in here. ``tracker_not_found``
    # and ``no_primary_recipient`` are the canonical blockers; if either
    # ever shows up in this set, refunds are imminent.
    assert "tracker_not_found" not in _NON_BLOCKING_REVIEW_REASON_CODES
    assert "no_primary_recipient" not in _NON_BLOCKING_REVIEW_REASON_CODES


def test_dup_keys_does_not_cause_skip_in_pipeline_skip_gate():
    """Direct simulation of the pipeline skip-gate logic against a plan
    whose only review reason is ``tracker_duplicate_keys`` — the row
    must be marked ``approved`` (or ``rendered`` under approval mode),
    NEVER ``skipped``. Mirrors ``persist_plans`` in pipeline/process.py."""

    review_reasons = [
        {
            "code": "tracker_duplicate_keys",
            "detail": "merged 3 rows",
            "human_message": "x",
            "suggested_action": "y",
        }
    ]
    blocking = [
        r for r in review_reasons
        if r.get("code") not in _NON_BLOCKING_REVIEW_REASON_CODES
    ]
    assert blocking == [], (
        "tracker_duplicate_keys leaked into the blocking set — the row "
        "would be skipped, defeating the merge contract"
    )


def test_dup_keys_skip_gate_still_blocks_real_problems():
    """Mixing the informational dup-keys notice with a real blocker
    (e.g. ``tracker_disabled``) must still skip the row — informational
    codes never mask blocking ones."""

    review_reasons = [
        {"code": "tracker_duplicate_keys", "detail": ""},
        {"code": "tracker_disabled", "detail": ""},
    ]
    blocking = [
        r for r in review_reasons
        if r.get("code") not in _NON_BLOCKING_REVIEW_REASON_CODES
    ]
    assert len(blocking) == 1
    assert blocking[0]["code"] == "tracker_disabled"
