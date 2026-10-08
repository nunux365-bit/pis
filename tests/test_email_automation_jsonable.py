"""Tests for pipeline JSON serialization helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from app.email_automation.pipeline._shared import jsonable


def test_jsonable_serializes_decimal_datetime_date_and_time() -> None:
    payload = {
        "amount": Decimal("1234.50"),
        "received_at": datetime(2026, 8, 10, 9, 55, 32, tzinfo=timezone.utc),
        "invoice_date": date(2026, 7, 31),
        "posted_at": time(14, 30, 15),
    }

    out = jsonable(payload)

    assert out == {
        "amount": "1234.50",
        "received_at": "2026-08-10T09:55:32+00:00",
        "invoice_date": "2026-07-31",
        "posted_at": "14:30:15",
    }


def test_jsonable_serializes_nested_sample_rows_shape() -> None:
    """Regression: raw Excel rows may include ``datetime.time`` in sample_rows."""

    payload = {
        "variant": "chw",
        "sample_rows": [
            {
                "hana code": "1000008550",
                "invoice date": date(2026, 7, 15),
                "last updated": time(10, 22, 17),
            }
        ],
    }

    out = jsonable(payload)

    assert out["sample_rows"][0]["invoice date"] == "2026-07-15"
    assert out["sample_rows"][0]["last updated"] == "10:22:17"


def test_jsonable_matches_run_variant_aggregated_snapshot_shape() -> None:
    """Mirrors ``run_variant``'s ``aggregated_snapshot`` payload at DB persist time."""

    plan = {
        "business_key_parts": ["1000008550"],
        "row_count": 3,
        "totals": {"net_pending": "12345.67"},
        "source_sheets": ["Invoice details-H&T"],
        "sample_rows": [
            {
                "hana code#0": "1000008550",
                "invoice date#0": date(2026, 7, 15),
                "period#0": time(10, 22, 17),
            }
        ],
        "client_recipient_missing": False,
        "client_not_in_tracker": False,
    }

    aggregated_snapshot = jsonable(
        {
            "variant": "chw",
            "business_key_parts": plan.get("business_key_parts") or [],
            "row_count": plan.get("row_count") or 0,
            "totals": plan.get("totals") or {},
            "source_sheets": plan.get("source_sheets") or [],
            "period_key": "2026-W33",
            "sample_rows": plan.get("sample_rows") or [],
            "client_recipient_missing": bool(plan.get("client_recipient_missing")),
            "client_not_in_tracker": bool(plan.get("client_not_in_tracker")),
        }
    )

    row = aggregated_snapshot["sample_rows"][0]
    assert row["invoice date#0"] == "2026-07-15"
    assert row["period#0"] == "10:22:17"


def test_jsonable_rejects_unknown_types() -> None:
    with pytest.raises(TypeError, match="Unserializable"):
        jsonable({"bad": object()})
