"""Tests for issue tag computation."""

from __future__ import annotations

from app.agents.responder_eval.issue_tags import (
    composite_issues,
    compute_issue_tags,
    eval_dict_for_issue_tags,
    segment_scoped_issues,
)
from app.agents.responder_eval.policy_buckets import normalize_policy_rule
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT


def test_normalize_policy_rule_buckets():
    assert normalize_policy_rule("order_selection_before_action") == "order_selection"
    assert normalize_policy_rule("Unsupported status/timing claim") == "unsupported_status_or_eta"
    assert normalize_policy_rule("unsupported_refund_timing_claim") == "unsupported_refund"
    assert normalize_policy_rule("professional_tone") == "tone"
    assert normalize_policy_rule("something_weird") == "other"


def test_composite_issues_hierarchical_parents_and_attrs():
    ev = {
        "violations": [{"rule": "order_selection_before_action", "turns": [1]}],
        "policy_violations": [{"rule": "Unsupported status/timing claim", "turns": [2]}],
        "hallucination_flagged": True,
        "resolution": "no",
        "policy_lifecycle_stage": "in_transit",
        "traceability_pass": False,
        "traceability_failure_reasons": ["unsupported_refund", "nuance_or_paraphrase"],
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_failure_modes": ["false_refund", "false_shipment"],
        "letter_grade": "E",
    }
    issues = set(composite_issues(ev))
    assert "guardrail:order_selection_before_action" in issues
    assert "policy:unsupported_status_or_eta" in issues
    assert "resolution:no" in issues
    assert "resolution_lifecycle:in_transit" in issues
    assert "traceability_failure" in issues
    assert "traceability:unsupported_refund" in issues
    assert "hard_gate:hallucination" not in issues
    assert "hallucination" in issues
    assert "hallucination_severity:P0" in issues
    assert "hallucination:false_refund" in issues
    assert "hallucination:false_shipment" in issues
    assert "hard_gate:hallucination_P0" not in issues


def test_composite_issues_dedupes_policy_echo_of_guardrail():
    ev = {
        "violations": [{"rule": "order_selection_before_action", "turns": [1]}],
        "policy_violations": [
            {"rule": "order_selection_before_action", "turns": [1]},
            {"rule": "unsupported_refund_claim", "turns": [2]},
        ],
        "resolution": "yes",
        "letter_grade": "C",
    }
    issues = set(composite_issues(ev))
    assert "guardrail:order_selection_before_action" in issues
    assert "policy:order_selection" not in issues
    assert "policy:unsupported_refund" in issues


def test_composite_issues_drops_unknown_mode_and_orphan_pii_flag():
    ev = {
        "pii_incident": True,  # orphan flag without hard_gate_reasons
        "pii_incident_types": ["phone"],
        "hallucination_flagged": True,
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_failure_modes": ["false_refund", "not_a_real_mode"],
        "letter_grade": "E",
    }
    issues = set(composite_issues(ev))
    assert "hard_gate:pii" not in issues
    assert "pii:phone" not in issues
    assert "hallucination" in issues
    assert "hard_gate:hallucination" not in issues
    assert "hallucination:false_refund" in issues
    assert "hallucination:not_a_real_mode" not in issues


def test_composite_issues_pii_hard_gate():
    ev = {
        "hard_gate_reasons": ["pii_incident"],
        "pii_incident": True,
        "pii_incident_types": ["phone", "email"],
        "resolution": "yes",
        "letter_grade": "E",
    }
    issues = set(composite_issues(ev))
    assert "hard_gate:pii" in issues
    assert "pii:phone" in issues
    assert "pii:email" in issues


def test_segment_scoped_issues_filters_by_turn_indexes():
    ev = {
        "violations": [
            {"rule": "no_pii", "turns": [1]},
            {"rule": "no_links", "turns": [3]},
        ],
        "policy_violations": [{"rule": "tone", "turns": [1]}],
        "policy_lifecycle_stage": "delivered",
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1, 2],
                "guardrail_violations": 1,
                "resolution": "partial",
                "letter_grade": "C",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [3],
                "resolution": "no",
                "letter_grade": "D",
            },
        },
    }
    bot_issues = set(segment_scoped_issues(ev, SEGMENT_BOT))
    assert "guardrail:no_pii" in bot_issues
    assert "policy:tone" in bot_issues
    assert "guardrail:no_links" not in bot_issues
    assert "guardrail_violations" not in bot_issues  # concrete guardrail:* present
    assert "resolution:partial" in bot_issues
    assert "resolution_lifecycle:delivered" in bot_issues

    human_issues = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "guardrail:no_links" in human_issues
    assert "guardrail:no_pii" not in human_issues
    assert "resolution:no" in human_issues


def test_segment_hard_gate_not_soft_and_modes_turn_scoped():
    """Hard-hal parent + attrs only on segments whose turns are implicated."""
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_turns": [3],
        "hallucination_failure_modes": ["false_refund"],
        "policy_lifecycle_stage": "delivered",
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [0, 1],
                "letter_grade": "E",
                "composite_score": 0,
                "hallucination_flagged": False,
                "resolution": "partial",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "E",
                "composite_score": 0,
                "hallucination_flagged": True,
                "hallucination_turns": [3],
                "resolution": "no",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" not in bot
    assert "hard_gate:hallucination" not in bot
    assert "hallucination:false_refund" not in bot
    assert "hallucination_severity:P0" not in bot
    assert "hard_gate:inherited_hallucination" in bot

    assert "hallucination" in human
    assert "hard_gate:hallucination" not in human
    assert "hallucination:false_refund" in human
    assert "hallucination_severity:P0" in human


def test_compute_issue_tags_only_for_low_grades():
    ev = {
        "letter_grade": "B",
        "resolution": "yes",
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "letter_grade": "B",
                "composite_score": 80,
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "letter_grade": "A",
                "composite_score": 95,
                "resolution": "yes",
            },
        },
    }
    tags = compute_issue_tags(ev)
    assert tags["composite_issues"] == []
    assert tags["bot_issues"] == []
    assert tags["human_issues"] == []


def test_eval_dict_for_issue_tags_uses_column_grades_when_segment_sparse():
    """Legacy rubric JSON may omit segment letter_grade; columns must drive gating."""
    ev = {
        "violations": [{"rule": "no_links", "turns": [3]}],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [3],
                "guardrail_violations": 1,
                "resolution": "no",
            },
        },
    }
    tags = compute_issue_tags(
        eval_dict_for_issue_tags(
            ev,
            letter_grade="B",
            bot_grade="D",
            human_grade="-",
            bot_score=40,
            human_score=None,
        )
    )
    assert tags["composite_issues"] == []
    assert "guardrail:no_links" in tags["bot_issues"]
    assert "resolution:no" in tags["bot_issues"]
    assert tags["human_issues"] == []


def test_eval_dict_for_issue_tags_column_grade_overrides_stale_segment_json():
    ev = {
        "violations": [{"rule": "no_links", "turns": [3]}],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "letter_grade": "B",
                "composite_score": 80,
                "turn_indexes": [3],
                "guardrail_violations": 1,
                "resolution": "no",
            },
        },
    }
    tags = compute_issue_tags(
        eval_dict_for_issue_tags(
            ev,
            letter_grade="B",
            bot_grade="D",
            human_grade="-",
            bot_score=40,
            human_score=None,
        )
    )
    assert tags["bot_issues"] != []
    assert "guardrail:no_links" in tags["bot_issues"]
