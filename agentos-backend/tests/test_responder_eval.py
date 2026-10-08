"""Responder eval unit and integration tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.order_rca.constants import ORDER_RCA_INTERNAL_USER_ID
from app.agents.responder_eval.chat_source import ChatFetchResult
from app.agents.responder_eval.constants import (
    CHAT_ROLE_AGENT,
    CHAT_ROLE_BOT,
    CHAT_ROLE_USER,
    EVAL_STATUS_PENDING,
    LETTER_GRADE_NOT_GRADED,
    RUN_STATUS_NOT_GRADED,
    chat_speaker_for_judge,
)
from app.agents.responder_eval.deterministic import (
    guardrail_checks,
    hallucination_precheck,
    run_deterministic,
    structural_signals,
    traceability_check,
)
from app.agents.responder_eval.ground_truth import build_eval_artifact
from app.agents.responder_eval.hard_gates import (
    apply_hard_gates,
    classify_hallucination,
    classify_hallucination_severity,
    resolve_hallucination_gate,
)
from app.agents.responder_eval.judge import prepare_chat_for_judge, run_judge, _infer_lifecycle_stage
from app.agents.responder_eval.models import OrderRcaEvalDump
from app.agents.responder_eval.pipeline import run_eval_for_chat
from app.agents.responder_eval.pii_verify import verify_no_pii_leak
from app.agents.responder_eval.redact import redact_run_document, redact_string
from app.agents.responder_eval.scoring import apply_scores, composite_score, letter_grade, merge_traceability, _sentiment_score_from_llm
from app.config.settings import settings

FIXTURE_CHAT_DIR = Path(__file__).resolve().parent / "fixtures" / "responder_eval"
CHILD = "PO13326295207344"


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


@pytest.fixture
def sample_chat():
    return json.loads((FIXTURE_CHAT_DIR / "chat-fixture-001.json").read_text(encoding="utf-8"))


def test_redact_string_strips_email_phone():
    assert "[email]" in (redact_string("reach me at a@b.com") or "")
    assert "[phone]" in (redact_string("call 9876543210") or "")
    assert "[phone]" in (redact_string("call 09876543210") or "")


def test_composite_score_uses_partial_traceability():
    score = composite_score({
        "policy_score": 90,
        "resolution": "yes",
        "guardrail_violations": 0,
        "hallucination_flagged": False,
        "traceability_score": 50,
        "tonality_score": 80,
        "sentiment_score": 90,
    })
    score_full = composite_score({
        "policy_score": 90,
        "resolution": "yes",
        "guardrail_violations": 0,
        "hallucination_flagged": False,
        "traceability_score": 100,
        "tonality_score": 80,
        "sentiment_score": 90,
    })
    assert score < score_full


def test_merge_traceability_hybrid_uses_minimum():
    det = {"traceability_score": 100, "traceability_mode": "ground_truth"}
    llm = {"traceability_score": 40}
    out = merge_traceability(det, llm)
    assert out["traceability_score"] == 40
    assert out["traceability_mode"] == "hybrid"
    assert out["traceability_pass"] is False


def test_apply_scores_includes_hybrid_traceability():
    det = {
        "guardrail_violations": 0,
        "traceability_pass": True,
        "traceability_score": 100,
        "traceability_mode": "ground_truth",
        "hallucination_flagged": False,
    }
    llm = {
        "policy_score": 88,
        "resolution": "partial",
        "hallucination_flagged": False,
        "traceability_score": 90,
        "untraceable_turns": [],
        "ground_truth_citations": [],
        "tonality_score": 75,
        "sentiment_score": 85,
    }
    out = apply_scores(det, llm, {"messages": []})
    assert out["traceability_mode"] == "hybrid"
    assert out["llm_traceability_score"] == 90


@pytest.mark.asyncio
async def test_tick_marks_waiting_chat_for_open_conversation(monkeypatch):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.chat_source import ChatFetchResult
    from app.agents.responder_eval.constants import EVAL_STATUS_WAITING
    from app.agents.responder_eval.tick import DumpWorkItem

    mark_mock = AsyncMock()

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="open-chat",
                    order_id="PO1",
                    run_id="r1",
                    eval_artifact={},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(return_value=ChatFetchResult(not_closed_ids=frozenset({"open-chat"}))),
    )
    monkeypatch.setattr(tick, "mark_dump_eval_status", mark_mock)
    monkeypatch.setattr(tick, "fail_eval", AsyncMock())
    monkeypatch.setattr(tick, "abandon_eval", AsyncMock())

    out = await tick.run_responder_eval_batch()
    assert out["skipped_not_closed"] == 1
    mark_mock.assert_awaited_once()
    args, kwargs = mark_mock.await_args
    assert args[0] == "open-chat"
    assert args[1] == EVAL_STATUS_WAITING
    assert kwargs["last_error"] == "chat_not_closed"
    assert kwargs["expected_run_id"] == "r1"
    assert kwargs["from_status"] == "processing"
    assert kwargs["db"] is not None


def test_composite_score_and_grade():
    score = composite_score({
        "policy_score": 90,
        "resolution": "yes",
        "guardrail_violations": 0,
        "hallucination_flagged": False,
        "traceability_pass": True,
        "tonality_score": 80,
        "sentiment_score": 90,
    })
    assert score >= 85
    assert letter_grade(score) in ("A", "B")


def test_guardrail_no_solution_before_concern():
    chat = {
        "messages": [
            {"role": "assistant", "content": "I have initiated your refund."},
            {"role": "user", "content": "My order is late"},
        ]
    }
    out = guardrail_checks(chat)
    assert out["guardrail_violations"] >= 1


def test_guardrail_explain_absence_dont_repeat():
    chat = {
        "messages": [
            {"role": "user", "content": "Where is my refund?"},
            {"role": "assistant", "content": "We don't have refund information yet."},
            {"role": "user", "content": "Please check again"},
            {"role": "assistant", "content": "We don't have refund information yet."},
        ]
    }
    out = guardrail_checks(chat)
    rules = {v["rule"] for v in out["violations"]}
    assert "explain_absence_dont_repeat" in rules


def test_guardrail_order_selection_before_action():
    chat = {
        "messages": [
            {"role": "assistant", "content": "I will cancel your order now."},
            {"role": "user", "content": "ok", "metadata": {"key": "select_order_123"}},
        ]
    }
    out = guardrail_checks(chat)
    rules = {v["rule"] for v in out["violations"]}
    assert "order_selection_before_action" in rules


def test_pii_verify_flags_residual_email():
    out = verify_no_pii_leak({"messages": [{"role": "user", "content": "email me at leak@example.com"}]}, {})
    assert out["pii_incident"] is True
    assert "email" in out["pii_incident_types"]


def test_pii_verify_passes_redacted_chat():
    chat = {"messages": [{"role": "user", "content": "reach [email]"}]}
    out = verify_no_pii_leak(chat, {})
    assert out["pii_incident"] is False


def test_sentiment_default_100_when_missing():
    assert _sentiment_score_from_llm({}) == 100


def test_infer_lifecycle_stage_refund():
    assert _infer_lifecycle_stage({"preflight": {"order_status": "Delivered"}, "order_ops": {"payment_summary": {"total_refund_due": 50}}}) == "refund"


def test_classify_hallucination_severity_p0():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {"messages": [{"role": "assistant", "content": "Your order is still shipped and in transit."}]}
    hal = hallucination_precheck(chat, artifact)
    assert classify_hallucination_severity(chat, artifact, hal) == "P0"
    _sev, modes = classify_hallucination(chat, artifact, hal)
    assert "false_shipment" in modes


def test_classify_hallucination_modes_refund_processed():
    artifact = {
        "preflight": {"order_status": "Cancelled"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your refund has been processed successfully."}
        ]
    }
    # Mark turns so classifier inspects the agent message.
    hal = {
        "hallucination_flagged": True,
        "hallucination_turns": [0],
        "hallucination_facts_missed": 1,
    }
    sev, modes = classify_hallucination(chat, artifact, hal)
    assert sev == "P0"
    assert "false_refund_processed" in modes


def test_hallucination_precheck_ignores_menu_refund_wording():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0.0, "total_refunded_amount": 0}},
    }
    chat = {
        "messages": [
            {
                "role": "bot",
                "content": (
                    "I can see order PO22726604116116 is Delivered — could you please tell me "
                    "what you want to do (e.g., return/refund, view invoice, or something else)?"
                ),
            }
        ]
    }
    out = hallucination_precheck(chat, artifact)
    assert out["hallucination_flagged"] is False
    assert out["hallucination_turns"] == []


def test_resolve_hallucination_gate_llm_refund_status_p0():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [{"role": "bot", "content": "We have issued your full refund already."}]
    }
    scored = {
        "hallucination_flagged": True,
        "hallucination_turns": [0],
        "refund_fact_check": {
            "claim_kind": "status",
            "severity": "P0",
            "turns": [0],
            "failure_mode": "false_refund",
        },
    }
    sev, modes = resolve_hallucination_gate(chat, artifact, scored)
    assert sev == "P0"
    assert "false_refund" in modes


def test_resolve_hallucination_gate_menu_refund_not_p0():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [
            {
                "role": "bot",
                "content": "What would you like (e.g., return/refund or invoice)?",
            }
        ]
    }
    scored = {
        "hallucination_flagged": False,
        "hallucination_turns": [],
        "refund_fact_check": {
            "claim_kind": "option",
            "severity": "none",
            "turns": [],
            "failure_mode": "none",
        },
    }
    sev, modes = resolve_hallucination_gate(chat, artifact, scored)
    assert sev is None
    assert modes == []


def test_resolve_hallucination_gate_llm_refund_without_turns_no_gate():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {"messages": [{"role": "bot", "content": "Refund issued."}]}
    scored = {
        "hallucination_flagged": False,
        "hallucination_turns": [],
        "refund_fact_check": {
            "claim_kind": "status",
            "severity": "P0",
            "turns": [],
            "failure_mode": "false_refund",
        },
    }
    sev, modes = resolve_hallucination_gate(chat, artifact, scored)
    assert sev is None
    assert modes == []


def test_resolve_hallucination_gate_drops_spurious_llm_flagged_when_llm_refund():
    artifact = {
        "preflight": {"order_status": "Placed"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {"messages": [{"role": "bot", "content": "We will process your refund today."}]}
    scored = {
        "hallucination_flagged": True,
        "hallucination_turns": [0],
        "refund_fact_check": {
            "claim_kind": "status",
            "severity": "P0",
            "turns": [0],
            "failure_mode": "false_refund",
        },
    }
    sev, modes = resolve_hallucination_gate(chat, artifact, scored)
    assert sev == "P0"
    assert "false_refund" in modes
    assert "llm_flagged_no_claim_keyword" not in modes


@pytest.mark.asyncio
async def test_run_eval_menu_refund_not_hard_gate_e(monkeypatch):
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {
            "payment_summary": {
                "total_refund_due": 0.0,
                "total_refunded_amount": 0,
                "online_refund_initiated": 0,
            }
        },
    }
    chat = {
        "messages": [
            {"role": "user", "content": "help"},
            {
                "role": "bot",
                "content": (
                    "I can see order PO22726604116116 is Delivered — could you please tell me "
                    "what you want to do (e.g., return/refund, view invoice, or something else)?"
                ),
            },
        ]
    }
    dump = type("D", (), {"chat_id": "21799414", "order_id": "PO22726604116116", "run_id": "r1", "eval_artifact": artifact})()
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)
    out = await run_eval_for_chat(dump, chat, chat_redacted=True)
    assert out.get("hard_gate") is not True
    assert out.get("letter_grade") != "E"


def test_refund_processed_grounded_when_refunded_amount_positive():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0, "total_refunded_amount": 50}},
    }
    from app.agents.responder_eval.deterministic import _refund_processed_ungrounded

    assert _refund_processed_ungrounded("Your refund has been processed.", artifact) is False


def test_classify_hallucination_empty_turns_does_not_scan_transcript():
    """Legacy semantics: empty hallucination_turns ⇒ no claim scan ⇒ no severity."""
    artifact = {
        "preflight": {"order_status": "Placed"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your order is shipped and refund processed."}
        ]
    }
    hal = {"hallucination_flagged": True, "hallucination_turns": [], "hallucination_facts_missed": 1}
    sev, modes = classify_hallucination(chat, artifact, hal)
    assert sev is None
    assert modes == []


def test_classify_hallucination_empty_artifact_does_not_invent_hard_gate():
    chat = {"messages": [{"role": "assistant", "content": "Your order shipped and refund processed."}]}
    for artifact in ({}, {"preflight": {}, "order_ops": {}}):
        sev, modes = classify_hallucination(
            chat,
            artifact,
            {"hallucination_flagged": True, "hallucination_turns": [0]},
        )
        assert sev is None
        assert modes == []


def test_classify_hallucination_supported_claims_do_not_force_p1():
    """Flagged turns with only GT-supported claim keywords must not hard-gate."""
    artifact = {
        "preflight": {"order_status": "shipped"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {"messages": [{"role": "assistant", "content": "Your order has been shipped."}]}
    sev, modes = classify_hallucination(
        chat,
        artifact,
        {"hallucination_flagged": True, "hallucination_turns": [0]},
    )
    assert sev is None
    assert modes == []


def test_soft_hallucination_emits_soft_severity_without_modes():
    from app.agents.responder_eval.issue_tags import composite_issues

    ev = {
        "hallucination_flagged": True,
        "hallucination_turns": [0],
        "hallucination_failure_modes": [],
        "hard_gate_reasons": [],
        "resolution": "yes",
        "letter_grade": "C",
    }
    tags = set(composite_issues(ev))
    assert "hallucination" in tags
    assert "hallucination_severity:soft" in tags
    assert "hallucination_severity:P0" not in tags


def test_soft_hallucination_without_turns_skipped_on_composite():
    from app.agents.responder_eval.issue_tags import composite_issues

    ev = {
        "hallucination_flagged": True,
        "hallucination_turns": [],
        "hallucination_failure_modes": [],
        "hard_gate_reasons": [],
        "resolution": "yes",
        "letter_grade": "C",
    }
    tags = set(composite_issues(ev))
    assert "hallucination" not in tags
    assert "hallucination_severity:soft" not in tags


def test_normalize_merges_legacy_hal_and_dedupes_policy_guardrail():
    from app.agents.responder_eval.dashboard import _is_parent_tag, _normalize_stored_tags

    tags = _normalize_stored_tags(
        [
            "hard_gate:hallucination_P0",
            "hallucination",
            "hallucination:false_refund",
            "guardrail:order_selection_before_action",
            "policy:order_selection_before_action",
            "policy:unsupported_refund_claim",
        ]
    )
    parents = {t for t in tags if _is_parent_tag(t)}
    assert parents == {
        "hallucination",
        "guardrail:order_selection_before_action",
        "policy:unsupported_refund",
    }
    assert "policy:order_selection" not in tags
    assert "hallucination_severity:P0" in tags


def test_apply_hard_gate_pii_forces_grade_e():
    scored = {
        "graded": True,
        "composite_score": 92,
        "letter_grade": "A",
        "resolution": "yes",
        "policy_score": 90,
        "guardrail_violations": 0,
        "hallucination_flagged": False,
        "traceability_pass": True,
        "tonality_score": 80,
        "sentiment_score": 90,
    }
    out = apply_hard_gates(
        scored,
        pii_check={"pii_incident": True, "pii_incident_types": ["email"]},
        hallucination_severity=None,
    )
    assert out["letter_grade"] == "E"
    assert out["composite_score"] == 0
    assert out["hard_gate"] is True


def test_apply_hard_gate_hallucination_p0_forces_grade_e():
    scored = {
        "graded": True,
        "composite_score": 85,
        "letter_grade": "B",
        "resolution": "yes",
        "policy_score": 90,
        "guardrail_violations": 0,
        "hallucination_flagged": True,
        "traceability_pass": True,
        "tonality_score": 80,
        "sentiment_score": 90,
    }
    out = apply_hard_gates(
        scored,
        pii_check={"pii_incident": False, "pii_incident_types": []},
        hallucination_severity="P0",
    )
    assert out["letter_grade"] == "E"
    assert "hallucination_P0" in out["hard_gate_reasons"]


def test_apply_hard_gate_zeros_segment_evals():
    scored = {
        "graded": True,
        "composite_score": 85,
        "letter_grade": "B",
        "segment_evals": {
            "bot": {"graded": True, "composite_score": 72, "letter_grade": "C"},
            "human_agent": {"graded": True, "composite_score": 55, "letter_grade": "D"},
        },
    }
    out = apply_hard_gates(
        scored,
        pii_check={"pii_incident": False, "pii_incident_types": []},
        hallucination_severity="P0",
    )
    assert out["letter_grade"] == "E"
    assert out["segment_evals"]["bot"]["composite_score"] == 0
    assert out["segment_evals"]["bot"]["letter_grade"] == "E"
    assert out["segment_evals"]["human_agent"]["letter_grade"] == "E"


def test_apply_hard_gate_pii_blocks_not_graded_persist():
    not_graded = {
        "graded": False,
        "composite_score": None,
        "letter_grade": "N/A",
        "resolution": "not_applicable",
    }
    out = apply_hard_gates(
        not_graded,
        pii_check={"pii_incident": True, "pii_incident_types": ["email"]},
        hallucination_severity=None,
    )
    assert out["hard_gate"] is True
    assert out["persist_blocked"] is True
    assert out["letter_grade"] == "N/A"


@pytest.mark.asyncio
async def test_build_eval_artifact_from_fixture_run(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    from app.agents.order_rca import graph, run_store

    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)
    doc = await run_store.create_run(CHILD, user_id=ORDER_RCA_INTERNAL_USER_ID)
    await graph.run_graph(doc["run_id"], CHILD)
    run_doc = json.loads(await fake.get(f"order_rca:run:{doc['run_id']}"))
    artifact = build_eval_artifact(run_doc)
    assert artifact["order_id"] == CHILD
    assert "perfect_order" in artifact
    assert "sku_price_increases" in artifact
    assert "sku_price_decreases" in artifact
    assert "post_order_sku_removals" in artifact
    assert "p3" not in artifact
    ops = artifact.get("operations") or {}
    full_ops = (run_doc.get("report") or {}).get("facts", {}).get("operations") or {}
    for key in ("segments", "clickpost_events", "return_followed", "return_note"):
        if key in full_ops:
            assert key in ops, f"expected {key} in eval artifact operations"
    assert "status_chronology" in ops or not full_ops.get("status_chronology")
    assert len(json.dumps(artifact)) < len(json.dumps(run_doc)) * 0.2


@pytest.mark.asyncio
async def test_redact_run_document_removes_email():
    doc = {"report": {"order_details": {"email": "user@test.com", "order_id": "PO1"}}}
    out = redact_run_document(doc)
    assert out["report"]["order_details"]["email"] == "[email]"


@pytest.mark.asyncio
async def test_maybe_persist_rca_eval_dump_upserts(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    captured: dict = {}

    class _Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def execute(self, stmt):
            captured["stmt"] = stmt

        def begin(self):
            return _Begin()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.agents.responder_eval.rca_dump.AsyncSessionLocal", lambda: _Session())
    from app.agents.responder_eval.rca_dump import maybe_persist_rca_eval_dump

    await maybe_persist_rca_eval_dump({
        "chat_id": "chat-1",
        "run_id": "r1",
        "order_id": CHILD,
        "status": "completed",
        "report": {"facts": {"preflight": {"order_status": "Delivered"}}},
    })
    assert captured.get("stmt") is not None


@pytest.mark.asyncio
async def test_maybe_persist_skips_full_run_redaction(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_presidio_enabled", False)
    captured: dict = {}

    class _Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def execute(self, stmt):
            captured["stmt"] = stmt

        def begin(self):
            return _Begin()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.agents.responder_eval.rca_dump.AsyncSessionLocal", lambda: _Session())
    build = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("api persist must not build artifact"))
    monkeypatch.setattr("app.agents.responder_eval.rca_dump.build_eval_artifact", build)
    run_redact = AsyncMock(side_effect=AssertionError("must not redact on api persist"))
    monkeypatch.setattr("app.agents.responder_eval.rca_dump.redact_dict_async", run_redact)
    from app.agents.responder_eval.rca_dump import maybe_persist_rca_eval_dump

    await maybe_persist_rca_eval_dump({
        "chat_id": "chat-1",
        "run_id": "r1",
        "order_id": CHILD,
        "status": "completed",
        "report": {
            "facts": {"preflight": {"order_status": "Delivered", "email": "user@test.com"}},
            "order_details": {"email": "user@test.com"},
        },
    })
    run_redact.assert_not_called()
    assert captured.get("stmt") is not None


@pytest.mark.asyncio
async def test_eval_artifact_for_dump_builds_from_staging_response(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_presidio_enabled", False)
    from app.agents.responder_eval.models import OrderRcaEvalDump
    from app.agents.responder_eval.rca_dump import eval_artifact_for_dump

    dump = OrderRcaEvalDump(
        chat_id="c1",
        response={
            "run_id": "r1",
            "order_id": CHILD,
            "status": "completed",
            "report": {
                "facts": {"preflight": {"order_status": "Delivered"}},
                "payment_details": {"data": [{"payment_id": 1}]},
            },
        },
        eval_artifact={},
        order_id=CHILD,
        run_id="r1",
        eval_status=EVAL_STATUS_PENDING,
    )
    artifact = await eval_artifact_for_dump(dump)
    assert artifact["preflight"]["order_status"] == "Delivered"
    assert "payment_details" not in artifact


@pytest.mark.asyncio
async def test_eval_artifact_for_dump_retry_uses_stored_when_response_cleared():
    from app.agents.responder_eval.models import OrderRcaEvalDump
    from app.agents.responder_eval.rca_dump import eval_artifact_for_dump

    dump = OrderRcaEvalDump(
        chat_id="c1",
        response={},
        eval_artifact={"preflight": {"order_status": "Cancelled"}},
        order_id=CHILD,
        run_id="r1",
        eval_status=EVAL_STATUS_PENDING,
    )
    artifact = await eval_artifact_for_dump(dump)
    assert artifact["preflight"]["order_status"] == "Cancelled"


@pytest.mark.asyncio
async def test_maybe_persist_skips_without_chat_id(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    called = False

    class _Session:
        async def execute(self, stmt):
            nonlocal called
            called = True

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.agents.responder_eval.rca_dump.AsyncSessionLocal", lambda: _Session())
    from app.agents.responder_eval.rca_dump import maybe_persist_rca_eval_dump

    await maybe_persist_rca_eval_dump({"status": "completed", "order_id": CHILD})
    assert called is False


@pytest.mark.asyncio
async def test_pipeline_mock_judge(sample_chat):
    dump = OrderRcaEvalDump(
        chat_id="chat-fixture-001",
        response={},
        eval_artifact={
            "preflight": {"order_status": "Delivered"},
            "perfect_order": {"overall_pass": True},
            "order_ops": {"payment_summary": {"total_refund_due": 0}},
        },
        order_id=CHILD,
        run_id="run-1",
        eval_status=EVAL_STATUS_PENDING,
    )
    settings.responder_eval_mock_judge = True
    result = await run_eval_for_chat(dump, sample_chat)
    assert "composite_score" in result
    assert result["letter_grade"] in ("A", "B", "C", "D", "E")
    assert result["chat_id"] == "chat-fixture-001"
    assert "segment_evals" in result
    assert "bot" in result["segment_evals"]


@pytest.mark.asyncio
async def test_pipeline_segment_evals_for_bot_and_human():
    dump = OrderRcaEvalDump(
        chat_id="chat-segment-1",
        response={},
        eval_artifact={
            "preflight": {"order_status": "Delivered"},
            "perfect_order": {"overall_pass": False},
            "order_ops": {"payment_summary": {"total_refund_due": 0}},
        },
        order_id=CHILD,
        run_id="run-seg",
        eval_status=EVAL_STATUS_PENDING,
    )
    chat = {
        "messages": [
            {"role": "bot", "content": "How can I help?"},
            {"role": "user", "content": "Missing package"},
            {"role": "agent", "content": "I will check with the courier"},
        ]
    }
    settings.responder_eval_mock_judge = True
    result = await run_eval_for_chat(dump, chat)
    segs = result["segment_evals"]
    assert "bot" in segs
    assert "human_agent" in segs
    assert segs["bot"]["graded"] is True
    assert segs["human_agent"]["graded"] is True
    assert result["graded"] is True
    assert result["handoff_bucket"] == "user_self_service"
    assert result["handoff_sub_bucket"] == "user_self_service.tracking_status_check"
    from app.agents.responder_eval.denormalized import build_persist_row_fields

    row = build_persist_row_fields(result)
    assert row["handoff_bucket"] == "user_self_service"
    assert row["human_score"] is not None


@pytest.mark.asyncio
async def test_fetch_chats_from_fixtures(sample_chat, monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", True)
    monkeypatch.setattr(settings, "responder_eval_chat_fixture_dir", str(FIXTURE_CHAT_DIR))
    from app.agents.responder_eval.chat_source import fetch_chats

    result = fetch_chats(["chat-fixture-001", "missing"])
    assert "chat-fixture-001" in result.chats
    assert result.chats["chat-fixture-001"]["messages"]


@pytest.mark.asyncio
async def test_graph_persists_eval_dump_with_chat_id(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    persist = AsyncMock()
    monkeypatch.setattr("app.agents.responder_eval.rca_dump.maybe_persist_rca_eval_dump", persist)
    from app.agents.order_rca import graph, run_store

    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)
    doc = await run_store.create_run(CHILD, user_id=ORDER_RCA_INTERNAL_USER_ID, chat_id="chat-abc")
    await graph.run_graph(doc["run_id"], CHILD)
    assert persist.await_count == 1
    args = persist.await_args.args[0]
    assert args.get("chat_id") == "chat-abc"
    assert args.get("status") == "completed"


@pytest.mark.asyncio
async def test_run_store_chat_id_roundtrip():
    fake = _FakeRedis()
    with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
        from app.agents.order_rca import run_store

        doc = await run_store.create_run("PO1234", user_id="u1", chat_id="chat-xyz")
        loaded = await run_store.get_run(doc["run_id"])
    assert loaded is not None
    assert loaded.get("chat_id") == "chat-xyz"


def test_hallucination_precheck_flags_wrong_status():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your order is still shipped and in transit."},
        ]
    }
    out = hallucination_precheck(chat, artifact)
    assert out["hallucination_flagged"] is True
    assert out["hallucination_facts_missed"] >= 1


def test_hallucination_negation_ignores_not_delivered():
    artifact = {
        "preflight": {"order_status": "In transit"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your order is not delivered yet."},
        ]
    }
    out = hallucination_precheck(chat, artifact)
    assert out["hallucination_flagged"] is False
    assert out["hallucination_facts_missed"] == 0


def test_merge_hallucination_counts_facts():
    from app.agents.responder_eval.scoring import _merge_hallucination

    out = _merge_hallucination(
        {"hallucination_flagged": True, "hallucination_turns": [1], "hallucination_facts_missed": 2},
        {"hallucination_flagged": False, "hallucination_turns": [2]},
    )
    assert out["hallucination_flagged"] is True
    assert out["hallucination_facts_missed"] == 3
    assert out["hallucination_turns"] == [1, 2]


def test_merge_hallucination_empty_turns_not_flagged():
    from app.agents.responder_eval.scoring import _merge_hallucination

    out = _merge_hallucination(
        {"hallucination_flagged": True, "hallucination_turns": [], "hallucination_facts_missed": 1},
        {"hallucination_flagged": True, "hallucination_turns": []},
    )
    assert out["hallucination_flagged"] is False
    assert out["hallucination_turns"] == []
    assert out["hallucination_facts_missed"] == 0


def test_policy_score_uses_judge_value_without_double_penalty():
    from app.agents.responder_eval.scoring import _policy_score_from_llm

    score = _policy_score_from_llm(
        {"policy_score": 90, "policy_violations": [{"rule": "x", "turns": [0]}, {"rule": "y", "turns": [1]}]}
    )
    assert score == 90


@pytest.mark.asyncio
async def test_apply_scores_merges_det_and_llm():
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 88,
        "resolution": "partial",
        "hallucination_flagged": False,
        "tonality_score": 75,
        "sentiment_score": 85,
    }
    out = apply_scores(det, llm, {"messages": []})
    assert out["letter_grade"] in ("A", "B", "C", "D", "E")


def test_scoring_grade_e_for_zero():
    assert letter_grade(0) == "E"


def test_apply_scores_not_applicable_is_not_graded():
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 90,
        "resolution": "not_applicable",
        "hallucination_flagged": False,
        "tonality_score": 80,
        "sentiment_score": 90,
    }
    out = apply_scores(det, llm, {"messages": []})
    assert out["graded"] is False
    assert out["letter_grade"] == LETTER_GRADE_NOT_GRADED
    assert out["composite_score"] is None


def test_apply_scores_merges_hallucination_from_deterministic():
    det = {
        "guardrail_violations": 0,
        "traceability_pass": True,
        "hallucination_flagged": True,
        "hallucination_turns": [0],
    }
    llm = {
        "policy_score": 88,
        "resolution": "partial",
        "hallucination_flagged": False,
        "tonality_score": 75,
        "sentiment_score": 85,
    }
    out = apply_scores(det, llm, {"messages": [{"role": "assistant", "content": "hi"}]})
    assert out["hallucination_flagged"] is True


def test_apply_scores_ignores_flag_only_hallucination_without_turns():
    det = {
        "guardrail_violations": 0,
        "traceability_pass": True,
        "hallucination_flagged": True,
        "hallucination_turns": [],
    }
    llm = {
        "policy_score": 88,
        "resolution": "partial",
        "hallucination_flagged": True,
        "hallucination_turns": [],
        "tonality_score": 75,
        "sentiment_score": 85,
    }
    out = apply_scores(det, llm, {"messages": []})
    assert out["hallucination_flagged"] is False


def test_traceability_ground_truth_passes_when_claim_matches_rca():
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your order has been delivered."},
        ]
    }
    artifact = {"preflight": {"order_status": "Delivered"}}
    out = traceability_check(chat, artifact)
    assert out["traceability_mode"] == "ground_truth"
    assert out["traceability_pass"] is True
    assert out["traceability_score"] == 100


def test_traceability_ground_truth_fails_when_claim_ungrounded():
    chat = {
        "messages": [
            {"role": "assistant", "content": "Your refund has been processed."},
        ]
    }
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    out = traceability_check(chat, artifact)
    assert out["traceability_mode"] == "ground_truth"
    assert out["traceability_pass"] is False
    assert out["traceability_score"] == 0
    assert out["untraceable_turns"] == [0]


def test_traceability_skipped_without_ground_truth():
    chat = {
        "messages": [
            {"role": "assistant", "content": "How can I help you today?"},
        ]
    }
    out = traceability_check(chat, {})
    assert out["traceability_pass"] is True
    assert out.get("traceability_skipped") is True


def test_apply_scores_skipped_traceability_not_overwritten_by_llm():
    det = {
        "traceability_skipped": True,
        "traceability_pass": True,
        "guardrail_violations": 0,
        "hallucination_flagged": False,
    }
    llm = {
        "resolution": "yes",
        "policy_score": 90,
        "traceability_score": 50,
        "tonality_score": 80,
        "sentiment_score": 90,
    }
    out = apply_scores(det, llm, {"messages": [{"role": "bot", "content": "hi"}]})
    assert out.get("traceability_skipped") is True
    assert out.get("traceability_score") is None


def test_metadata_traceability_checks_agent_side_turns():
    chat = {
        "messages": [
            {
                "role": "agent",
                "content": "Your refund policy allows a return",
                "metadata": {"policy_version_id": "v1"},
            },
            {"role": "user", "content": "What is the refund policy?"},
        ]
    }
    out = traceability_check(chat, {})
    assert out["traceability_mode"] == "metadata"
    assert out["traceability_pass"] is True


def test_structural_signals_separates_bot_and_human_talk():
    long_human = " ".join(["word"] * 200)
    chat = {
        "messages": [
            {"role": CHAT_ROLE_USER, "content": "Help"},
            {"role": CHAT_ROLE_BOT, "content": "Hi there"},
            {"role": CHAT_ROLE_AGENT, "content": long_human},
        ]
    }
    out = structural_signals(chat)
    assert out["bot_turns"] == 1
    assert out["human_agent_turns"] == 1
    assert "imbalanced_talk_ratio" not in out["flags"]
    assert "long_bot_monologue" not in out["flags"]


def test_guardrail_checks_apply_to_human_agent():
    chat = {
        "messages": [
            {"role": CHAT_ROLE_USER, "content": "My order is wrong"},
            {"role": CHAT_ROLE_AGENT, "content": "I will process your refund today"},
        ]
    }
    out = guardrail_checks(chat)
    rules = {v["rule"] for v in out["violations"]}
    assert "order_selection_before_action" in rules


def test_chat_speaker_for_judge_maps_roles():
    assert chat_speaker_for_judge(CHAT_ROLE_USER) == "customer"
    assert chat_speaker_for_judge(CHAT_ROLE_BOT) == "bot"
    assert chat_speaker_for_judge(CHAT_ROLE_AGENT) == "human_agent"
    assert chat_speaker_for_judge("assistant") == "bot"


def test_prepare_chat_for_judge_adds_speaker_labels():
    chat = {
        "chat_id": "1",
        "messages": [
            {"role": "user", "content": "I didn't receive my order"},
            {"role": "bot", "content": "Connecting you to an agent"},
            {"role": "agent", "content": "I will help with your delivery issue"},
        ],
    }
    prepared = prepare_chat_for_judge(chat)
    speakers = [m["speaker"] for m in prepared["messages"]]
    assert speakers == ["customer", "bot", "human_agent"]
    assert "role_legend" in prepared


def test_prepare_chat_for_judge_strips_seed_handoff_metadata():
    prepared = prepare_chat_for_judge(
        {
            "chat_id": "seed-1",
            "metadata": {
                "channel": "app",
                "handoff_bucket": "refund_return",
                "handoff_sub_bucket": "refund_return.new_request",
                "seed_seq": 3,
            },
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert prepared["metadata"] == {"channel": "app"}
    assert prepared["messages"][0]["speaker"] == "customer"


@pytest.mark.asyncio
async def test_run_judge_sends_speaker_labels_to_llm(monkeypatch):
    captured: dict[str, str] = {}

    class _Message:
        content = "{}"

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs):
            captured["user"] = kwargs["messages"][1]["content"]
            return _Response()

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(settings, "responder_eval_mock_judge", False)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.get_shared_openai_client",
        lambda: _Client(),
    )

    chat = {
        "messages": [
            {"role": "user", "content": "help"},
            {"role": "agent", "content": "Sure"},
        ]
    }
    await run_judge(chat, {}, {})
    payload = json.loads(captured["user"])
    assert payload["chat"]["messages"][0]["speaker"] == "customer"
    assert payload["chat"]["messages"][1]["speaker"] == "human_agent"
    assert payload["chat"]["role_legend"]["human_agent"].startswith("Live human")


def test_parse_judge_response_rejects_truncated():
    from app.agents.responder_eval.judge import _parse_judge_response

    with pytest.raises(ValueError, match="truncated"):
        _parse_judge_response('{"policy_score": 80', finish_reason="length")


def test_parse_judge_response_accepts_valid_json_despite_length_finish_reason():
    from app.agents.responder_eval.judge import _parse_judge_response

    out = _parse_judge_response('{"policy_score": 80}', finish_reason="length")
    assert out["policy_score"] == 80


@pytest.mark.asyncio
async def test_run_judge_retries_on_invalid_json(monkeypatch):
    from app.agents.responder_eval.judge import run_judge

    calls: list[dict[str, Any]] = []

    class _Message:
        def __init__(self, content: str):
            self.content = content

    class _Choice:
        def __init__(self, content: str, finish_reason: str):
            self.message = _Message(content)
            self.finish_reason = finish_reason

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return type("Resp", (), {"choices": [_Choice('{"policy_score": 80', "length")]})()
            return type(
                "Resp",
                (),
                {"choices": [_Choice('{"policy_score": 80, "policy_violations": []}', "stop")]},
            )()

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(settings, "responder_eval_mock_judge", False)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.get_shared_openai_client",
        lambda: _Client(),
    )

    out = await run_judge({"messages": [{"role": "assistant", "content": "hi"}]}, {}, {})
    assert out["policy_score"] == 80
    assert len(calls) == 2
    assert calls[0]["max_completion_tokens"] == calls[1]["max_completion_tokens"]
    assert "Be very concise" in calls[0]["messages"][0]["content"]
    assert "previous output was too long" in calls[1]["messages"][0]["content"].lower()


@pytest.mark.asyncio
async def test_run_judge_raises_after_retry_exhausted(monkeypatch):
    from app.agents.responder_eval.judge import JudgeError, run_judge

    class _Message:
        content = '{"policy_score": 80'

    class _Choice:
        message = _Message()
        finish_reason = "length"

    class _Completions:
        async def create(self, **_kwargs):
            return type("Resp", (), {"choices": [_Choice()]})()

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(settings, "responder_eval_mock_judge", False)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.get_shared_openai_client",
        lambda: _Client(),
    )

    with pytest.raises(JudgeError, match="truncated"):
        await run_judge({"messages": [{"role": "assistant", "content": "hi"}]}, {}, {})


def test_hallucination_precheck_handles_bad_refund_due():
    artifact = {
        "preflight": {"order_status": "Delivered"},
        "order_ops": {"payment_summary": {"total_refund_due": "not-a-number"}},
    }
    chat = {"messages": [{"role": "assistant", "content": "Refund processed for you."}]}
    out = hallucination_precheck(chat, artifact)
    assert "hallucination_flagged" in out


@pytest.mark.asyncio
async def test_pipeline_not_graded_without_agent_turns():
    dump = OrderRcaEvalDump(
        chat_id="chat-no-bot",
        response={},
        eval_artifact={"preflight": {"order_status": "Delivered"}},
        order_id=CHILD,
        run_id="run-1",
        eval_status=EVAL_STATUS_PENDING,
    )
    chat = {"messages": [{"role": "user", "content": "hello"}]}
    settings.responder_eval_mock_judge = True
    result = await run_eval_for_chat(dump, chat)
    assert result["graded"] is False
    assert result["letter_grade"] == LETTER_GRADE_NOT_GRADED
    assert result["not_graded_reason"] == "no_agent_turns"


@pytest.mark.asyncio
async def test_already_evaluated_requires_matching_run_id(monkeypatch):
    from app.agents.responder_eval import tick

    dump_old = OrderRcaEvalDump(
        chat_id="chat-1",
        response={},
        eval_artifact={},
        order_id=CHILD,
        run_id="run-new",
        eval_status=EVAL_STATUS_PENDING,
    )

    class _Result:
        def all(self):
            return [type("Row", (), {"chat_id": "chat-1", "rca_run_id": "run-old"})()]

    class _Session:
        async def execute(self, _q):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tick, "AsyncSessionLocal", lambda: _Session())
    done = await tick._already_evaluated(_Session(), [dump_old])
    assert done == set()


def test_dashboard_summary_shape():
    from app.agents.responder_eval.dashboard import eval_resolution_distribution, eval_summary

    assert callable(eval_summary)
    assert callable(eval_resolution_distribution)


def test_mock_judge_exported():
    from app.agents.responder_eval.judge import JUDGE_SCHEMA, JUDGE_SYSTEM, mock_judge_result

    out = mock_judge_result({"messages": [{"role": "user", "content": "hi"}]}, {}, {})
    assert "policy_score" in out
    assert "handoff_bucket" not in out
    assert JUDGE_SYSTEM
    assert "handoff_bucket" not in JUDGE_SCHEMA["properties"]


@pytest.mark.asyncio
async def test_pending_dumps_query_builds(monkeypatch):
    from app.agents.responder_eval import tick

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, _q):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tick, "AsyncSessionLocal", lambda: _Session())
    out = await tick._pending_dumps(_Session(), 5)
    assert out == []


@pytest.mark.asyncio
async def test_pending_dumps_prioritizes_pending_and_backs_off_waiting(monkeypatch):
    """waiting_chat must not HOL-block pending/failed (closed chats)."""
    from sqlalchemy.dialects import postgresql

    from app.agents.responder_eval import tick

    captured: list = []

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, q):
            captured.append(q)
            return _Result()

    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 1.0)
    monkeypatch.setattr(settings, "responder_eval_waiting_recheck_minutes", 60)
    monkeypatch.setattr(settings, "responder_eval_processing_stale_minutes", 60)

    await tick._pending_dumps(_Session(), 5)
    assert len(captured) == 1
    sql = str(
        captured[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "case" in sql
    assert "waiting_chat" in sql
    assert "pending" in sql
    assert "failed" in sql
    # Backoff: waiting rows gated on updated_at, not only created_at.
    assert "updated_at" in sql
    # Pending/failed sort ahead of waiting (priority 0 before else 2).
    assert "order by" in sql


def test_claim_eligibility_contract_pending_beats_older_waiting():
    """Pure contract: newer pending ranks before older eligible waiting_chat."""
    from app.agents.responder_eval.constants import (
        EVAL_STATUS_FAILED,
        EVAL_STATUS_PENDING,
        EVAL_STATUS_PROCESSING,
        EVAL_STATUS_WAITING,
    )

    now = datetime.now(UTC)
    min_age_h = 1.0
    waiting_recheck_m = 60
    stale_m = 60
    cutoff = now - timedelta(hours=min_age_h)
    waiting_cutoff = now - timedelta(minutes=waiting_recheck_m)
    stale_cutoff = now - timedelta(minutes=stale_m)

    def eligible(status: str, created_at: datetime, updated_at: datetime) -> bool:
        if status in (EVAL_STATUS_PENDING, EVAL_STATUS_FAILED) and created_at <= cutoff:
            return True
        if (
            status == EVAL_STATUS_WAITING
            and created_at <= cutoff
            and updated_at <= waiting_cutoff
        ):
            return True
        if status == EVAL_STATUS_PROCESSING and updated_at <= stale_cutoff:
            return True
        return False

    def priority(status: str) -> int:
        if status in (EVAL_STATUS_PENDING, EVAL_STATUS_FAILED):
            return 0
        if status == EVAL_STATUS_PROCESSING:
            return 1
        return 2

    rows = [
        ("wait-old", EVAL_STATUS_WAITING, now - timedelta(days=3), now - timedelta(hours=2)),
        ("pend-new", EVAL_STATUS_PENDING, now - timedelta(hours=2), now - timedelta(hours=2)),
        ("wait-fresh", EVAL_STATUS_WAITING, now - timedelta(days=3), now - timedelta(minutes=5)),
        ("fail-mid", EVAL_STATUS_FAILED, now - timedelta(hours=5), now - timedelta(hours=5)),
    ]
    claimed = sorted(
        (r for r in rows if eligible(r[1], r[2], r[3])),
        key=lambda r: (priority(r[1]), r[2], r[0]),
    )
    assert [r[0] for r in claimed] == ["fail-mid", "pend-new", "wait-old"]
    assert "wait-fresh" not in {r[0] for r in claimed}


@pytest.mark.asyncio
async def test_fetch_chats_async_from_fixtures(monkeypatch, sample_chat):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", True)
    monkeypatch.setattr(settings, "responder_eval_chat_fixture_dir", str(FIXTURE_CHAT_DIR))
    from app.agents.responder_eval.chat_source import fetch_chats_async

    result = await fetch_chats_async(["chat-fixture-001"])
    assert "chat-fixture-001" in result.chats


def test_validate_responder_eval_settings_requires_chat_db(monkeypatch):
    from app.agents.responder_eval.startup import validate_responder_eval_settings

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)
    monkeypatch.setattr(settings, "responder_eval_chat_db_url_sync", "")
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    with pytest.raises(RuntimeError, match="CHAT_DB_URL"):
        validate_responder_eval_settings()


@pytest.mark.asyncio
async def test_judge_requires_api_key_when_not_mock(monkeypatch):
    from app.agents.responder_eval.judge import JudgeError, run_judge

    monkeypatch.setattr(settings, "responder_eval_mock_judge", False)
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(JudgeError, match="OPENAI_API_KEY"):
        await run_judge({"messages": [{"role": "assistant", "content": "hi"}]}, {}, {})


@pytest.mark.asyncio
async def test_tick_evaluates_pending_dump(monkeypatch, sample_chat):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.tick import DumpWorkItem

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 0)
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="chat-fixture-001",
                    order_id=CHILD,
                    run_id="run-tick-1",
                    eval_artifact={"preflight": {"order_status": "Delivered"}},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(
            return_value=ChatFetchResult(
                chats={"chat-fixture-001": sample_chat},
            )
        ),
    )
    monkeypatch.setattr(
        tick,
        "_claimed_dump_with_artifact",
        AsyncMock(
            return_value=DumpWorkItem(
                chat_id="chat-fixture-001",
                order_id=CHILD,
                run_id="run-tick-1",
                eval_artifact={"preflight": {"order_status": "Delivered"}},
            )
        ),
    )
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(tick, "persist_eval_run", persist)

    out = await tick.run_responder_eval_batch()
    assert out["evaluated"] == 1
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_batch_marks_processing_before_commit(monkeypatch):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.models import OrderRcaEvalDump

    dump = OrderRcaEvalDump(
        chat_id="chat-claim",
        response={},
        eval_artifact={},
        order_id="PO1",
        run_id="r1",
        eval_status="pending",
    )
    processing_mock = AsyncMock()
    done_mock = AsyncMock()
    executed: list[str] = []

    class _Scalars:
        def all(self):
            return [dump]

    class _Result:
        def __init__(self, value=True):
            self._value = value

        def scalars(self):
            return _Scalars()

        def scalar_one(self):
            return self._value

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Session:
        def begin(self):
            return _Txn()

        async def execute(self, q, *_a, **_k):
            s = str(q)
            executed.append(s)
            return _Result(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tick, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(tick, "_already_evaluated", AsyncMock(return_value=set()))
    monkeypatch.setattr(tick, "_mark_dumps_done_batch", done_mock)
    monkeypatch.setattr(tick, "_mark_dumps_processing_batch", processing_mock)

    work = await tick._load_batch_work_items(5)
    assert len(work) == 1
    assert work[0].chat_id == "chat-claim"
    processing_mock.assert_awaited_once()
    chat_ids = processing_mock.await_args.args[1]
    assert chat_ids == ["chat-claim"]
    done_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_batch_returns_empty_when_no_pending_dumps(monkeypatch):
    from app.agents.responder_eval import tick

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Session:
        def begin(self):
            return _Txn()

        async def execute(self, *_a, **_k):
            class _Scalars:
                def all(self):
                    return []

            class _Result:
                def scalars(self):
                    return _Scalars()

            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tick, "AsyncSessionLocal", lambda: _Session())
    assert await tick._load_batch_work_items(5) == []


@pytest.mark.asyncio
async def test_tick_skips_evaluated_count_when_persist_returns_false(monkeypatch, sample_chat):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.tick import DumpWorkItem

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="chat-fixture-001",
                    order_id=CHILD,
                    run_id="run-tick-1",
                    eval_artifact={"preflight": {"order_status": "Delivered"}},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(return_value=ChatFetchResult(chats={"chat-fixture-001": sample_chat})),
    )
    monkeypatch.setattr(
        tick,
        "_claimed_dump_with_artifact",
        AsyncMock(
            return_value=DumpWorkItem(
                chat_id="chat-fixture-001",
                order_id=CHILD,
                run_id="run-tick-1",
                eval_artifact={"preflight": {"order_status": "Delivered"}},
            )
        ),
    )
    monkeypatch.setattr(tick, "persist_eval_run", AsyncMock(return_value=False))
    reset_mock = AsyncMock()
    monkeypatch.setattr(tick, "_reset_dumps_to_pending_batch", reset_mock)

    out = await tick.run_responder_eval_batch()
    assert out["evaluated"] == 0
    assert out["skipped_superseded"] == 1
    reset_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_raises_in_production_mode(monkeypatch):
    from app.agents.responder_eval.judge import JudgeError, run_judge

    monkeypatch.setattr(settings, "responder_eval_mock_judge", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    class _BrokenClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise RuntimeError("api down")

    monkeypatch.setattr(
        "app.agents.responder_eval.judge.get_shared_openai_client",
        lambda: _BrokenClient(),
    )
    with pytest.raises(JudgeError):
        await run_judge({"messages": []}, {}, {})


@pytest.mark.asyncio
async def test_persist_eval_run_skips_dump_done_on_run_id_mismatch(monkeypatch):
    from app.agents.responder_eval import pipeline
    from app.agents.responder_eval.models import OrderRcaEvalDump

    dump = OrderRcaEvalDump(
        chat_id="chat-1",
        response={},
        eval_artifact={},
        order_id="PO1",
        run_id="new-run",
        eval_status="pending",
    )
    apply_mock = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(pipeline, "get_dump_by_chat_id", AsyncMock(return_value=dump))
    monkeypatch.setattr(pipeline, "mark_dump_eval_succeeded", apply_mock)

    persisted = await pipeline.persist_eval_run(
        {
            "chat_id": "chat-1",
            "rca_run_id": "stale-run",
            "eval_version": "responder_eval_v1.1",
            "composite_score": 80,
            "letter_grade": "B",
            "deterministic": {},
            "llm": {},
            "resolution": "resolved",
        },
        db=session,
    )

    assert persisted is False
    apply_mock.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_eval_run_skips_when_dump_not_processing(monkeypatch):
    from app.agents.responder_eval import pipeline
    from app.agents.responder_eval.models import OrderRcaEvalDump

    dump = OrderRcaEvalDump(
        chat_id="chat-1",
        response={},
        eval_artifact={},
        order_id="PO1",
        run_id="run-1",
        eval_status="done",
    )
    apply_mock = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(pipeline, "get_dump_by_chat_id", AsyncMock(return_value=dump))
    monkeypatch.setattr(pipeline, "mark_dump_eval_succeeded", apply_mock)

    persisted = await pipeline.persist_eval_run(
        {
            "chat_id": "chat-1",
            "rca_run_id": "run-1",
            "eval_version": "responder_eval_v1.1",
            "composite_score": 80,
            "letter_grade": "B",
            "deterministic": {},
            "llm": {},
            "resolution": "resolved",
        },
        db=session,
    )

    assert persisted is False
    apply_mock.assert_not_awaited()
    session.execute.assert_not_awaited()

