"""Hard gates — PII incident and P0/P1 hallucination → Grade E."""

from __future__ import annotations

from typing import Any

from app.agents.responder_eval.constants import is_agent_side_role

P0_CLAIMS = frozenset({"delivered", "delivery", "shipped", "dispatched", "dispatch"})
P1_CLAIMS = frozenset({"cancelled", "canceled"})

_CLAIM_TO_MODE = {
    "delivered": "false_delivery",
    "delivery": "false_delivery",
    "shipped": "false_shipment",
    "dispatched": "false_shipment",
    "dispatch": "false_shipment",
    "cancelled": "false_cancellation",
    "canceled": "false_cancellation",
}

HALLUCINATION_FAILURE_MODES = frozenset(_CLAIM_TO_MODE.values()) | {
    "false_refund",
    "false_refund_processed",
    "llm_flagged_no_claim_keyword",
}


def _safe_turn_set(raw: Any) -> set[int]:
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for t in raw:
        try:
            out.add(int(t))
        except (TypeError, ValueError):
            continue
    return out


def artifact_has_claim_evidence(artifact: dict[str, Any] | None) -> bool:
    """True when GT has enough substance to judge unsupported factual claims."""
    if not isinstance(artifact, dict) or not artifact:
        return False
    from app.agents.responder_eval.deterministic import _ground_truth_blob

    if _ground_truth_blob(artifact).strip():
        return True
    payment = (artifact.get("order_ops") or {}).get("payment_summary")
    if isinstance(payment, dict) and payment:
        return True
    if artifact.get("rca_verdict"):
        return True
    return False


def chat_has_messages(chat: dict[str, Any] | None) -> bool:
    if not isinstance(chat, dict):
        return False
    messages = chat.get("messages")
    return isinstance(messages, list) and len(messages) > 0


def classify_hallucination(
    chat: dict[str, Any],
    artifact: dict[str, Any],
    hal: dict[str, Any],
) -> tuple[str | None, list[str]]:
    """Return (severity P0|P1|None, closed failure-mode ids)."""
    if not hal.get("hallucination_flagged"):
        return None, []

    from app.agents.responder_eval.deterministic import (
        _claim_supported,
        _extract_claims,
        _ground_truth_blob,
        _messages,
        _refund_processed_ungrounded,
    )

    # Align with hallucination_precheck: empty/sparse GT cannot prove unsupported claims.
    if not artifact_has_claim_evidence(artifact):
        return None, []

    truth_blob = _ground_truth_blob(artifact)
    severity: str | None = None
    modes: set[str] = set()
    # Match legacy filter: membership in hallucination_turns. An empty turns list
    # means no turns are inspected (do not scan the whole transcript).
    turns = _safe_turn_set(hal.get("hallucination_turns"))
    saw_claim_keyword = False

    for i, m in enumerate(_messages(chat)):
        if int(i) not in turns:
            continue
        if not is_agent_side_role(m.get("role")):
            continue
        text = str(m.get("content") or "")
        for claim in _extract_claims(text):
            saw_claim_keyword = True
            if _claim_supported(claim, truth_blob, artifact):
                continue
            modes.add(_CLAIM_TO_MODE.get(claim, "llm_flagged_no_claim_keyword"))
            if claim in P0_CLAIMS:
                severity = "P0"
            elif claim in P1_CLAIMS:
                severity = severity or "P1"
        if _refund_processed_ungrounded(text, artifact):
            modes.add("false_refund_processed")
            severity = "P0"
            saw_claim_keyword = True

    # Fallback only when turns were flagged but no factual claim keywords mapped.
    # If keywords were present and all GT-supported, do not force a hard gate.
    if severity is None and turns and not saw_claim_keyword and not modes:
        rfc = hal.get("refund_fact_check")
        if isinstance(rfc, dict) and str(rfc.get("claim_kind") or "").lower() == "status":
            if str(rfc.get("severity") or "none").upper() in ("P0", "P1"):
                pass  # LLM refund classification owns this turn set.
            else:
                severity = "P1"
                modes.add("llm_flagged_no_claim_keyword")
        else:
            severity = "P1"
            modes.add("llm_flagged_no_claim_keyword")

    cleaned = sorted(m for m in modes if m in HALLUCINATION_FAILURE_MODES)
    return severity, cleaned


def _max_hallucination_severity(a: str | None, b: str | None) -> str | None:
    rank = {"P0": 2, "P1": 1}
    if rank.get(a or "", 0) >= rank.get(b or "", 0):
        return a
    return b


def _refund_failure_mode(raw: Any) -> str | None:
    key = str(raw or "none").strip()
    if key in ("false_refund", "false_refund_processed"):
        return key
    return None


def _llm_refund_hallucination(result: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Refund hard-gate signal from judge refund_fact_check (status claims only)."""
    rfc = result.get("refund_fact_check")
    if not isinstance(rfc, dict):
        return None, []
    claim_kind = str(rfc.get("claim_kind") or "none").lower()
    if claim_kind != "status":
        return None, []
    severity = str(rfc.get("severity") or "none").upper()
    if severity not in ("P0", "P1"):
        return None, []
    turns = _safe_turn_set(rfc.get("turns"))
    if not turns:
        return None, []
    mode = _refund_failure_mode(rfc.get("failure_mode")) or "false_refund"
    return severity, [mode] if mode in HALLUCINATION_FAILURE_MODES else []


def merge_refund_fact_check_hal(result: dict[str, Any]) -> dict[str, Any]:
    """Fold LLM refund status claims into hallucination fields for composite scoring."""
    severity, modes = _llm_refund_hallucination(result)
    if severity is None:
        return result
    rfc = result.get("refund_fact_check") or {}
    turns = _safe_turn_set(rfc.get("turns"))
    if not turns:
        return result
    out = dict(result)
    merged_turns = sorted(_safe_turn_set(out.get("hallucination_turns")) | turns)
    out["hallucination_turns"] = merged_turns
    out["hallucination_flagged"] = bool(merged_turns)
    missed = int(out.get("hallucination_facts_missed") or 0)
    out["hallucination_facts_missed"] = max(missed, len(merged_turns))
    if modes:
        out["hallucination_failure_modes"] = sorted(
            set(out.get("hallucination_failure_modes") or []) | set(modes)
        )
    return out


def resolve_hallucination_gate(
    chat: dict[str, Any],
    artifact: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str | None, list[str]]:
    """Merge deterministic delivery/cancel gates, LLM refund status, and processed backstop."""
    det_sev, det_modes = classify_hallucination(chat, artifact, result)
    llm_sev, llm_modes = _llm_refund_hallucination(result)
    modes = set(det_modes)
    if llm_modes:
        modes |= set(llm_modes)
        modes.discard("llm_flagged_no_claim_keyword")
    severity = _max_hallucination_severity(det_sev, llm_sev)
    if llm_sev in ("P0", "P1") and llm_modes:
        severity = llm_sev
    return severity, sorted(m for m in modes if m in HALLUCINATION_FAILURE_MODES)


def classify_hallucination_severity(
    chat: dict[str, Any],
    artifact: dict[str, Any],
    hal: dict[str, Any],
) -> str | None:
    """Classify unsupported factual claims as P0 (critical) or P1."""
    severity, _modes = classify_hallucination(chat, artifact, hal)
    return severity


def _zero_graded_segment_evals(result: dict[str, Any]) -> dict[str, Any] | None:
    segs = result.get("segment_evals")
    if not isinstance(segs, dict):
        return None
    out: dict[str, Any] = {}
    for key, seg in segs.items():
        if isinstance(seg, dict) and seg.get("graded"):
            out[key] = {**seg, "composite_score": 0, "letter_grade": "E"}
        else:
            out[key] = seg
    return out


def apply_hard_gates(
    result: dict[str, Any],
    *,
    pii_check: dict[str, Any],
    hallucination_severity: str | None,
    hallucination_failure_modes: list[str] | None = None,
) -> dict[str, Any]:
    """Force composite 0 / Grade E when hard gates trigger on graded evals.

    PII incidents on not-graded evals set ``persist_blocked`` so the tick does not
    write potentially leaky chat text to the database.
    """
    reasons: list[str] = []
    if pii_check.get("pii_incident"):
        reasons.append("pii_incident")
    if result.get("graded") and hallucination_severity in ("P0", "P1"):
        reasons.append(f"hallucination_{hallucination_severity}")

    modes = [
        str(m)
        for m in (hallucination_failure_modes or [])
        if str(m) in HALLUCINATION_FAILURE_MODES
    ]
    out = {
        **result,
        "pii_incident": bool(pii_check.get("pii_incident")),
        "pii_incident_types": pii_check.get("pii_incident_types") or [],
        "hallucination_severity": hallucination_severity,
        "hallucination_failure_modes": modes,
    }
    if not reasons:
        out["hard_gate"] = False
        out["hard_gate_reasons"] = []
        return out

    out["hard_gate"] = True
    out["hard_gate_reasons"] = reasons
    if result.get("graded"):
        out["composite_score"] = 0
        out["letter_grade"] = "E"
        segment_evals = _zero_graded_segment_evals(result)
        if segment_evals is not None:
            out["segment_evals"] = segment_evals
    elif pii_check.get("pii_incident"):
        out["persist_blocked"] = True
    return out
