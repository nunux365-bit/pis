"""Segment eval (bot vs human agent) tests."""

from __future__ import annotations

from app.agents.responder_eval.constants import CHAT_ROLE_AGENT, CHAT_ROLE_BOT, CHAT_ROLE_USER
from app.agents.responder_eval.scoring import apply_scores, build_segment_evals, merge_traceability
from app.agents.responder_eval.segments import (
    SEGMENT_BOT,
    SEGMENT_HUMAN_AGENT,
    merge_segment_hallucination,
    merge_segment_traceability,
    segment_row_fields,
    segment_turn_indexes,
)


def _llm_segment_evals(
    *,
    bot_present: bool = True,
    human_present: bool = False,
    bot_resolution: str = "partial",
    human_resolution: str = "no",
) -> dict:
    def seg(present: bool, resolution: str) -> dict:
        return {
            "present": present,
            "resolution": resolution if present else "not_applicable",
            "policy_score": 80 if present else 0,
            "tonality_score": 75 if present else 0,
            "sentiment_score": 85 if present else 0,
            "hallucination_flagged": False,
            "hallucination_turns": [],
            "traceability_score": 90 if present else 0,
            "untraceable_turns": [],
        }

    return {
        "segment_evals": {
            SEGMENT_BOT: seg(bot_present, bot_resolution),
            SEGMENT_HUMAN_AGENT: seg(human_present, human_resolution),
        }
    }


def test_segment_turn_indexes_splits_bot_and_human():
    chat = {
        "messages": [
            {"role": CHAT_ROLE_BOT, "content": "Hi"},
            {"role": CHAT_ROLE_USER, "content": "Help"},
            {"role": CHAT_ROLE_AGENT, "content": "I will assist"},
        ]
    }
    idx = segment_turn_indexes(chat)
    assert idx[SEGMENT_BOT] == [0]
    assert idx[SEGMENT_HUMAN_AGENT] == [2]


def test_apply_scores_includes_segment_evals_for_bot_and_human():
    chat = {
        "messages": [
            {"role": CHAT_ROLE_BOT, "content": "Bot greeting"},
            {"role": CHAT_ROLE_USER, "content": "Issue"},
            {"role": CHAT_ROLE_AGENT, "content": "Human follow-up"},
        ]
    }
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 85,
        "resolution": "partial",
        "hallucination_flagged": False,
        "traceability_score": 90,
        "tonality_score": 80,
        "sentiment_score": 88,
        **_llm_segment_evals(bot_present=True, human_present=True),
    }
    out = apply_scores(det, llm, chat)
    assert out["graded"] is True
    assert out["composite_score"] is not None
    segs = out["segment_evals"]
    assert SEGMENT_BOT in segs
    assert SEGMENT_HUMAN_AGENT in segs
    assert segs[SEGMENT_BOT]["composite_score"] is not None
    assert segs[SEGMENT_HUMAN_AGENT]["composite_score"] is not None
    assert segs[SEGMENT_BOT]["resolution"] == "partial"
    assert segs[SEGMENT_HUMAN_AGENT]["resolution"] == "no"


def test_segment_evals_omits_empty_segment():
    chat = {"messages": [{"role": CHAT_ROLE_BOT, "content": "Only bot"}]}
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 85,
        "resolution": "yes",
        "hallucination_flagged": False,
        "traceability_score": 100,
        "tonality_score": 80,
        "sentiment_score": 90,
        **_llm_segment_evals(bot_present=True, human_present=False),
    }
    segs = build_segment_evals(
        chat,
        det,
        llm,
        {"hallucination_flagged": False, "hallucination_turns": []},
        {"traceability_score": 100},
    )
    assert SEGMENT_BOT in segs
    assert SEGMENT_HUMAN_AGENT not in segs


def test_segment_traceability_scores_segment_turns_only():
    trace = {
        "traceability_score": 0,
        "traceability_pass": False,
        "untraceable_turns": [2],
    }
    det: dict = {"untraceable_turns": [2]}
    bot_llm = {"traceability_score": 90, "untraceable_turns": []}
    human_llm = {"traceability_score": 40, "untraceable_turns": [2]}
    bot_only = merge_segment_traceability(det, trace, bot_llm, {0, 1})
    human_only = merge_segment_traceability(det, trace, human_llm, {2})
    assert bot_only["traceability_score"] == 90
    assert bot_only["traceability_pass"] is True
    assert bot_only["llm_traceability_score"] == 90
    assert human_only["traceability_score"] == 0
    assert human_only["traceability_pass"] is False


def test_segment_traceability_uses_judge_score_without_untraceable_turns():
    trace = {"traceability_score": 90, "untraceable_turns": []}
    det: dict = {"untraceable_turns": []}
    llm_seg = {"traceability_score": 55, "untraceable_turns": []}
    out = merge_segment_traceability(det, trace, llm_seg, {0, 1})
    assert out["traceability_score"] == 55
    assert out["traceability_mode"] == "llm"


def test_segment_traceability_skipped_has_no_score():
    out = merge_segment_traceability({"traceability_skipped": True}, {}, {}, {0})
    assert out["traceability_skipped"] is True
    assert out["traceability_score"] is None


def test_apply_scores_segment_traceability_from_judge_segment_evals():
    chat = {
        "messages": [
            {"role": CHAT_ROLE_BOT, "content": "Bot line"},
            {"role": CHAT_ROLE_AGENT, "content": "Human line"},
        ]
    }
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 85,
        "resolution": "partial",
        "hallucination_flagged": False,
        "traceability_score": 80,
        "tonality_score": 80,
        "sentiment_score": 88,
        "segment_evals": {
            SEGMENT_BOT: {
                "present": True,
                "resolution": "partial",
                "policy_score": 80,
                "tonality_score": 75,
                "sentiment_score": 85,
                "hallucination_flagged": False,
                "hallucination_turns": [],
                "traceability_score": 92,
                "untraceable_turns": [],
            },
            SEGMENT_HUMAN_AGENT: {
                "present": True,
                "resolution": "no",
                "policy_score": 70,
                "tonality_score": 65,
                "sentiment_score": 60,
                "hallucination_flagged": False,
                "hallucination_turns": [],
                "traceability_score": 48,
                "untraceable_turns": [],
            },
        },
    }
    out = apply_scores(det, llm, chat)
    assert out["segment_evals"][SEGMENT_BOT]["traceability_score"] == 92
    assert out["segment_evals"][SEGMENT_HUMAN_AGENT]["traceability_score"] == 48


def test_merge_traceability_skipped_ignores_llm_score():
    det = {"traceability_skipped": True, "traceability_mode": "ground_truth"}
    out = merge_traceability(det, {"traceability_score": 50})
    assert out["traceability_skipped"] is True
    assert out["traceability_score"] is None


def test_merge_segment_hallucination_uses_judge_segment_turns():
    hal = {"hallucination_flagged": False, "hallucination_turns": []}
    llm_seg = {"present": True, "hallucination_flagged": True, "hallucination_turns": [3]}
    out = merge_segment_hallucination(hal, llm_seg, {3})
    assert out["hallucination_flagged"] is True
    assert out["hallucination_turns"] == [3]


def test_merge_segment_hallucination_empty_turns_not_flagged():
    """Chat soft flag with no intersecting turns must not soft-flag the segment."""
    hal = {"hallucination_flagged": True, "hallucination_turns": [9]}
    llm_seg = {"present": True, "hallucination_flagged": True, "hallucination_turns": []}
    out = merge_segment_hallucination(hal, llm_seg, {0, 1})
    assert out["hallucination_flagged"] is False
    assert out["hallucination_turns"] == []


def test_apply_scores_segment_hallucination_from_judge():
    chat = {
        "messages": [
            {"role": CHAT_ROLE_BOT, "content": "Bot"},
            {"role": CHAT_ROLE_AGENT, "content": "Human claimed refund processed"},
        ]
    }
    det = {"guardrail_violations": 0, "traceability_pass": True, "hallucination_flagged": False}
    llm = {
        "policy_score": 85,
        "resolution": "partial",
        "hallucination_flagged": False,
        "hallucination_turns": [],
        "traceability_score": 90,
        "tonality_score": 80,
        "sentiment_score": 88,
        "segment_evals": {
            SEGMENT_BOT: {
                "present": True,
                "resolution": "partial",
                "policy_score": 80,
                "tonality_score": 75,
                "sentiment_score": 85,
                "hallucination_flagged": False,
                "hallucination_turns": [],
                "traceability_score": 90,
                "untraceable_turns": [],
            },
            SEGMENT_HUMAN_AGENT: {
                "present": True,
                "resolution": "no",
                "policy_score": 70,
                "tonality_score": 65,
                "sentiment_score": 60,
                "hallucination_flagged": True,
                "hallucination_turns": [1],
                "traceability_score": 80,
                "untraceable_turns": [],
            },
        },
    }
    out = apply_scores(det, llm, chat)
    bot = out["segment_evals"][SEGMENT_BOT]
    human = out["segment_evals"][SEGMENT_HUMAN_AGENT]
    assert bot["hallucination_flagged"] is False
    assert human["hallucination_flagged"] is True
    assert human["hallucination_turns"] == [1]


def test_segment_row_fields_graded_segments():
    fields = segment_row_fields({
        "segment_evals": {
            "bot": {"graded": True, "composite_score": 72, "letter_grade": "C"},
            "human_agent": {"graded": True, "composite_score": 55, "letter_grade": "D"},
        }
    })
    assert fields == {
        "bot_score": 72,
        "human_score": 55,
        "bot_grade": "C",
        "human_grade": "D",
    }


def test_segment_row_fields_missing_segment():
    fields = segment_row_fields({"segment_evals": {}})
    assert fields["bot_score"] is None
    assert fields["human_score"] is None
    assert fields["bot_grade"] == "-"
    assert fields["human_grade"] == "-"


def test_segment_row_fields_not_graded_segment_keeps_grade_only():
    fields = segment_row_fields({
        "segment_evals": {
            "bot": {"graded": False, "composite_score": None, "letter_grade": "-"},
        }
    })
    assert fields["bot_score"] is None
    assert fields["bot_grade"] == "-"
