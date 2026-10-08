"""Heuristic Phase-2 traceability failure reasons (closed enum for TEXT[] attrs)."""

from __future__ import annotations

from typing import Any

from app.agents.responder_eval.constants import is_agent_side_role

TRACEABILITY_REASONS = frozenset(
    {
        "unsupported_refund",
        "unsupported_shipment",
        "unsupported_delivery",
        "unsupported_cancellation",
        "nuance_or_paraphrase",
        "no_claim_keyword",
        "score_disagreement",
        "other",
    }
)

_CLAIM_TO_REASON = {
    "delivered": "unsupported_delivery",
    "delivery": "unsupported_delivery",
    "shipped": "unsupported_shipment",
    "dispatched": "unsupported_shipment",
    "dispatch": "unsupported_shipment",
    "refund": "unsupported_refund",
    "cancelled": "unsupported_cancellation",
    "canceled": "unsupported_cancellation",
}


def compute_traceability_failure_reasons(
    chat: dict[str, Any],
    artifact: dict[str, Any],
    ev: dict[str, Any],
    *,
    untraceable_turns: list[int] | None = None,
    include_score_disagreement: bool = True,
) -> list[str]:
    """Derive closed ``traceability:*`` reason ids for a failing chat (or turn subset).

    Pass ``untraceable_turns`` to scope heuristics to a speaker segment instead of
    the full chat-level failure list.
    """
    if ev.get("traceability_pass") is not False:
        return []

    from app.agents.responder_eval.deterministic import (
        _claim_supported,
        _extract_claims,
        _ground_truth_blob,
        _messages,
        _refund_processed_ungrounded,
    )

    reasons: set[str] = set()
    truth_blob = _ground_truth_blob(artifact)
    if not truth_blob.strip():
        return ["other"]

    turns_raw = (
        untraceable_turns
        if untraceable_turns is not None
        else (ev.get("untraceable_turns") or [])
    )
    turns: set[int] = set()
    if isinstance(turns_raw, list):
        for t in turns_raw:
            try:
                turns.add(int(t))
            except (TypeError, ValueError):
                continue
    msgs = _messages(chat)
    any_supported_flagged = False
    any_no_keyword = False
    refund_status_turns: set[int] = set()
    rfc = ev.get("refund_fact_check")
    if isinstance(rfc, dict) and str(rfc.get("claim_kind") or "").lower() == "status":
        if str(rfc.get("severity") or "none").upper() in ("P0", "P1"):
            for t in rfc.get("turns") or []:
                try:
                    refund_status_turns.add(int(t))
                except (TypeError, ValueError):
                    continue

    if turns:
        for i in turns:
            if i < 0 or i >= len(msgs):
                continue
            m = msgs[i]
            if not is_agent_side_role(m.get("role")):
                reasons.add("other")
                continue
            text = str(m.get("content") or "")
            claims = _extract_claims(text)
            if i in refund_status_turns or _refund_processed_ungrounded(text, artifact):
                reasons.add("unsupported_refund")
            if not claims and i not in refund_status_turns and not _refund_processed_ungrounded(
                text, artifact
            ):
                any_no_keyword = True
                continue
            for claim in claims:
                if _claim_supported(claim, truth_blob, artifact):
                    any_supported_flagged = True
                else:
                    reasons.add(_CLAIM_TO_REASON.get(claim, "other"))
    else:
        reasons.add("other")

    if any_no_keyword:
        reasons.add("no_claim_keyword")
    if any_supported_flagged:
        reasons.add("nuance_or_paraphrase")

    if include_score_disagreement:
        det = ev.get("det_traceability_score")
        llm = ev.get("llm_traceability_score")
        try:
            if det is not None and llm is not None:
                det_i, llm_i = int(det), int(llm)
                if (det_i >= 80 and llm_i < 80) or (llm_i >= 80 and det_i < 80):
                    reasons.add("score_disagreement")
        except (TypeError, ValueError):
            pass

    cleaned = sorted(r for r in reasons if r in TRACEABILITY_REASONS)
    return cleaned or ["other"]
