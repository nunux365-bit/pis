"""Hand-off taxonomy tests."""

from __future__ import annotations

from app.agents.responder_eval.handoff_taxonomy import (
    BUCKET_ORDER,
    HANDOFF_NOT_APPLICABLE,
    HANDOFF_TAXONOMY,
    JUDGE_HANDOFF_BUCKET_ENUM,
    JUDGE_HANDOFF_SUB_BUCKET_ENUM,
    handoff_row_fields,
    normalize_handoff_classification,
    validate_handoff_pair,
)


def test_every_sub_bucket_belongs_to_its_bucket():
    for bucket, subs in HANDOFF_TAXONOMY.items():
        assert bucket in BUCKET_ORDER
        for sub in subs:
            assert sub.startswith(f"{bucket}.")
            assert validate_handoff_pair(bucket, sub)


def test_judge_enums_include_not_applicable():
    assert HANDOFF_NOT_APPLICABLE in JUDGE_HANDOFF_BUCKET_ENUM
    assert HANDOFF_NOT_APPLICABLE in JUDGE_HANDOFF_SUB_BUCKET_ENUM


def test_normalize_handoff_without_human():
    assert normalize_handoff_classification("user_self_service", "user_self_service.others", has_human=False) == (
        None,
        None,
    )


def test_normalize_handoff_repairs_mismatched_bucket():
    bucket, sub = normalize_handoff_classification(
        "delivery_disputes",
        "user_self_service.tracking_status_check",
        has_human=True,
    )
    assert bucket == "user_self_service"
    assert sub == "user_self_service.tracking_status_check"


def test_normalize_handoff_repairs_invalid_bucket_from_sub():
    bucket, sub = normalize_handoff_classification(
        "not_a_bucket",
        "delivery_disputes.ghost_delivery",
        has_human=True,
    )
    assert bucket == "delivery_disputes"
    assert sub == "delivery_disputes.ghost_delivery"


def test_normalize_handoff_falls_back_to_others_for_unknown_sub():
    bucket, sub = normalize_handoff_classification(
        "refund_return",
        "refund_return.not_a_real_sub",
        has_human=True,
    )
    assert bucket == "refund_return"
    assert sub == "refund_return.others"


def test_normalize_rejects_mismatched_bucket_sub_pair():
    bucket, sub = normalize_handoff_classification(
        "refund_return",
        "delivery_disputes.ghost_delivery",
        has_human=True,
    )
    assert bucket == "delivery_disputes"
    assert sub == "delivery_disputes.ghost_delivery"


def test_handoff_row_fields_from_judge_output():
    row = handoff_row_fields(
        {
            "handoff_bucket": "refund_return",
            "handoff_sub_bucket": "refund_return.new_request",
            "segment_evals": {"human_agent": {"present": True}},
        }
    )
    assert row["handoff_bucket"] == "refund_return"
    assert row["handoff_sub_bucket"] == "refund_return.new_request"


def test_handoff_row_fields_from_scored_segment_without_present():
    row = handoff_row_fields(
        {
            "handoff_bucket": "delivery_disputes",
            "handoff_sub_bucket": "delivery_disputes.ghost_delivery",
            "segment_evals": {
                "human_agent": {"graded": True, "composite_score": 80, "turn_indexes": [2]}
            },
        }
    )
    assert row["handoff_bucket"] == "delivery_disputes"
    assert row["handoff_sub_bucket"] == "delivery_disputes.ghost_delivery"
