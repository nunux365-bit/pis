"""Per-variant ``static_to`` recipient + ``client_recipient_missing`` banner.

Behavioural contract (per requirements):

1. ``static_to`` is appended to every rendered email's TO list in **production**.
2. ``test_mode=True`` strips ``static_to`` from the wire-bound TO list so the
   AR desk's mailbox isn't polluted with test traffic (and so DB audits
   unambiguously distinguish test vs prod intent).
3. When the customer's tracker row has TO **but** also has CC, the email
   goes to that CC (no banner — we have a real customer recipient).
4. When the customer's tracker row has neither TO nor CC, the variant's
   ``static_to`` becomes the SOLE recipient and the resolver flags
   ``client_recipient_missing=True`` so the pipeline injects a body banner.
5. Variants WITHOUT a configured ``static_to`` keep the legacy
   ``no_primary_recipient`` blocker so the row lands in ``skipped``.
"""

from __future__ import annotations

from app.email_automation.engine.resolver import ResolverConfig, resolve


_BASE_TRACKER = [
    {
        "BP Code": "H_FULL",
        "Email 1": "alice@client.com",
        "Email 2": "bob@client.com",
        "KAM Email": "kam@1mg.com",
    },
    {
        "BP Code": "H_TO_ONLY",
        "Email 1": "to-only@client.com",
        "KAM Email": None,
    },
    {
        "BP Code": "H_CC_ONLY",
        "Email 1": None,
        "KAM Email": "cc-only@client.com",
    },
    {
        "BP Code": "H_EMPTY",
        "Email 1": None,
        "Email 2": "  ",
        "KAM Email": None,
    },
]


def _epharma_cfg(*, with_static: bool = True) -> ResolverConfig:
    return ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1", "Email 2"),
        cc_columns=("KAM Email",),
        static_to=("ar-desk@1mg.com",) if with_static else (),
        static_cc=(),
    )


def test_static_to_is_appended_in_production():
    cfg = _epharma_cfg()
    res = resolve(_BASE_TRACKER, business_key_parts=["H_FULL"], config=cfg)
    assert res.to == ("alice@client.com", "bob@client.com", "ar-desk@1mg.com")
    assert res.cc == ("kam@1mg.com",)
    assert res.client_recipient_missing is False
    assert res.review_reasons == ()


def test_static_to_is_preserved_in_test_mode_preview():
    """Test mode must keep the business-intended TO list intact.

    The sender does the wire-level redirect separately; the resolver should
    still expose the real recipients so previews, persisted audit rows, and
    the in-body TEST MODE banner stay accurate."""

    cfg = _epharma_cfg()
    res = resolve(
        _BASE_TRACKER, business_key_parts=["H_FULL"], config=cfg, test_mode=True
    )
    assert res.to == ("alice@client.com", "bob@client.com", "ar-desk@1mg.com")
    assert res.client_recipient_missing is False


def test_client_with_to_only_is_normal_send():
    """TO present, CC absent → normal send, no banner."""

    cfg = _epharma_cfg()
    res = resolve(_BASE_TRACKER, business_key_parts=["H_TO_ONLY"], config=cfg)
    assert "to-only@client.com" in res.to
    assert "ar-desk@1mg.com" in res.to
    assert res.cc == ()
    assert res.client_recipient_missing is False


def test_client_with_cc_only_is_normal_send_no_banner():
    """No TO but CC present → still a real customer recipient (in CC).
    No banner needed; static_to provides the TO."""

    cfg = _epharma_cfg()
    res = resolve(_BASE_TRACKER, business_key_parts=["H_CC_ONLY"], config=cfg)
    # Static is the only TO since the customer's TO column was empty.
    assert res.to == ("ar-desk@1mg.com",)
    # Customer CC is preserved.
    assert res.cc == ("cc-only@client.com",)
    # Banner is NOT triggered because we have a customer recipient (in CC).
    assert res.client_recipient_missing is False
    # And no blocking review reason.
    assert all(
        r["code"] != "no_primary_recipient" for r in res.review_reasons
    )


def test_client_with_no_to_and_no_cc_triggers_banner():
    """Neither TO nor CC on the tracker row → static_to is the sole TO,
    banner flag fires so the pipeline injects an explanatory body banner."""

    cfg = _epharma_cfg()
    res = resolve(_BASE_TRACKER, business_key_parts=["H_EMPTY"], config=cfg)
    assert res.to == ("ar-desk@1mg.com",)
    assert res.cc == ()
    assert res.client_recipient_missing is True
    # No blocking ``no_primary_recipient`` because static_to covers TO.
    assert all(
        r["code"] != "no_primary_recipient" for r in res.review_reasons
    )


def test_no_static_configured_keeps_legacy_skip_behavior():
    """Variants without a static_to fallback still hit the legacy blocker
    when the tracker row has neither TO nor CC — the row lands in
    ``skipped`` and ops fix the tracker."""

    cfg = _epharma_cfg(with_static=False)
    res = resolve(_BASE_TRACKER, business_key_parts=["H_EMPTY"], config=cfg)
    assert res.to == ()
    # Banner flag is False — no static means no fallback recipient at all.
    assert res.client_recipient_missing is False
    codes = {r["code"] for r in res.review_reasons}
    assert "no_primary_recipient" in codes


def test_tracker_not_found_routes_to_static_when_configured():
    """When the customer is missing from the master tracker but the variant
    has a ``static_to`` fallback, the resolver routes the email to that
    static address (AR desk) and flags ``client_not_in_tracker`` so the
    pipeline injects the "customer not in tracker" banner. Tracker hygiene
    becomes housekeeping for ops, not a blocker for the customer."""

    cfg = _epharma_cfg()
    res = resolve(_BASE_TRACKER, business_key_parts=["H_GHOST"], config=cfg)
    # Static address is the sole TO; customer is unknown so no client emails.
    assert res.to == ("ar-desk@1mg.com",)
    assert res.cc == ()
    # New flag drives the second banner variant in process.py.
    assert res.client_not_in_tracker is True
    # Mutually exclusive with the recipient-missing flag.
    assert res.client_recipient_missing is False
    # No review reasons at all — the row ships clean. The audit signal
    # lives on aggregated_data so SQL queries can find these orphans
    # without parsing review reason JSON.
    assert res.review_reasons == ()


def test_tracker_not_found_test_mode_keeps_static_preview():
    """Unknown customers still preview as routing to the static fallback in
    test mode; only the sender changes the actual wire recipients."""

    cfg = _epharma_cfg()
    res = resolve(
        _BASE_TRACKER, business_key_parts=["H_GHOST"], config=cfg, test_mode=True
    )
    assert res.to == ("ar-desk@1mg.com",)
    assert res.client_not_in_tracker is True


def test_tracker_not_found_no_static_keeps_blocking_skip():
    """Variants without a ``static_to`` fallback can't route the orphan
    anywhere safe → we keep the legacy blocking ``tracker_not_found``
    review reason so the row lands in ``skipped`` and ops fix the tracker."""

    cfg = _epharma_cfg(with_static=False)
    res = resolve(_BASE_TRACKER, business_key_parts=["H_GHOST"], config=cfg)
    assert res.to == ()
    assert res.client_not_in_tracker is False
    codes = {r["code"] for r in res.review_reasons}
    assert "tracker_not_found" in codes


def test_static_to_dedup_against_customer_to():
    """If the AR desk address ALSO appears in the customer TO columns,
    it appears once in the final TO list (order preserved: customer first)."""

    tracker = [
        {
            "BP Code": "H_DUP",
            "Email 1": "ar-desk@1mg.com",
            "Email 2": "alice@client.com",
        }
    ]
    cfg = _epharma_cfg()
    res = resolve(tracker, business_key_parts=["H_DUP"], config=cfg)
    assert res.to == ("ar-desk@1mg.com", "alice@client.com")
    assert res.to.count("ar-desk@1mg.com") == 1


def test_cc_omitted_when_same_address_already_on_to():
    """An address must not appear on both To and Cc — Cc drops the overlap."""

    cfg = ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1", "Email 2"),
        cc_columns=("KAM Email", "CC 1"),
        static_to=("ar-desk@1mg.com",),
        static_cc=(),
    )
    tracker = [
        {
            "BP Code": "H1",
            "Email 1": "same@client.com",
            "Email 2": "other@client.com",
            "KAM Email": "kam@1mg.com",
            "CC 1": "same@client.com",
        }
    ]
    res = resolve(tracker, business_key_parts=["H1"], config=cfg)
    assert res.to == ("same@client.com", "other@client.com", "ar-desk@1mg.com")
    assert "same@client.com" not in res.cc
    assert "kam@1mg.com" in res.cc


def test_multiple_cc_columns_merge():
    """Mirrors ePharma master: KAM Email + Internal Team ID as two CC sources."""

    cfg = ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1",),
        cc_columns=("KAM Email", "Internal Team ID"),
        static_to=("ar-desk@1mg.com",),
        static_cc=(),
    )
    tracker = [
        {
            "BP Code": "H1",
            "Email 1": "client@x.com",
            "KAM Email": "kam@1mg.com",
            "Internal Team ID": "desk@1mg.com",
        }
    ]
    res = resolve(tracker, business_key_parts=["H1"], config=cfg)
    assert res.cc == ("kam@1mg.com", "desk@1mg.com")
