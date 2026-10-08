"""Resolver + renderer — recipient lookup and HTML rendering."""

from __future__ import annotations

from decimal import Decimal

from app.email_automation.engine.renderer import (
    RendererConfig,
    TableColumnSpec,
    render,
)
from app.email_automation.engine.resolver import ResolverConfig, resolve
from app.email_automation.engine.types import AggregatedClient, NormalizedRow


def test_resolver_matches_and_extracts_emails():
    tracker_rows = [
        {
            "BP Code": "H001",
            "Email 1": "alice@client.com",
            "Email 2": " bob@client.com ;  carol@client.com",
            "Email 3": None,
            "KAM Email": "kam@1mg.com",
            "Status": "Active",
        },
        {
            "BP Code": "H002",
            "Email 1": "x@y.com",
            "KAM Email": None,
            "Status": "Inactive",
        },
    ]
    cfg = ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1", "Email 2", "Email 3"),
        cc_columns=("KAM Email",),
        static_cc=("ar-desk@1mg.com",),
        enabled_column="Status",
        enabled_true_values=("Active",),
    )
    res = resolve(tracker_rows, business_key_parts=["H001"], config=cfg)
    assert res.to == ("alice@client.com", "bob@client.com", "carol@client.com")
    assert res.cc == ("ar-desk@1mg.com", "kam@1mg.com")
    assert res.review_reasons == ()


def test_resolver_reports_disabled_and_missing_tracker_rows():
    tracker_rows = [
        {"BP Code": "H1", "Email 1": "a@b.com", "Status": "Inactive"},
    ]
    cfg = ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1",),
        enabled_column="Status",
        enabled_true_values=("Active",),
    )
    # Row found but disabled.
    res = resolve(tracker_rows, business_key_parts=["H1"], config=cfg)
    reasons = {r["code"] for r in res.review_reasons}
    assert "tracker_disabled" in reasons

    # Row not found.
    res2 = resolve(tracker_rows, business_key_parts=["H2"], config=cfg)
    assert res2.to == ()
    assert any(r["code"] == "tracker_not_found" for r in res2.review_reasons)

    # Round-2 contract: every review reason carries a plain-language
    # ``human_message`` and a concrete ``suggested_action`` so the AR
    # analyst on the HITL queue knows what to do without escalating.
    not_found = next(
        r for r in res2.review_reasons if r["code"] == "tracker_not_found"
    )
    assert not_found["human_message"]
    assert "Add the customer" in not_found["suggested_action"]
    disabled = next(r for r in res.review_reasons if r["code"] == "tracker_disabled")
    assert disabled["human_message"]
    assert disabled["suggested_action"]


def test_resolver_composite_key_order_matches_config():
    tracker_rows = [
        {"Customer": "Foo Clinic", "CoCd": "1MGHC", "TO 1": "ops@foo.com"},
        {"Customer": "Foo Clinic", "CoCd": "1MGT", "TO 1": "other@foo.com"},
    ]
    cfg = ResolverConfig(
        key_columns=("Customer", "CoCd"),
        to_columns=("TO 1",),
    )
    res = resolve(
        tracker_rows, business_key_parts=["Foo Clinic", "1MGT"], config=cfg
    )
    assert res.to == ("other@foo.com",)


def test_renderer_subject_is_plain_text_body_is_escaped():
    """Subjects are plain text on the wire — escaping there shows ``&amp;`` literal in
    inboxes. HTML escaping must apply to the body only."""

    rows = [
        NormalizedRow(
            source_sheet="H(all)",
            values={
                "invoice no#0": "INV-<script>",
                "net amount pending#0": Decimal("123456.78"),
                "business unit#0": "e-Pharmacy",
            },
            raw={},
        ),
    ]
    client = AggregatedClient(
        business_key="H1",
        business_key_parts=("H1",),
        party_name="Acme <Pharma> & Co",
        rows=rows,
        totals={"0-1 months#0": Decimal("1234567.89")},
    )
    cfg = RendererConfig(
        subject_template="Reminder {party_name} total {total_outstanding_inr}",
        body_template=(
            "<b>{party_name}</b> outstanding {total_outstanding_inr} "
            "across {row_count} invoice(s){invoices_table_html}"
        ),
        invoice_columns=(
            TableColumnSpec(display="Invoice", source_key="invoice no#0"),
            TableColumnSpec(
                display="Net Pending", source_key="net amount pending#0",
                kind="decimal", align="right",
            ),
        ),
    )
    subject, body = render(client, cfg)
    # Subject: NOT HTML-escaped (recipient inbox shows it as-is).
    assert "Acme <Pharma> & Co" in subject
    assert "&amp;" not in subject
    # Body: HTML-escaped where untrusted data appears.
    assert "Acme &lt;Pharma&gt; &amp; Co" in body
    assert "&lt;script&gt;" in body
    # Indian grouping: 12,34,567.89
    assert "₹12,34,567.89" in subject
    assert "₹1,23,456.78" in body  # per-row Decimal re-grouped


def test_resolver_flags_duplicate_tracker_keys():
    """Two tracker rows for the same key → MERGE contacts (no row dropped),
    surface ``tracker_duplicate_keys`` as an informational notice so ops can
    clean the sheet at their own cadence."""

    tracker_rows = [
        {"BP Code": "H1", "Email 1": "a@x.com", "Status": "Active"},
        {"BP Code": "H1", "Email 1": "b@x.com", "Status": "Active"},
    ]
    cfg = ResolverConfig(
        key_columns=("BP Code",),
        to_columns=("Email 1",),
        enabled_column="Status",
        enabled_true_values=("Active",),
    )
    res = resolve(tracker_rows, business_key_parts=["H1"], config=cfg)
    codes = {r["code"] for r in res.review_reasons}
    assert "tracker_duplicate_keys" in codes
    dup = next(
        r for r in res.review_reasons if r["code"] == "tracker_duplicate_keys"
    )
    assert dup["human_message"]
    # The reason text now reflects the merge contract (not "we used the first").
    assert "merged" in dup["human_message"].lower()
    # Suggested action tells the operator to clean the sheet (whatever
    # phrasing they prefer — "de-duplicate", "remove the duplicates",
    # "keep one row per customer" all qualify). The intent test is that
    # the suggested action mentions duplicates somewhere.
    assert "duplicat" in dup["suggested_action"].lower()
    # MERGE: both rows' contacts come through; order = first-seen wins.
    assert res.to == ("a@x.com", "b@x.com")
