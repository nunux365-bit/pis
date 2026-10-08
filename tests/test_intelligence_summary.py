"""Unit tests for intelligence summary aggregation logic.

The summary endpoint uses two separate data sources:
  - Snapshot (Excel-parsed): party overdue ₹ amounts, keyed by HANA/SAP code
  - DB tables: latest send (email_automation_sends) and latest reply
               classification (gmail_intelligence) per snapshot party

Engagement counts (Emails Delivered, Replies Received, Reply Rate, Reply
Breakdown) are scoped to the snapshot party_map AND windowed to the trailing
``REPLY_TRACKER_WINDOW_DAYS`` so the summary cards reconcile exactly with the
party-level detail table / exported sheet.  These tests verify the pure windowing
math stays correct.
"""

from datetime import datetime, timedelta, timezone

from app.api.routes.email_automation import (
    REPLY_TRACKER_WINDOW_DAYS,
    _ts_within,
    reply_tracker_window_start,
)


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def test_window_start_is_midnight_utc():
    """Window start is anchored to 00:00 UTC, N days back — not a rolling time.

    Anchoring to midnight is what makes a send dated exactly N days ago either
    fully in or fully out of the window, so the cards line up with a calendar-date
    filter on the exported sheet (the 167-vs-165 regression we chased).
    """
    start = reply_tracker_window_start()
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.tzinfo is timezone.utc
    expected_date = (datetime.now(timezone.utc) - timedelta(days=REPLY_TRACKER_WINDOW_DAYS)).date()
    assert start.date() == expected_date


def test_ts_within_boundary_and_none():
    since = reply_tracker_window_start()
    assert _ts_within(since, since=since) is True               # lower bound inclusive
    assert _ts_within(since - timedelta(seconds=1), since=since) is False
    assert _ts_within(since + timedelta(days=1), since=since) is True
    assert _ts_within(None, since=since) is False               # never-sent / never-replied


def test_send_dated_exactly_7_days_ago_is_fully_included():
    """A send any time on the boundary date counts — the calendar-day fix.

    Before the midnight anchor, a send earlier in the day than 'now minus 7d'
    was dropped, making the card read one less than the sheet's date filter.
    """
    since = reply_tracker_window_start()           # boundary date at 00:00 UTC
    boundary_date = since.date()
    early = datetime(boundary_date.year, boundary_date.month, boundary_date.day, 0, 30, tzinfo=timezone.utc)
    late = datetime(boundary_date.year, boundary_date.month, boundary_date.day, 23, 59, tzinfo=timezone.utc)
    assert _ts_within(early, since=since) is True
    assert _ts_within(late, since=since) is True


def test_reply_rate_zero_when_no_emails():
    emails_delivered = 0
    replies_received = 0
    reply_rate = (replies_received / emails_delivered * 100) if emails_delivered else 0.0
    assert reply_rate == 0.0


def test_reply_rate_calculation():
    emails_delivered = 225
    replies_received = 68
    reply_rate = round((replies_received / emails_delivered * 100), 1)
    assert reply_rate == 30.2


def test_category_pct_sums_to_100():
    """Category percentages must sum to ~100% (float rounding tolerance)."""
    category_totals = {
        "internal_processing": {"count": 26, "amount": 110.0},
        "recon_pending": {"count": 21, "amount": 79.8},
        "request_for_more_info": {"count": 13, "amount": 15.1},
        "discrepancy_or_issues": {"count": 5, "amount": 4.5},
        "email_poc_issues": {"count": 2, "amount": 12.5},
        "automated_reply": {"count": 1, "amount": 1.0},
    }
    replies_received = sum(v["count"] for v in category_totals.values())
    pcts = [
        round(v["count"] / replies_received * 100, 1)
        for v in category_totals.values()
    ]
    assert abs(sum(pcts) - 100.0) < 1.0  # rounding tolerance


def test_category_breakdown_sorted_by_count_desc():
    from collections import defaultdict

    category_totals: dict = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for cat, count in [("b", 10), ("a", 20), ("c", 5)]:
        category_totals[cat]["count"] += count

    sorted_cats = sorted(category_totals.items(), key=lambda x: -x[1]["count"])
    assert [c for c, _ in sorted_cats] == ["a", "b", "c"]


def test_kpi_totals_logic():
    """KPI aggregation: in-campaign and replies are subsets of the full book."""
    party_map = {
        "HANA001": {"overdue": 100.0},
        "HANA002": {"overdue": 50.0},
        "HANA003": {"overdue": 25.0},
    }
    latest_send = {"HANA001", "HANA002"}   # 2 emailed
    latest_intel = {"HANA001"}             # 1 replied

    book = round(sum(v["overdue"] for v in party_map.values()), 1)
    in_campaign = round(sum(party_map[k]["overdue"] for k in party_map if k in latest_send), 1)
    replies = round(sum(party_map[k]["overdue"] for k in party_map if k in latest_intel), 1)

    assert book == 175.0
    assert in_campaign == 150.0
    assert replies == 100.0
    assert replies <= in_campaign <= book


def _sn(sent_at=None, classified_at=None):
    """Stand-in for a latest_send / latest_intel row with a timestamp column."""
    from types import SimpleNamespace

    return SimpleNamespace(sent_at=sent_at, classified_at=classified_at)


def test_send_in_window_is_send_anchored():
    """`_send_in_window` keys purely on the send date — a reply never rescues a
    party whose email was sent before the window."""
    from app.api.routes.email_automation import _send_in_window, reply_tracker_window_start

    since = reply_tracker_window_start()

    assert _send_in_window(_sn(sent_at=_days_ago(2)), since=since) is True    # sent in-window
    assert _send_in_window(_sn(sent_at=_days_ago(30)), since=since) is False  # sent before window
    assert _send_in_window(None, since=since) is False                       # never sent


def test_window_start_parameterized_7_14_30():
    """The window start honours the requested view (7 / 14 / 30 days), midnight-anchored."""
    from datetime import timedelta

    from app.api.routes.email_automation import reply_tracker_window_start

    midnight_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for days in (7, 14, 30):
        start = reply_tracker_window_start(days)
        assert start == midnight_today - timedelta(days=days)


def test_reply_to_out_of_window_send_not_counted():
    """Send-anchored: a recent reply to an email sent *before* the window does NOT
    count, and Reply Rate stays ≤ 100% (replies ⊆ emailed-in-window)."""
    from app.api.routes.email_automation import _send_in_window, reply_tracker_window_start

    since = reply_tracker_window_start()

    latest_send = {
        "1": _sn(sent_at=_days_ago(1)),    # sent in-window
        "2": _sn(sent_at=_days_ago(2)),    # sent in-window
        "3": _sn(sent_at=_days_ago(20)),   # sent before window, but replied this week
    }
    latest_intel = {
        "2": _sn(classified_at=_days_ago(1)),   # reply to an in-window send -> counts
        "3": _sn(classified_at=_days_ago(1)),   # reply to an out-of-window send -> ignored
    }

    in_window = [c for c in latest_send if _send_in_window(latest_send.get(c), since=since)]
    emails_delivered = len(in_window)                                   # 1, 2
    replies_received = sum(1 for c in in_window if c in latest_intel)   # only 2
    reply_rate = round(replies_received / emails_delivered * 100, 1) if emails_delivered else 0.0

    assert emails_delivered == 2
    assert replies_received == 1     # party 3's reply excluded (its email predates the window)
    assert reply_rate == 50.0
    assert reply_rate <= 100.0


def test_party_table_reply_category_matches_breakdown():
    """Send-anchored: the party-table reply category is shown only when the party's
    email was sent in-window, so a reply-category filter on the table returns exactly
    the Reply Breakdown card count — a reply to an out-of-window send is dropped from
    both.
    """
    from collections import Counter

    from app.api.routes.email_automation import _send_in_window, reply_tracker_window_start

    since = reply_tracker_window_start()

    # per party: (latest send, latest reply category)
    parties = {
        "1000001": (_sn(sent_at=_days_ago(1)), "recon_pending"),          # sent in-window
        "1000002": (_sn(sent_at=_days_ago(2)), "recon_pending"),          # sent in-window
        "1000003": (_sn(sent_at=_days_ago(3)), "internal_processing"),    # sent in-window
        "1000004": (_sn(sent_at=_days_ago(40)), "recon_pending"),         # sent BEFORE window -> dropped
        "1000005": (None, "recon_pending"),                              # never sent -> dropped
    }

    # Breakdown card: category counted only when the send is in-window
    card = Counter(
        cat for (send, cat) in parties.values() if _send_in_window(send, since=since)
    )

    # Party table: reply_category blanked when send is out-of-window (endpoint _row logic)
    def row_category(send, cat):
        return cat if _send_in_window(send, since=since) else None

    table_rows = [row_category(send, cat) for (send, cat) in parties.values()]
    table_recon = sum(1 for c in table_rows if c == "recon_pending")

    assert card["recon_pending"] == 2            # out-of-window + never-sent excluded
    assert table_recon == card["recon_pending"]  # table filter == breakdown card
    assert card["internal_processing"] == 1


def test_category_breakdown_uses_latest_per_key():
    """Each business_key contributes exactly once to the breakdown (latest classification)."""
    from collections import defaultdict

    # Simulate latest-per-key intel rows (window function deduplicated)
    latest_intel = [
        {"business_key": "1000001", "category": "recon_pending"},
        {"business_key": "1000002", "category": "recon_pending"},
        {"business_key": "1000003", "category": "internal_processing"},
        # 1000001 appears twice in raw table but only once here (latest row)
    ]

    cat_totals: dict = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for row in latest_intel:
        cat_totals[row["category"]]["count"] += 1

    assert cat_totals["recon_pending"]["count"] == 2
    assert cat_totals["internal_processing"]["count"] == 1
    total = sum(v["count"] for v in cat_totals.values())
    assert total == len(latest_intel)  # one entry per unique business_key


def test_engagement_counts_windowed_send_anchored():
    """Summary engagement counts are send-anchored to the window.

    "Emails Delivered" = parties whose latest send is in-window; "Replies Received"
    = the subset of those that also replied.  A party sent before the window does not
    count even if it replied this week.
    """
    from app.api.routes.email_automation import _send_in_window, reply_tracker_window_start

    since = reply_tracker_window_start()

    latest_send = {
        "1000001": _sn(sent_at=_days_ago(1)),    # sent in-window
        "1000002": _sn(sent_at=_days_ago(3)),    # sent in-window
        "1000003": _sn(sent_at=_days_ago(30)),   # sent too old
        "1000004": _sn(sent_at=_days_ago(30)),   # sent too old, but replied this week
    }
    latest_intel = {
        "1000002": _sn(classified_at=_days_ago(2)),   # reply to in-window send -> counts
        "1000004": _sn(classified_at=_days_ago(2)),   # reply to out-of-window send -> ignored
    }

    in_window = [c for c in latest_send if _send_in_window(latest_send.get(c), since=since)]
    emails_delivered = len(in_window)                                 # 1000001, 1000002
    replies_received = sum(1 for c in in_window if c in latest_intel)  # only 1000002
    reply_rate = round(replies_received / emails_delivered * 100, 1) if emails_delivered else 0.0

    assert emails_delivered == 2
    assert replies_received == 1
    assert reply_rate == 50.0


def test_overdue_amounts_zero_on_key_mismatch():
    """Overdue amounts are 0 when snapshot codes don't match send/intel business_keys.

    This is expected until the snapshot is re-ingested with all_parties.
    """
    party_map = {
        "HOSP-SAP-1000016588 & 1000016589": {"overdue": 1847.3},  # composite HANA
        "HOSP-SAP-1000016592 & 1000016593": {"overdue": 1160.7},
    }
    # Send/intel business_keys are plain SAP codes — no overlap with party_map
    send_keys = {"1000000001", "1000001144", "1000001162"}
    intel_keys = {"1000001184", "1000001189"}

    in_campaign = sum(party_map[k]["overdue"] for k in party_map if k in send_keys)
    replies = sum(party_map[k]["overdue"] for k in party_map if k in intel_keys)

    assert in_campaign == 0.0
    assert replies == 0.0
