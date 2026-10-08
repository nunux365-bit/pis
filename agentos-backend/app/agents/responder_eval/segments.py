"""Chat segment turn indexes (bot vs human agent) for per-segment eval."""

from __future__ import annotations

from typing import Any

from app.agents.responder_eval.constants import (
    CHAT_ROLE_AGENT,
    LETTER_GRADE_NOT_GRADED,
    TRACEABILITY_PASS_SCORE,
    is_bot_role,
)

SEGMENT_BOT = "bot"
SEGMENT_HUMAN_AGENT = "human_agent"
SEGMENT_KEYS = (SEGMENT_BOT, SEGMENT_HUMAN_AGENT)


def segment_turn_indexes(chat: dict[str, Any]) -> dict[str, list[int]]:
    """Map transcript turn indexes to bot vs human_agent by message role."""
    bot_turns: list[int] = []
    human_turns: list[int] = []
    for i, msg in enumerate(chat.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        if role == CHAT_ROLE_AGENT:
            human_turns.append(i)
        elif is_bot_role(role):
            bot_turns.append(i)
    return {SEGMENT_BOT: bot_turns, SEGMENT_HUMAN_AGENT: human_turns}


def _filter_turns(turns: Any, allowed: set[int]) -> list[int]:
    if not turns or not allowed:
        return []
    out: list[int] = []
    for t in turns:
        try:
            idx = int(t)
        except (TypeError, ValueError):
            continue
        if idx in allowed:
            out.append(idx)
    return sorted(set(out))


def segment_guardrail_violations(det: dict[str, Any], allowed: set[int]) -> int:
    if not allowed:
        return 0
    rules: set[str] = set()
    for violation in det.get("violations") or []:
        if not isinstance(violation, dict):
            continue
        turns = violation.get("turns") or []
        if any(int(t) in allowed for t in turns if str(t).isdigit() or isinstance(t, int)):
            rule = str(violation.get("rule") or "")
            if rule:
                rules.add(rule)
    return len(rules)


def merge_segment_hallucination(
    hal: dict[str, Any],
    llm_seg: dict[str, Any],
    allowed: set[int],
) -> dict[str, Any]:
    """Per-segment hallucination: merged chat flags + judge segment turns."""
    det_turns = _filter_turns(hal.get("hallucination_turns"), allowed)
    llm_turns = _filter_turns(llm_seg.get("hallucination_turns"), allowed)
    all_turns = sorted(set(det_turns) | set(llm_turns))
    # Require turn evidence. Do not inherit chat LLM flag with empty turns —
    # that tanks score while issue tags (which need turns) stay silent.
    flagged = bool(all_turns)
    facts_missed = len(all_turns)
    if facts_missed == 0 and flagged:
        facts_missed = max(1, int(llm_seg.get("hallucination_facts_missed") or 0))
    return {
        "hallucination_flagged": flagged,
        "hallucination_turns": all_turns,
        "hallucination_facts_missed": facts_missed,
    }


def merge_segment_traceability(
    det: dict[str, Any],
    merged_trace: dict[str, Any],
    llm_seg: dict[str, Any],
    allowed: set[int],
) -> dict[str, Any]:
    """Per-segment traceability: segment turns + judge segment score (hybrid with det)."""
    empty: dict[str, Any] = {
        "traceability_score": None,
        "traceability_pass": None,
        "traceability_skipped": False,
        "untraceable_turns": [],
    }
    if not allowed:
        return empty
    if det.get("traceability_skipped") or merged_trace.get("traceability_skipped"):
        return {**empty, "traceability_skipped": True}

    det_turns = _filter_turns(det.get("untraceable_turns"), allowed)
    llm_turns = _filter_turns(llm_seg.get("untraceable_turns"), allowed)
    merged_turns = _filter_turns(merged_trace.get("untraceable_turns"), allowed)
    all_turns = sorted(set(det_turns) | set(llm_turns) | set(merged_turns))

    llm_int: int | None = None
    raw_llm = llm_seg.get("traceability_score")
    if raw_llm is not None:
        try:
            llm_int = max(0, min(100, int(raw_llm)))
        except (TypeError, ValueError):
            llm_int = None

    det_score: int | None = 0 if det_turns else None

    if all_turns:
        final = 0
        mode = "hybrid" if llm_int is not None else "deterministic"
    elif det_score is not None and llm_int is not None:
        final = min(det_score, llm_int)
        mode = "hybrid"
    elif llm_int is not None:
        final = llm_int
        mode = "llm"
    elif det_score is not None:
        final = det_score
        mode = "deterministic"
    else:
        final = 100
        mode = "segment_clean"

    return {
        "traceability_score": final,
        "traceability_pass": final >= TRACEABILITY_PASS_SCORE,
        "traceability_mode": mode,
        "det_traceability_score": det_score,
        "llm_traceability_score": llm_int,
        "traceability_skipped": False,
        "untraceable_turns": all_turns,
    }


def segment_row_fields(eval_result: dict[str, Any]) -> dict[str, int | str | None]:
    """Denormalized bot/human score+grade for responder_eval_runs columns."""
    segs = eval_result.get("segment_evals")
    if not isinstance(segs, dict):
        segs = {}
    out: dict[str, int | str | None] = {
        "bot_score": None,
        "human_score": None,
        "bot_grade": LETTER_GRADE_NOT_GRADED,
        "human_grade": LETTER_GRADE_NOT_GRADED,
    }
    for seg_key, score_key, grade_key in (
        (SEGMENT_BOT, "bot_score", "bot_grade"),
        (SEGMENT_HUMAN_AGENT, "human_score", "human_grade"),
    ):
        seg = segs.get(seg_key)
        if not isinstance(seg, dict):
            continue
        grade = str(seg.get("letter_grade") or LETTER_GRADE_NOT_GRADED)
        out[grade_key] = grade
        if seg.get("graded") and seg.get("composite_score") is not None:
            out[score_key] = int(seg["composite_score"])
    return out
