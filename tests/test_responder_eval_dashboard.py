"""Dashboard aggregate tests for responder eval."""

from __future__ import annotations

from datetime import UTC, timedelta

from app.agents.responder_eval.dashboard import (
    _round_avg,
    _window_bounds,
    _window_filters,
)


def test_round_avg():
    assert _round_avg(None) is None
    assert _round_avg(81.26) == 81.3
    assert _round_avg(0) == 0.0


def test_window_bounds_rolling():
    since, until = _window_bounds(30)
    assert until > since
    assert until - since == timedelta(days=30)
    assert until.tzinfo == UTC


def test_window_filters_tuple_length():
    since, until = _window_bounds(7)
    filters = _window_filters(since, until)
    assert len(filters) == 3


def test_count_issue_tags_splits_by_grade_and_bot_cohort():
    from types import SimpleNamespace

    from app.agents.responder_eval.dashboard import _count_issue_tags

    rows = [
        SimpleNamespace(
            letter_grade="E",
            bot_grade="C",
            human_grade="D",
            bot_score=55,
            human_score=40,
            composite_issues=[
                "hallucination",
                "hallucination:false_refund",
                "hallucination_severity:P0",
            ],
            bot_issues=["resolution:partial", "resolution_lifecycle:in_transit"],
            human_issues=["resolution:no"],
        ),
        SimpleNamespace(
            letter_grade="C",
            bot_grade="D",
            human_grade="-",
            bot_score=30,
            human_score=None,
            composite_issues=["traceability_failure", "traceability:unsupported_refund"],
            bot_issues=["guardrail:no_links"],
            human_issues=[],
        ),
        SimpleNamespace(
            letter_grade="E",
            bot_grade="-",
            human_grade="-",
            bot_score=None,
            human_score=None,
            # Legacy tags still in DB until backfill.
            composite_issues=["hard_gate:hallucination_P0"],
            bot_issues=[],
            human_issues=[],
        ),
    ]
    out = _count_issue_tags(rows, limit=10)
    e_issues = {row["issue"]: row for row in out["composite"]["E"]}
    assert e_issues["hallucination"]["count"] == 2
    attr_ids = {a["id"] for a in e_issues["hallucination"]["attributes"]}
    assert "false_refund" in attr_ids
    assert "severity_P0" in attr_ids
    assert out["composite"]["C"][0]["issue"] == "traceability_failure"
    assert out["composite"]["C"][0]["attributes"][0]["id"] == "unsupported_refund"
    assert out["bot_pre_handoff"]["C"][0]["issue"] == "resolution:partial"
    assert out["bot_pre_handoff"]["C"][0]["attributes"][0]["id"] == "in_transit"
    assert out["bot_closed"]["D"][0]["issue"] == "guardrail:no_links"
    assert out["bot_closed"]["C"] == []
    assert out["human"]["D"][0]["issue"] == "resolution:no"

