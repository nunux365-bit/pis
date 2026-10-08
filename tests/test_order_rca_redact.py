"""Order RCA PII redaction."""

from __future__ import annotations

import json

from app.agents.order_rca.redact import redact_free_text
from app.agents.order_rca import rules, sources


def test_redact_phone_and_email():
    text = "Call 9818886159 or email user@example.com about cancel"
    out = redact_free_text(text)
    assert "9818886159" not in (out or "")
    assert "user@example.com" not in (out or "")
    assert "cancel" in (out or "")


def test_groot_comments_redacted_in_facts_cross_path():
    """CROSS path parses groot; comments must not contain raw phone/email."""
    bundle = {
        "order_id": "PO_TEST",
        "order": {
            "order_id": "PO_TEST",
            "delivery_address": {"city": "X", "state": "Y", "pincode": "110001"},
            "eta": {"eta_to": 1779058800.0},
            "order_lines": [],
            "shipment_detail": {},
        },
        "parent_id": None,
        "allocation": {
            "data": {
                "PO_TEST": {
                    "allocated_vendor": "2",
                    "selected_vendors": {
                        "1": {
                            "vendor_id": 1,
                            "vendor_code": "1MG_NEAR_02",
                            "vendor_type": "RETAIL",
                            "distance": 1,
                        },
                        "2": {
                            "vendor_id": 2,
                            "vendor_code": "1MG_FAR_01",
                            "vendor_type": "WAREHOUSE",
                            "distance": 500,
                        },
                    },
                    "rejected_vendors": {"1": {"vendor_id": 1, "vendor_code": "1MG_NEAR_02", "vendor_type": "RETAIL", "distance": 1}},
                }
            }
        },
        "status": {"data": {"PO_TEST": []}},
        "history": {},
        "groot": {
            "data": {
                "day1": [
                    {
                        "status": "on_the_way",
                        "comments": "Rider 9818886159 retry user@test.com",
                        "performed_at": "17-05-2026 14:30:00",
                    }
                ]
            }
        },
    }
    facts = rules.build_facts(bundle)
    assert facts["preflight"]["allocation_badge"] == "CROSS"
    ev = facts["operations"]["groot_events"]
    assert ev
    assert "9818886159" not in ev[0]["comments"]
    assert "user@test.com" not in ev[0]["comments"]
