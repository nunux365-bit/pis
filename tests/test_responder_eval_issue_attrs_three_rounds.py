"""Three-round review coverage for hierarchical issue attributes (all types)."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.responder_eval.dashboard import (
    _bind_attr_parent,
    _count_issue_tags,
    _is_attr_tag,
    _is_parent_tag,
    _normalize_stored_tags,
)
from app.agents.responder_eval.hard_gates import classify_hallucination
from app.agents.responder_eval.issue_tags import (
    composite_issues,
    enrich_eval_attr_fields,
    ensure_segment_turn_indexes,
    segment_scoped_issues,
)
from app.agents.responder_eval.policy_buckets import normalize_policy_rule
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT
from app.agents.responder_eval.traceability_reasons import compute_traceability_failure_reasons


# ---------------------------------------------------------------------------
# Round 1 — per-type parent/attr contracts
# ---------------------------------------------------------------------------


def test_round1_pii_parent_and_types():
    ev = {
        "hard_gate_reasons": ["pii_incident"],
        "pii_incident_types": ["phone", "email"],
        "letter_grade": "E",
        "resolution": "yes",
    }
    tags = set(composite_issues(ev))
    assert "hard_gate:pii" in tags
    assert "pii:phone" in tags and "pii:email" in tags
    assert "hallucination" not in tags


def test_round1_hallucination_unified_parent_with_modes_and_severity():
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_failure_modes": ["false_refund", "false_shipment"],
        "letter_grade": "E",
        "resolution": "yes",
    }
    tags = set(composite_issues(ev))
    assert "hallucination" in tags
    assert "hard_gate:hallucination" not in tags
    assert "hallucination_severity:P0" in tags
    assert "hallucination:false_refund" in tags
    assert "hallucination:false_shipment" in tags


def test_round1_guardrail_and_policy_and_resolution_and_traceability():
    ev = {
        "violations": [{"rule": "order_selection_before_action", "turns": [1]}],
        "policy_violations": [
            {"rule": "order_selection_before_action", "turns": [1]},
            {"rule": "Unsupported status/timing claim", "turns": [2]},
        ],
        "resolution": "partial",
        "policy_lifecycle_stage": "in_transit",
        "traceability_pass": False,
        "traceability_failure_reasons": ["unsupported_refund", "nuance_or_paraphrase"],
        "letter_grade": "C",
    }
    tags = set(composite_issues(ev))
    assert "guardrail:order_selection_before_action" in tags
    assert "policy:order_selection" not in tags  # deduped
    assert "policy:unsupported_status_or_eta" in tags
    assert "resolution:partial" in tags
    assert "resolution_lifecycle:in_transit" in tags
    assert "traceability_failure" in tags
    assert "traceability:unsupported_refund" in tags


def test_round1_prefix_collision_attrs_never_parents():
    tags = _normalize_stored_tags(
        [
            "hallucination",
            "hallucination:false_refund",
            "hallucination_severity:P0",
            "traceability_failure",
            "traceability:unsupported_refund",
            "resolution:partial",
            "resolution_lifecycle:delivered",
            "hard_gate:pii",
            "pii:phone",
            "guardrail:order_selection_before_action",
            "policy:unsupported_refund",
        ]
    )
    parents = {t for t in tags if _is_parent_tag(t)}
    attrs = {t for t in tags if _is_attr_tag(t)}
    assert "hallucination:false_refund" not in parents
    assert "hallucination:false_refund" in attrs
    assert "traceability:unsupported_refund" in attrs
    assert "resolution_lifecycle:delivered" in attrs
    assert "pii:phone" in attrs
    assert parents == {
        "hallucination",
        "traceability_failure",
        "resolution:partial",
        "hard_gate:pii",
        "guardrail:order_selection_before_action",
        "policy:unsupported_refund",
    }


# ---------------------------------------------------------------------------
# Round 2 — prod-criteria + aggregation invariants
# ---------------------------------------------------------------------------


def test_round2_prod_policy_buckets_from_mine_head():
    assert normalize_policy_rule("Unsupported status/timing claim") == "unsupported_status_or_eta"
    assert normalize_policy_rule("unsupported_refund_timing_claim") == "unsupported_refund"
    assert normalize_policy_rule("unsupported_delivery_eta_claim") == "unsupported_delivery"
    assert normalize_policy_rule("unsupported cancellation claim") == "unsupported_cancellation"
    assert normalize_policy_rule("professional_tone") == "tone"
    # refund wins over cancel when both present
    assert normalize_policy_rule("cancel_and_refund_claim") == "unsupported_refund"


def test_round2_prod_hal_modes_closed_set():
    from app.agents.responder_eval.hard_gates import HALLUCINATION_FAILURE_MODES

    expected = {
        "false_refund",
        "false_shipment",
        "false_delivery",
        "false_cancellation",
        "false_refund_processed",
        "llm_flagged_no_claim_keyword",
    }
    assert HALLUCINATION_FAILURE_MODES == expected


def test_round2_multilabel_attr_sums_can_exceed_parent():
    rows = [
        SimpleNamespace(
            letter_grade="E",
            bot_grade="-",
            human_grade="-",
            bot_score=None,
            human_score=None,
            composite_issues=[
                "hallucination",
                "hallucination:false_refund",
                "hallucination:false_refund_processed",
                "hallucination_severity:P0",
            ],
            bot_issues=[],
            human_issues=[],
        )
    ]
    item = _count_issue_tags(rows, limit=5)["composite"]["E"][0]
    assert item["issue"] == "hallucination"
    assert item["count"] == 1
    by_id = {a["id"]: a["count"] for a in item["attributes"]}
    assert by_id["false_refund"] == 1
    assert by_id["false_refund_processed"] == 1
    assert by_id["severity_P0"] == 1
    assert sum(by_id.values()) > item["count"]


def test_round2_legacy_dual_hal_tokens_count_once():
    rows = [
        SimpleNamespace(
            letter_grade="E",
            bot_grade="-",
            human_grade="-",
            bot_score=None,
            human_score=None,
            composite_issues=["hard_gate:hallucination", "hallucination", "hallucination:false_refund"],
            bot_issues=[],
            human_issues=[],
        )
    ]
    item = _count_issue_tags(rows, limit=5)["composite"]["E"][0]
    assert item["issue"] == "hallucination"
    assert item["count"] == 1


def test_round2_traceability_reasons_safe_on_junk_turns():
    reasons = compute_traceability_failure_reasons(
        {"messages": [{"role": "assistant", "content": "refund done"}]},
        {"preflight": {"order_status": "Placed"}, "order_ops": {"payment_summary": {}}},
        {"traceability_pass": False, "untraceable_turns": ["bad", None, 0]},
    )
    assert reasons
    assert all(isinstance(r, str) for r in reasons)


# ---------------------------------------------------------------------------
# Round 3 — adversarial edges
# ---------------------------------------------------------------------------


def test_round3_soft_hal_without_modes_emits_soft_severity():
    tags = set(
        composite_issues(
            {
                "hallucination_flagged": True,
                "hallucination_turns": [0],
                "hallucination_failure_modes": [],
                "hard_gate_reasons": [],
                "resolution": "yes",
                "letter_grade": "C",
            }
        )
    )
    assert "hallucination" in tags
    assert "hallucination_severity:soft" in tags


def test_round3_empty_turns_classify_and_supported_claims():
    chat = {"messages": [{"role": "assistant", "content": "Your order has shipped and refund processed"}]}
    artifact = {
        "preflight": {"order_status": "shipped"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    sev, modes = classify_hallucination(
        chat, artifact, {"hallucination_flagged": True, "hallucination_turns": []}
    )
    assert sev is None and modes == []

    sev2, modes2 = classify_hallucination(
        chat,
        artifact,
        {"hallucination_flagged": True, "hallucination_turns": [0]},
    )
    assert sev2 == "P0"
    assert "false_refund_processed" in modes2
    assert "llm_flagged_no_claim_keyword" not in modes2

    # All claims GT-supported and no refund-processed phrase → no hard gate.
    sev3, modes3 = classify_hallucination(
        {"messages": [{"role": "assistant", "content": "Your order has shipped"}]},
        artifact,
        {"hallucination_flagged": True, "hallucination_turns": [0]},
    )
    assert sev3 is None and modes3 == []


def test_round3_segment_severity_only_when_implicated():
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_turns": [3],
        "hallucination_failure_modes": ["false_refund"],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [0, 1],
                "letter_grade": "E",
                "hallucination_flagged": False,
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [3],
                "letter_grade": "E",
                "hallucination_flagged": True,
                "hallucination_turns": [3],
                "resolution": "yes",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" not in bot
    assert "hallucination_severity:P0" not in bot
    assert "hallucination:false_refund" not in bot
    assert "hallucination" in human
    assert "hallucination_severity:P0" in human
    assert "hallucination:false_refund" in human


def test_round3_soft_flag_alone_does_not_inherit_hard_severity():
    """Human soft-flagged with empty turns must not inherit chat P0 from bot turns."""
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_turns": [0],
        "hallucination_failure_modes": ["false_refund"],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [0],
                "letter_grade": "E",
                "hallucination_flagged": True,
                "hallucination_turns": [0],
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "E",
                # Soft LLM flag with no intersecting turns.
                "hallucination_flagged": True,
                "hallucination_turns": [],
                "resolution": "yes",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" in bot
    assert "hallucination_severity:P0" in bot
    assert "hallucination:false_refund" in bot
    assert "hallucination" not in human
    assert "hallucination_severity:P0" not in human
    assert "hard_gate:inherited_hallucination" in human


def test_round3_attr_binds_only_with_parent_present():
    parents = {"traceability_failure"}
    assert _bind_attr_parent("traceability:unsupported_refund", parents) == "traceability_failure"
    assert _bind_attr_parent("hallucination:false_refund", parents) is None
    assert _bind_attr_parent("pii:phone", parents) is None


def test_round3_ensure_turn_indexes_from_chat_roles():
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_turns": [1],
        "hallucination_failure_modes": ["false_refund"],
        "chat": {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "bot", "content": "refund done"},
                {"role": "user", "content": "ok"},
                {"role": "agent", "content": "checking"},
            ]
        },
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [],  # missing — must rebuild
                "letter_grade": "E",
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [],
                "letter_grade": "E",
                "resolution": "yes",
            },
        },
    }
    filled = ensure_segment_turn_indexes(ev)
    assert filled["segment_evals"][SEGMENT_BOT]["turn_indexes"] == [1]
    assert filled["segment_evals"][SEGMENT_HUMAN_AGENT]["turn_indexes"] == [3]
    bot = set(segment_scoped_issues(filled, SEGMENT_BOT))
    human = set(segment_scoped_issues(filled, SEGMENT_HUMAN_AGENT))
    assert "hallucination" in bot
    assert "hallucination:false_refund" in bot
    assert "hallucination" not in human
    assert "hard_gate:inherited_hallucination" in human


def test_round3_soft_flag_empty_turns_does_not_inherit_modes():
    """Soft flag without intersecting turns must not create a soft parent."""
    ev = {
        "hallucination_flagged": True,
        "hallucination_turns": [1],
        "hallucination_failure_modes": ["false_refund"],
        "hard_gate_reasons": [],
        "segment_evals": {
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "hallucination_flagged": True,
                "hallucination_turns": [],
                "resolution": "partial",
            }
        },
    }
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" not in human
    assert "hallucination_severity:soft" not in human
    assert "hallucination:false_refund" not in human


def test_round3_soft_flag_with_intersecting_turns_emits_soft():
    ev = {
        "hallucination_flagged": True,
        "hallucination_turns": [2],
        "hallucination_failure_modes": [],
        "hard_gate_reasons": [],
        "segment_evals": {
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "hallucination_flagged": True,
                "hallucination_turns": [2],
                "resolution": "partial",
            }
        },
    }
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" in human
    assert "hallucination_severity:soft" in human
    assert "hallucination:false_refund" not in human


def test_round3_empty_ground_truth_does_not_strip_hard_or_invent():
    """Empty {} GT: do not invent modes; do not strip existing hard reasons."""
    chat = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "refund processed"},
        ]
    }
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_flagged": True,
        "hallucination_turns": [1],
        "hallucination_failure_modes": ["false_refund"],
        "traceability_pass": False,
        "untraceable_turns": [1],
        "traceability_failure_reasons": ["unsupported_refund"],
    }
    out = enrich_eval_attr_fields(ev, chat=chat, artifact={}, force=True)
    assert out.get("_attr_enrich_skipped") is not True
    assert out.get("_attr_enrich_partial")
    assert "hallucination_P0" in (out.get("hard_gate_reasons") or [])
    assert out.get("hallucination_failure_modes") == ["false_refund"]
    assert out.get("traceability_failure_reasons") == ["unsupported_refund"]


def test_round3_sparse_empty_content_gt_preserves_hard():
    """Truthy but empty-content GT: classify invents nothing; enrich does not strip."""
    chat = {"messages": [{"role": "assistant", "content": "Your order shipped and refund processed."}]}
    sparse = {"preflight": {}, "order_ops": {}}
    sev, modes = classify_hallucination(
        chat,
        sparse,
        {"hallucination_flagged": True, "hallucination_turns": [0]},
    )
    assert sev is None
    assert modes == []

    out = enrich_eval_attr_fields(
        {
            "hallucination_flagged": True,
            "hallucination_turns": [0],
            "hard_gate_reasons": ["hallucination_P0"],
            "hallucination_severity": "P0",
            "hallucination_failure_modes": ["false_shipment"],
        },
        chat=chat,
        artifact=sparse,
        force=True,
    )
    assert out.get("_attr_enrich_partial")
    assert "hallucination_P0" in (out.get("hard_gate_reasons") or [])
    assert out.get("hallucination_severity") == "P0"


def test_round3_missing_artifact_still_skips_enrich():
    chat = {"messages": [{"role": "assistant", "content": "shipped"}]}
    out = enrich_eval_attr_fields(
        {"hallucination_flagged": True, "hallucination_turns": [0]},
        chat=chat,
        artifact=None,
        force=True,
    )
    assert out.get("_attr_enrich_skipped") is True


def test_round3_enrich_strips_stale_hard_reasons_on_empty_turns():
    chat = {
        "messages": [
            {"role": "user", "content": "where"},
            {"role": "assistant", "content": "Your order shipped and refund processed."},
        ]
    }
    gt = {
        "preflight": {"order_status": "Placed"},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }
    # Stale P0 from legacy full-transcript scan; empty turns ⇒ classify None.
    out = enrich_eval_attr_fields(
        {
            "hallucination_flagged": True,
            "hallucination_turns": [],
            "hard_gate_reasons": ["hallucination_P0"],
            "hallucination_severity": "P0",
            "hallucination_failure_modes": ["false_refund"],
        },
        chat=chat,
        artifact=gt,
        force=True,
    )
    assert out.get("_attr_enrich_skipped") is not True
    assert out.get("hallucination_failure_modes") == []
    assert out.get("hallucination_severity") is None
    assert "hallucination_P0" not in (out.get("hard_gate_reasons") or [])
    tags = set(composite_issues(out))
    assert "hallucination_severity:P0" not in tags
    # Empty turns: soft composite parent is also skipped (parity with segments).
    assert "hallucination" not in tags
    assert "hallucination_severity:soft" not in tags


def test_round3_pii_composite_only_segments_get_inherited():
    ev = {
        "hard_gate_reasons": ["pii_incident"],
        "pii_incident_types": ["phone"],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "E",
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2],
                "letter_grade": "E",
                "resolution": "yes",
            },
        },
    }
    assert "hard_gate:pii" in set(composite_issues(ev))
    assert "pii:phone" in set(composite_issues(ev))
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hard_gate:pii" not in bot and "pii:phone" not in bot
    assert "hard_gate:inherited_pii" in bot
    assert "hard_gate:inherited_pii" in human


def test_round3_policy_guardrail_empty_turns_composite_only():
    """Empty-turn rules stay on composite; do not paint every speaker."""
    ev = {
        "violations": [{"rule": "order_selection_before_action"}],
        "policy_violations": [{"rule": "unsupported refund claim"}],
        "letter_grade": "C",
        "resolution": "yes",
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "C",
                "resolution": "yes",
                "guardrail_violations": 1,
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2],
                "letter_grade": "C",
                "resolution": "yes",
            },
        },
    }
    comp = set(composite_issues(ev))
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "guardrail:order_selection_before_action" in comp
    assert "policy:unsupported_refund" in comp
    assert "guardrail:order_selection_before_action" not in bot
    assert "policy:unsupported_refund" not in bot
    assert "guardrail:order_selection_before_action" not in human
    assert "policy:unsupported_refund" not in human
    # Bot still gets opaque count fallback when turn-less rules were filtered.
    assert "guardrail_violations" in bot


def test_round3_hall_modes_severity_recomputed_per_segment_turns():
    """Multi-speaker hard hall: each speaker gets modes/severity from their turns only."""
    ev = {
        "hard_gate_reasons": ["hallucination_P0"],
        "hallucination_severity": "P0",
        "hallucination_flagged": True,
        "hallucination_turns": [1, 3],
        "hallucination_failure_modes": ["false_refund", "false_cancellation"],
        "chat": {
            "messages": [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "We will process your refund today."},
                {"role": "user", "content": "y"},
                {"role": "agent", "content": "Your order was cancelled already."},
            ]
        },
        "eval_ground_truth": {
            "preflight": {"order_status": "Placed"},
            "order_ops": {"payment_summary": {"total_refund_due": 0}},
        },
        "refund_fact_check": {
            "claim_kind": "status",
            "severity": "P0",
            "turns": [1],
            "failure_mode": "false_refund",
        },
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "E",
                "hallucination_flagged": True,
                "hallucination_turns": [1],
                "resolution": "yes",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "E",
                "hallucination_flagged": True,
                "hallucination_turns": [3],
                "resolution": "yes",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "hallucination" in bot
    assert "hallucination:false_refund" in bot
    assert "hallucination:false_cancellation" not in bot
    assert "hallucination_severity:P0" in bot
    assert "hallucination" in human
    assert "hallucination:false_cancellation" in human
    assert "hallucination:false_refund" not in human
    # Cancellation is P1 when scoped to that turn alone.
    assert "hallucination_severity:P1" in human
    assert "hallucination_severity:P0" not in human


def test_round3_trace_score_disagreement_uses_segment_scores():
    ev = {
        "traceability_pass": False,
        "untraceable_turns": [1, 3],
        "chat": {
            "messages": [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "Refund coming."},
                {"role": "user", "content": "y"},
                {"role": "agent", "content": "Order shipped."},
            ]
        },
        "eval_ground_truth": {
            "preflight": {"order_status": "Placed"},
            "order_ops": {"payment_summary": {"total_refund_due": 0}},
        },
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [1],
                "det_traceability_score": 0,
                "llm_traceability_score": 90,
                "resolution": "partial",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [3],
                "det_traceability_score": 100,
                "llm_traceability_score": 100,
                "resolution": "partial",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "traceability:score_disagreement" in bot
    assert "traceability:score_disagreement" not in human


def test_round3_trace_empty_turns_emits_score_disagreement():
    ev = {
        "traceability_pass": False,
        "untraceable_turns": [],
        "chat": {"messages": [{"role": "assistant", "content": "ok"}]},
        "eval_ground_truth": {"preflight": {"order_status": "Placed"}},
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [0],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [],
                "det_traceability_score": 100,
                "llm_traceability_score": 40,
                "resolution": "yes",
            }
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    assert "traceability_failure" in bot
    assert "traceability:score_disagreement" in bot

    """Multi-speaker: each segment gets reasons from its own untraceable turns."""
    ev = {
        "traceability_pass": False,
        "untraceable_turns": [1, 3],
        "traceability_failure_reasons": ["unsupported_refund", "unsupported_shipment"],
        "chat": {
            "messages": [
                {"role": "user", "content": "where is my order"},
                {"role": "assistant", "content": "We will process your refund today."},
                {"role": "user", "content": "and shipping?"},
                {
                    "role": "agent",
                    "content": "Your order has shipped already.",
                },
            ]
        },
        "eval_ground_truth": {
            "preflight": {"order_status": "Placed"},
            "order_ops": {"payment_summary": {"total_refund_due": 0}},
        },
        "refund_fact_check": {
            "claim_kind": "status",
            "severity": "P0",
            "turns": [1],
            "failure_mode": "false_refund",
        },
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [1],
                "resolution": "partial",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [3],
                "resolution": "partial",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "traceability_failure" in bot
    assert "traceability:unsupported_refund" in bot
    assert "traceability:unsupported_shipment" not in bot
    assert "traceability_failure" in human
    assert "traceability:unsupported_shipment" in human
    assert "traceability:unsupported_refund" not in human


def test_round3_trace_reasons_without_chat_falls_back_subset_gate():
    # No chat/GT ⇒ do not copy chat-wide reasons onto both speakers.
    ev = {
        "traceability_pass": False,
        "untraceable_turns": [1, 3],
        "traceability_failure_reasons": ["unsupported_refund", "nuance_or_paraphrase"],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [1],
                "resolution": "partial",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "traceability_pass": False,
                "untraceable_turns": [3],
                "resolution": "partial",
            },
        },
    }
    bot = set(segment_scoped_issues(ev, SEGMENT_BOT))
    human = set(segment_scoped_issues(ev, SEGMENT_HUMAN_AGENT))
    assert "traceability_failure" in bot
    assert "traceability:unsupported_refund" not in bot
    assert "traceability_failure" in human
    assert "traceability:unsupported_refund" not in human

    # Sparse shape (no segment untr) — same withholding via chat-level list:
    sparse = {
        "traceability_pass": False,
        "untraceable_turns": [1, 3],
        "traceability_failure_reasons": ["unsupported_refund"],
        "segment_evals": {
            SEGMENT_BOT: {
                "graded": True,
                "turn_indexes": [1],
                "letter_grade": "C",
                "traceability_pass": False,
                "resolution": "partial",
            },
            SEGMENT_HUMAN_AGENT: {
                "graded": True,
                "turn_indexes": [2, 3],
                "letter_grade": "C",
                "traceability_pass": False,
                "resolution": "partial",
            },
        },
    }
    assert "traceability:unsupported_refund" not in set(
        segment_scoped_issues(sparse, SEGMENT_BOT)
    )

    # When all untr in one segment, stored reasons may attach without chat:
    ev2 = dict(ev)
    ev2["untraceable_turns"] = [1]
    ev2["traceability_failure_reasons"] = ["unsupported_refund"]
    ev2["segment_evals"] = {
        SEGMENT_BOT: {**ev["segment_evals"][SEGMENT_BOT], "untraceable_turns": [1]},
        SEGMENT_HUMAN_AGENT: {
            **ev["segment_evals"][SEGMENT_HUMAN_AGENT],
            "untraceable_turns": [],
            "traceability_pass": True,
        },
    }
    bot2 = set(segment_scoped_issues(ev2, SEGMENT_BOT))
    assert "traceability:unsupported_refund" in bot2
