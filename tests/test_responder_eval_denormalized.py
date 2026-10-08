"""Tests for denormalized responder eval storage."""

from __future__ import annotations

from app.agents.responder_eval.denormalized import (
    build_persist_row_fields,
    merge_eval_detail_json,
    split_eval_storage,
)
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT


def test_split_eval_storage_removes_bulk_keys():
    result = {
        "chat_id": "c1",
        "resolution": "partial",
        "chat": {"messages": [{"role": "user", "content": "hi"}]},
        "eval_ground_truth": {"preflight": {"order_status": "Delivered"}},
        "composite_score": 55,
        "letter_grade": "C",
    }
    rubric, chat, gt = split_eval_storage(result)
    assert "chat" not in rubric
    assert "eval_ground_truth" not in rubric
    assert rubric["resolution"] == "partial"
    assert chat == {"messages": [{"role": "user", "content": "hi"}]}
    assert gt == {"preflight": {"order_status": "Delivered"}}


def test_split_eval_storage_strips_handoff_denorm_keys():
    result = {
        "resolution": "yes",
        "handoff_bucket": "refund_return",
        "handoff_sub_bucket": "refund_return.new_request",
        "chat": {"messages": []},
    }
    rubric, _, _ = split_eval_storage(result)
    assert "handoff_bucket" not in rubric
    assert "handoff_sub_bucket" not in rubric
    assert rubric["resolution"] == "yes"


def test_merge_eval_detail_json_reconstructs_api_shape():
    rubric = {"resolution": "yes", "letter_grade": "B"}
    chat = {"messages": []}
    gt = {"preflight": {}}
    ev = merge_eval_detail_json(rubric, chat_json=chat, ground_truth_json=gt)
    assert ev["resolution"] == "yes"
    assert ev["chat"] == chat
    assert ev["eval_ground_truth"] == gt


def test_build_persist_row_fields_includes_resolutions_and_issues():
    ev = {
        "resolution": "no",
        "letter_grade": "C",
        "violations": [{"rule": "no_pii", "turns": [1]}],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "letter_grade": "D",
                "composite_score": 40,
                "resolution": "no",
                "turn_indexes": [1],
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "letter_grade": "A",
                "composite_score": 90,
                "resolution": "yes",
            },
        },
    }
    row = build_persist_row_fields(ev)
    assert row["composite_resolution"] == "no"
    assert row["bot_resolution"] == "no"
    assert row["human_resolution"] == "yes"
    assert "guardrail:no_pii" in row["composite_issues"]
    assert row["bot_issues"]
    assert row["human_issues"] == []
    assert row["handoff_bucket"] is None
    assert row["handoff_sub_bucket"] is None
