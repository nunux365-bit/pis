"""Promised ETA candidate collection and resolution."""

from __future__ import annotations

import pytest

from app.agents.order_rca import eta_resolution, rules, sources

CHILD = "PO13326295207344"
PARENT = "PO13326295017145"
RETURN_REFUND = "PO11526254696421"


@pytest.mark.asyncio
async def test_parent_promised_first_14_may_with_analytics():
    bundle = await sources._fetch_fixtures(PARENT)
    delivery = rules.extract_order_delivery(bundle)
    assert delivery["promised_delivery"] == "14 May, 2026 14:11 IST"
    assert "analytics.eta" in (delivery["promised_delivery_source"] or "")


@pytest.mark.asyncio
async def test_child_prefers_history_over_analytics_at_same_created():
    bundle = await sources._fetch_fixtures(CHILD)
    delivery = rules.extract_order_delivery(bundle)
    assert delivery["promised_delivery"] == "17 May, 2026 23:00 IST"
    assert "history.eta_communicated" in (delivery["promised_delivery_source"] or "")


def test_parse_comms_without_comma():
    dt, ht = eta_resolution._parse_customer_comms_string("17 May 2026 23:00")
    assert dt is not None and ht is True
    assert dt.day == 17 and dt.hour == 23


def test_parse_april_month_in_comms_string():
    dt, ht = eta_resolution._parse_customer_comms_string("27 April, 2026")
    assert dt is not None
    assert ht is False
    assert dt.month == 4 and dt.day == 27


def test_parse_april_with_time():
    dt, ht = eta_resolution._parse_customer_comms_string("29 April, 2026 00:00")
    assert dt is not None
    assert ht is True
    assert dt.hour == 0 and dt.minute == 0


def test_history_eta_communicated_uses_to_not_from():
    comment = "PO13426186269279|eta_communicated|from: 17 May, 2026|to: 20 May, 2026"
    from_p, to_p = eta_resolution.parse_eta_communicated_comment(comment)
    assert from_p == "17 May, 2026"
    assert to_p == "20 May, 2026"
    bundle = {
        "order_id": "PO13426186269279",
        "order": {
            "eta": {"eta_to": 1779926340.0},
            "shipment_detail": {},
        },
        "history": {
            "history": [
                {
                    "comment": comment,
                    "created": 1778822126,
                }
            ]
        },
    }
    d = rules.extract_order_delivery(bundle)
    assert "20 May" in d["promised_delivery"]
    assert "17 May" not in (d["promised_delivery"] or "").split("20")[0]


def test_return_refund_push_uses_to_instant():
    comment = "PO11526254696421|eta_communicated|from: 27 April, 2026|to: 29 April, 2026 00:00"
    from_p, to_p = eta_resolution.parse_eta_communicated_comment(comment)
    assert from_p == "27 April, 2026"
    assert to_p == "29 April, 2026 00:00"


def test_order_confirmation_eta_collected_from_order_payload():
    order = {
        "promised_eta": 1781374620,
        "confirmation_eta_information": {"order_confirmed_eta": 1781374620},
    }
    cands = eta_resolution.collect_eta_candidates("POX", order, {}, None)
    sources = {c.source for c in cands}
    assert "order.confirmation_eta_information" in sources
    assert "order.promised_eta" in sources


def test_build_eta_jumps_appends_snapshot_as_eta_n_not_current():
    timeline = [
        {
            "instant": "2026-06-13T23:47:00+05:30",
            "display": "13 Jun, 2026 23:47 IST",
            "source": "history.eta_communicated",
        },
        {
            "instant": "2026-06-15T23:59:00+05:30",
            "display": "15 Jun, 2026 23:59 IST",
            "source": "history.eta_communicated",
        },
    ]
    current = {
        "instant": "2026-06-16T23:59:00+05:30",
        "display": "16 Jun, 2026 23:59 IST",
        "source": "order.eta.eta_to",
    }
    jumps = eta_resolution.build_eta_jumps(
        promised_first=timeline[0],
        promised_current=current,
        eta_timeline=timeline,
        actual_display="16 Jun, 2026 10:13 IST",
    )
    labels = [j["label"] for j in jumps]
    assert labels == ["First promised", "ETA 1", "ETA 2", "Actual"]
    assert "Current" not in labels


def test_build_eta_jumps_omits_duplicate_snapshot_instant():
    timeline = [
        {
            "instant": "2026-06-13T23:47:00+05:30",
            "display": "13 Jun, 2026 23:47 IST",
            "source": "history.eta_communicated",
        },
        {
            "instant": "2026-06-16T23:59:00+05:30",
            "display": "16 Jun, 2026 23:59 IST",
            "source": "history.eta_communicated",
        },
    ]
    current = {
        "instant": "2026-06-16T23:59:00+05:30",
        "display": "16 Jun, 2026 23:59 IST",
        "source": "order.eta.eta_to",
    }
    jumps = eta_resolution.build_eta_jumps(
        promised_first=timeline[0],
        promised_current=current,
        eta_timeline=timeline,
        actual_display="16 Jun, 2026 10:13 IST",
    )
    labels = [j["label"] for j in jumps]
    assert labels == ["First promised", "ETA 1", "Actual"]
