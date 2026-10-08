"""Hand-off dashboard aggregate tests."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.agents.responder_eval import handoff_dashboard
from app.agents.responder_eval.handoff_dashboard import (
    _ReferenceWindow,
    _chat_ids_window,
    _day_rows,
    _reference_window,
    build_movers,
    day_header,
)


def test_day_rows_percentages_sum_to_bucket_total():
    counts = {
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 3,
        ("delivery_disputes", "delivery_disputes.tracking_inquiry"): 1,
        ("refund_return", "refund_return.new_request"): 2,
    }
    total = 6
    buckets = _day_rows(counts, total=total)
    delivery = next(b for b in buckets if b["bucket"] == "delivery_disputes")
    assert delivery["count"] == 4
    assert delivery["pct"] == round(100.0 * 4 / 6, 1)
    subs = {s["id"]: s for s in delivery["sub_buckets"]}
    assert subs["delivery_disputes.ghost_delivery"]["count"] == 3
    assert subs["delivery_disputes.ghost_delivery"]["pct"] == round(100.0 * 3 / 6, 1)
    assert subs["delivery_disputes.tracking_inquiry"]["count"] == 1


def test_day_rows_keeps_unknown_sub_in_bucket_total():
    counts = {
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 2,
        ("delivery_disputes", "delivery_disputes.legacy_unknown"): 1,
    }
    buckets = _day_rows(counts, total=3)
    delivery = next(b for b in buckets if b["bucket"] == "delivery_disputes")
    assert delivery["count"] == 3
    ids = [s["id"] for s in delivery["sub_buckets"]]
    assert "delivery_disputes.ghost_delivery" in ids
    assert "delivery_disputes.legacy_unknown" in ids


def test_day_header_matches_prototype():
    assert day_header(date(2026, 8, 11)) == "11 AUG TUE"
    assert day_header(date(2026, 8, 17)) == "17 AUG MON"


def test_movers_exclude_noise_and_split_rising_falling():
    d1 = {
        ("payment_billing", "payment_billing.coupon_discount"): 16,
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 4,
        ("refund_return", "refund_return.new_request"): 17,
    }
    last7 = {
        ("payment_billing", "payment_billing.coupon_discount"): 56,  # avg 8 → +8
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 70,
        ("refund_return", "refund_return.new_request"): 140,  # avg 20 → -3
    }
    movers = build_movers(d1_counts=d1, last7_counts=last7)
    rising_ids = [m["id"] for m in movers["rising"]]
    falling_ids = [m["id"] for m in movers["falling"]]
    assert "payment_billing.coupon_discount" in rising_ids
    assert movers["rising"][0]["delta"] == 8
    assert movers["rising"][0]["delta_pct"] == 100
    assert "delivery_disputes.ghost_delivery" not in rising_ids
    assert "delivery_disputes.ghost_delivery" not in falling_ids
    assert "refund_return.new_request" in falling_ids
    assert movers["falling"][0]["delta"] == -3


def test_movers_are_top_five_sub_buckets():
    d1 = {("delivery_disputes", f"delivery_disputes.sub_{i}"): 10 + i for i in range(8)}
    last7 = {("delivery_disputes", f"delivery_disputes.sub_{i}"): 35 for i in range(8)}
    movers = build_movers(d1_counts=d1, last7_counts=last7)
    assert len(movers["rising"]) == 5
    assert [m["count"] for m in movers["rising"]] == [17, 16, 15, 14, 13]



def test_day_rows_percentages_sum_to_bucket_total():
    counts = {
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 3,
        ("delivery_disputes", "delivery_disputes.tracking_inquiry"): 1,
        ("refund_return", "refund_return.new_request"): 2,
    }
    total = 6
    buckets = _day_rows(counts, total=total)
    delivery = next(b for b in buckets if b["bucket"] == "delivery_disputes")
    assert delivery["count"] == 4
    assert delivery["pct"] == round(100.0 * 4 / 6, 1)
    subs = {s["id"]: s for s in delivery["sub_buckets"]}
    assert subs["delivery_disputes.ghost_delivery"]["count"] == 3
    assert subs["delivery_disputes.ghost_delivery"]["pct"] == round(100.0 * 3 / 6, 1)
    assert subs["delivery_disputes.tracking_inquiry"]["count"] == 1


def test_day_rows_keeps_unknown_sub_in_bucket_total():
    counts = {
        ("delivery_disputes", "delivery_disputes.ghost_delivery"): 2,
        ("delivery_disputes", "delivery_disputes.legacy_unknown"): 1,
    }
    buckets = _day_rows(counts, total=3)
    delivery = next(b for b in buckets if b["bucket"] == "delivery_disputes")
    assert delivery["count"] == 3
    ids = [s["id"] for s in delivery["sub_buckets"]]
    assert "delivery_disputes.ghost_delivery" in ids
    assert "delivery_disputes.legacy_unknown" in ids


def test_reference_window_invariants():
    """`_reference_window` drives both the matrix and the chat-ids lookup — its internal
    dates must stay mutually consistent regardless of when the test runs."""
    ref = _reference_window()
    assert ref.d1 == ref.today - timedelta(days=1)
    assert ref.d2 == ref.d1 - timedelta(days=1)
    assert len(ref.last7_days) == 7
    assert ref.last7_days[-1] == ref.d1
    assert ref.last7_days[0] == ref.d1 - timedelta(days=6)
    assert ref.last7_days == sorted(ref.last7_days)
    assert ref.mtd_start.day == 1
    assert ref.mtd_start <= ref.d1
    assert ref.mtd_days[0] == ref.mtd_start
    assert ref.mtd_days[-1] == ref.d1


def test_chat_ids_window_day_scope_is_exact_regardless_of_now():
    since, until = _chat_ids_window(scope="day", day=date(2026, 8, 1))
    assert since == until == date(2026, 8, 1)


def test_chat_ids_window_day_scope_requires_day():
    with pytest.raises(ValueError):
        _chat_ids_window(scope="day", day=None)


def _fixed_window(d1: date) -> _ReferenceWindow:
    d2 = d1 - timedelta(days=1)
    last7_days = [d1 - timedelta(days=offset) for offset in range(6, -1, -1)]
    mtd_start = d1.replace(day=1)
    mtd_days = [mtd_start + timedelta(days=i) for i in range((d1 - mtd_start).days + 1)]
    return _ReferenceWindow(
        today=d1 + timedelta(days=1),
        d1=d1,
        d2=d2,
        last7_days=last7_days,
        mtd_start=mtd_start,
        mtd_days=mtd_days,
    )


def test_chat_ids_window_last_7_matches_reference_window(monkeypatch):
    monkeypatch.setattr(handoff_dashboard, "_reference_window", lambda: _fixed_window(date(2026, 9, 9)))
    since, until = _chat_ids_window(scope="last_7", day=None)
    assert since == date(2026, 9, 3)
    assert until == date(2026, 9, 9)


def test_chat_ids_window_mtd_matches_reference_window(monkeypatch):
    monkeypatch.setattr(handoff_dashboard, "_reference_window", lambda: _fixed_window(date(2026, 9, 9)))
    since, until = _chat_ids_window(scope="mtd", day=None)
    assert since == date(2026, 9, 1)
    assert until == date(2026, 9, 9)


def test_chat_ids_window_mtd_on_first_of_month_is_a_single_day(monkeypatch):
    monkeypatch.setattr(handoff_dashboard, "_reference_window", lambda: _fixed_window(date(2026, 9, 1)))
    since, until = _chat_ids_window(scope="mtd", day=None)
    assert since == until == date(2026, 9, 1)
