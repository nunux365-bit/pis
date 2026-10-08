"""Hand-off classifier tests (isolated from judge)."""

from __future__ import annotations

import pytest

from app.agents.responder_eval.handoff_classifier import (
    _HANDOFF_SCHEMA,
    mock_handoff_classification,
    prepare_chat_for_handoff,
    run_handoff_classifier,
)
from app.agents.responder_eval.handoff_taxonomy import HANDOFF_NOT_APPLICABLE
from app.agents.responder_eval.judge import JUDGE_SCHEMA, mock_judge_result
from app.agents.responder_eval.pipeline import _finalize_graded_cpu
from app.agents.responder_eval.scoring import apply_scores


def test_judge_schema_has_no_handoff_fields():
    assert "handoff_bucket" not in JUDGE_SCHEMA["properties"]
    assert "handoff_sub_bucket" not in JUDGE_SCHEMA["properties"]


def test_classifier_schema_excludes_not_applicable():
    assert HANDOFF_NOT_APPLICABLE not in _HANDOFF_SCHEMA["properties"]["handoff_bucket"]["enum"]
    assert HANDOFF_NOT_APPLICABLE not in _HANDOFF_SCHEMA["properties"]["handoff_sub_bucket"]["enum"]


def test_handoff_does_not_change_grading_scores():
    chat = {
        "messages": [
            {"role": "user", "content": "where is my order"},
            {"role": "bot", "content": "checking status"},
            {"role": "agent", "content": "I will check with courier"},
        ]
    }
    artifact = {
        "preflight": {"order_status": "In transit"},
        "perfect_order": {"overall_pass": False},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    det = {"guardrail_violations": 0, "hallucination_flagged": False, "traceability_pass": True}
    llm = mock_judge_result(chat, artifact, det)
    scored = apply_scores(det, llm, chat)
    handoff = {
        "handoff_bucket": "delivery_disputes",
        "handoff_sub_bucket": "delivery_disputes.tracking_inquiry",
    }
    with_handoff = {**scored, **handoff}
    for key in (
        "composite_score",
        "letter_grade",
        "policy_score",
        "resolution",
        "graded",
        "segment_evals",
    ):
        assert scored[key] == with_handoff[key]


@pytest.mark.asyncio
async def test_finalize_graded_cpu_grade_unchanged_by_handoff(monkeypatch):
    from app.agents.responder_eval import pipeline

    chat = {
        "messages": [
            {"role": "user", "content": "refund"},
            {"role": "agent", "content": "checking"},
        ]
    }
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "perfect_order": {"overall_pass": False},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    det = {"guardrail_violations": 0, "hallucination_flagged": False, "traceability_pass": True}
    llm = mock_judge_result(chat, artifact, det)
    dump = type("Dump", (), {"chat_id": "c1", "order_id": "PO1", "run_id": "r1"})()
    pii = type("Pii", (), {"blocked": False})()
    monkeypatch.setattr(pipeline, "resolve_hallucination_gate", lambda *a, **k: (None, []))
    monkeypatch.setattr(
        pipeline,
        "apply_hard_gates",
        lambda result, **kwargs: result,
    )
    base = _finalize_graded_cpu(dump, chat, artifact, det, llm, pii, handoff=None)
    with_h = _finalize_graded_cpu(
        dump,
        chat,
        artifact,
        det,
        llm,
        pii,
        handoff={"handoff_bucket": "refund_return", "handoff_sub_bucket": "refund_return.new_request"},
    )
    assert base["letter_grade"] == with_h["letter_grade"]
    assert base["composite_score"] == with_h["composite_score"]
    assert with_h["handoff_bucket"] == "refund_return"
    assert "handoff_bucket" not in base or base.get("handoff_bucket") is None


def test_prepare_chat_for_handoff_truncates_before_human():
    chat = {
        "messages": [
            {"role": "user", "content": "where is order"},
            {"role": "bot", "content": "checking"},
            {"role": "agent", "content": "I will help"},
        ]
    }
    window = prepare_chat_for_handoff(chat)
    assert window is not None
    assert len(window["messages"]) == 2
    assert all(m.get("speaker") for m in window["messages"])


def test_mock_handoff_classification_without_human():
    out = mock_handoff_classification({"messages": [{"role": "bot", "content": "hi"}]})
    assert out["handoff_bucket"] is None
    assert out["handoff_sub_bucket"] is None


def test_mock_handoff_classification_with_human():
    out = mock_handoff_classification(
        {"messages": [{"role": "user", "content": "hi"}, {"role": "agent", "content": "help"}]}
    )
    assert out["handoff_bucket"] == "user_self_service"
    assert out["handoff_sub_bucket"] == "user_self_service.tracking_status_check"


async def test_run_handoff_classifier_mock_mode():
    from app.config.settings import settings

    settings.responder_eval_mock_judge = True
    out = await run_handoff_classifier(
        {"messages": [{"role": "user", "content": "refund"}, {"role": "agent", "content": "ok"}]}
    )
    assert out["handoff_bucket"] == "user_self_service"
