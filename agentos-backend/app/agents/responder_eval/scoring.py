"""Deterministic scoring — composite + letter grade (PDF v1.1)."""

from __future__ import annotations

from typing import Any

from app.agents.responder_eval.constants import (
    COMPOSITE_WEIGHTS,
    GRADE_BANDS,
    LETTER_GRADE_NOT_GRADED,
    RESOLUTION_SCORES,
    TRACEABILITY_PASS_SCORE,
)
from app.agents.responder_eval.segments import (
    SEGMENT_KEYS,
    merge_segment_hallucination,
    merge_segment_traceability,
    segment_guardrail_violations,
    segment_turn_indexes,
)


def is_not_graded_resolution(resolution: str | None) -> bool:
    return str(resolution or "").lower() == "not_applicable"


def _guardrail_score(violation_count: int) -> int:
    return max(0, 100 - 25 * max(0, violation_count))


def letter_grade(composite: int) -> str:
    for grade, lo, hi in GRADE_BANDS:
        if lo <= composite <= hi:
            return grade
    return "E"


def _det_traceability_score(det: dict[str, Any]) -> int | None:
    raw = det.get("traceability_score")
    if raw is not None:
        return int(raw)
    if det.get("traceability_skipped"):
        return None
    if "traceability_pass" in det:
        return 100 if det.get("traceability_pass") else 0
    return None


def merge_traceability(det: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    """Hybrid traceability: deterministic floor + LLM for paraphrase/nuance."""
    llm_score = llm.get("traceability_score")
    llm_int = int(llm_score) if llm_score is not None else None

    if det.get("traceability_skipped"):
        return {
            "traceability_score": None,
            "traceability_pass": None,
            "traceability_skipped": True,
            "traceability_mode": str(det.get("traceability_mode") or "skipped"),
            "det_traceability_score": None,
            "llm_traceability_score": llm_int,
            "untraceable_turns": [],
            "ground_truth_citations": llm.get("ground_truth_citations") or [],
        }

    det_score = _det_traceability_score(det)
    if det_score is not None and llm_int is not None:
        final = min(det_score, llm_int)
        mode = "hybrid"
    elif llm_int is not None:
        final = llm_int
        mode = "llm"
    elif det_score is not None:
        final = det_score
        mode = str(det.get("traceability_mode") or "deterministic")
    else:
        final = 100
        mode = "skipped"

    det_turns = set(det.get("untraceable_turns") or [])
    llm_turns = set(llm.get("untraceable_turns") or [])
    return {
        "traceability_score": final,
        "traceability_pass": final >= TRACEABILITY_PASS_SCORE,
        "traceability_mode": mode,
        "det_traceability_score": det_score,
        "llm_traceability_score": llm_int,
        "untraceable_turns": sorted(det_turns | llm_turns),
        "ground_truth_citations": llm.get("ground_truth_citations") or [],
    }


def _traceability_score(evals: dict[str, Any]) -> int:
    if evals.get("traceability_skipped"):
        return 100
    raw = evals.get("traceability_score")
    if raw is not None:
        return int(raw)
    return 100 if evals.get("traceability_pass") else 0


def _policy_score_from_llm(llm: dict[str, Any]) -> int:
    """Use judge policy_score as-is; policy_violations are diagnostic only."""
    return max(0, min(100, int(llm.get("policy_score") or 0)))


def _merge_hallucination(det: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    """Union det + LLM turns; require turn evidence for the flag (parity with tags)."""
    det_turns = {int(t) for t in (det.get("hallucination_turns") or [])}
    llm_turns = {int(t) for t in (llm.get("hallucination_turns") or [])}
    all_turns = sorted(det_turns | llm_turns)
    det_facts = int(det.get("hallucination_facts_missed") or 0)
    llm_extra = len(llm_turns - det_turns)
    facts_missed = det_facts + llm_extra
    if facts_missed == 0 and all_turns:
        facts_missed = len(all_turns)
    # Empty turns + flag-only must not tank composite score while tags stay silent.
    flagged = bool(all_turns)
    return {
        "hallucination_flagged": flagged,
        "hallucination_turns": all_turns,
        "hallucination_facts_missed": facts_missed if flagged else 0,
    }


def _sentiment_score_from_llm(llm: dict[str, Any]) -> int:
    raw = llm.get("sentiment_score")
    if raw is None:
        return 100
    return max(0, min(100, int(raw)))


def composite_score(evals: dict[str, Any]) -> int:
    res_key = str(evals.get("resolution") or "no").lower()
    res_score = RESOLUTION_SCORES.get(res_key)
    if res_score is None:
        return 0

    parts = {
        "policy_adherence": int(evals.get("policy_score") or 0),
        "resolution_quality": res_score,
        "guardrail": _guardrail_score(int(evals.get("guardrail_violations") or 0)),
        "hallucination": 0 if evals.get("hallucination_flagged") else 100,
        "traceability": _traceability_score(evals),
        "tonality": int(evals.get("tonality_score") or 0),
        "sentiment": _sentiment_score_from_llm(evals),
    }
    total = sum(parts[k] * COMPOSITE_WEIGHTS[k] for k in COMPOSITE_WEIGHTS)
    return int(round(total))


def _build_segment_eval(
    seg_key: str,
    turn_indexes: list[int],
    det: dict[str, Any],
    llm_seg: dict[str, Any],
    hal: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any] | None:
    if not turn_indexes:
        return None
    allowed = set(turn_indexes)
    seg_hal = merge_segment_hallucination(hal, llm_seg, allowed)
    seg_trace = merge_segment_traceability(det, trace, llm_seg, allowed)
    resolution = str(llm_seg.get("resolution") or "not_applicable").lower()
    base: dict[str, Any] = {
        "segment": seg_key,
        "turn_indexes": turn_indexes,
        "present": bool(llm_seg.get("present", True)),
        "resolution": resolution,
        "policy_score": max(0, min(100, int(llm_seg.get("policy_score") or 0))),
        "tonality_score": max(0, min(100, int(llm_seg.get("tonality_score") or 0))),
        "sentiment_score": max(0, min(100, int(llm_seg.get("sentiment_score") or 100))),
        "guardrail_violations": segment_guardrail_violations(det, allowed),
        **seg_hal,
        **seg_trace,
    }
    if is_not_graded_resolution(resolution):
        return {
            **base,
            "graded": False,
            "not_graded": True,
            "composite_score": None,
            "letter_grade": LETTER_GRADE_NOT_GRADED,
        }
    comp = composite_score(base)
    return {
        **base,
        "graded": True,
        "not_graded": False,
        "composite_score": comp,
        "letter_grade": letter_grade(comp),
    }


def _llm_segment_payload(
    llm: dict[str, Any],
    llm_segments: dict[str, Any],
    seg_key: str,
    *,
    has_turns: bool,
) -> dict[str, Any]:
    raw = llm_segments.get(seg_key) if isinstance(llm_segments.get(seg_key), dict) else {}
    if raw:
        return raw
    if not has_turns:
        return {
            "present": False,
            "resolution": "not_applicable",
            "policy_score": 0,
            "tonality_score": 0,
            "sentiment_score": 0,
            "hallucination_flagged": False,
            "hallucination_turns": [],
            "traceability_score": 0,
            "untraceable_turns": [],
        }
    return {
        "present": True,
        "resolution": llm.get("resolution", "not_applicable"),
        "policy_score": llm.get("policy_score", 0),
        "tonality_score": llm.get("tonality_score", 0),
        "sentiment_score": llm.get("sentiment_score", 100),
        "hallucination_flagged": llm.get("hallucination_flagged", False),
        "hallucination_turns": [],
        "traceability_score": llm.get("traceability_score", 100),
        "untraceable_turns": [],
    }


def build_segment_evals(
    chat: dict[str, Any],
    det: dict[str, Any],
    llm: dict[str, Any],
    hal: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Per-segment scores (bot / human_agent) alongside overall eval."""
    indexes = segment_turn_indexes(chat)
    llm_segments = llm.get("segment_evals") if isinstance(llm.get("segment_evals"), dict) else {}
    out: dict[str, Any] = {}
    for seg_key in SEGMENT_KEYS:
        turns = indexes[seg_key]
        seg_llm = _llm_segment_payload(llm, llm_segments, seg_key, has_turns=bool(turns))
        built = _build_segment_eval(seg_key, turns, det, seg_llm, hal, trace)
        if built is not None:
            out[seg_key] = built
    return out


def apply_scores(det: dict[str, Any], llm: dict[str, Any], chat: dict[str, Any]) -> dict[str, Any]:
    hal = _merge_hallucination(det, llm)
    resolution = str(llm.get("resolution") or "no").lower()
    trace = merge_traceability(det, llm)
    policy_score = _policy_score_from_llm(llm)

    if is_not_graded_resolution(resolution):
        segment_evals = build_segment_evals(chat, det, llm, hal, trace)
        return {
            **det,
            **llm,
            **trace,
            **hal,
            "policy_score": policy_score,
            "graded": False,
            "not_graded": True,
            "not_graded_reason": "not_applicable",
            "composite_score": None,
            "letter_grade": LETTER_GRADE_NOT_GRADED,
            "segment_evals": segment_evals,
        }

    merged = {
        "guardrail_violations": det.get("guardrail_violations", 0),
        **hal,
        "policy_score": policy_score,
        "resolution": resolution,
        "tonality_score": llm.get("tonality_score", 0),
        "sentiment_score": _sentiment_score_from_llm(llm),
        **trace,
    }
    comp = composite_score(merged)
    segment_evals = build_segment_evals(chat, det, llm, hal, trace)
    return {
        **det,
        **llm,
        **trace,
        **hal,
        "policy_score": policy_score,
        "graded": True,
        "not_graded": False,
        "composite_score": comp,
        "letter_grade": letter_grade(comp),
        "segment_evals": segment_evals,
    }
