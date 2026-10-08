"""Denormalized columns and eval_json storage split for responder eval runs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agents.responder_eval.handoff_taxonomy import handoff_row_fields
from app.agents.responder_eval.issue_tags import compute_issue_tags
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT, segment_row_fields

_BULK_KEYS = frozenset({"chat", "eval_ground_truth"})
_DENORM_KEYS = frozenset({"handoff_bucket", "handoff_sub_bucket"})


def resolution_row_fields(eval_result: dict[str, Any]) -> dict[str, str | None]:
    segs = eval_result.get("segment_evals")
    if not isinstance(segs, dict):
        segs = {}
    bot_seg = segs.get(SEGMENT_BOT)
    human_seg = segs.get(SEGMENT_HUMAN_AGENT)
    composite = str(eval_result.get("resolution") or "").strip() or None
    bot_res = (
        str(bot_seg.get("resolution") or "").strip()
        if isinstance(bot_seg, dict) and bot_seg.get("resolution")
        else None
    )
    human_res = (
        str(human_seg.get("resolution") or "").strip()
        if isinstance(human_seg, dict) and human_seg.get("resolution")
        else None
    )
    return {
        "composite_resolution": composite,
        "bot_resolution": bot_res,
        "human_resolution": human_res,
    }


def split_eval_storage(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Rubric-only eval_json plus separate chat / ground-truth blobs."""
    rubric = {
        k: v for k, v in result.items() if k not in _BULK_KEYS and k not in _DENORM_KEYS
    }
    chat = result.get("chat")
    ground_truth = result.get("eval_ground_truth")
    chat_json = deepcopy(chat) if isinstance(chat, dict) else None
    ground_truth_json = deepcopy(ground_truth) if isinstance(ground_truth, dict) else None
    return rubric, chat_json, ground_truth_json


def build_persist_row_fields(eval_result: dict[str, Any]) -> dict[str, Any]:
    """All denormalized DB columns derived from a full eval result dict."""
    from app.agents.responder_eval.issue_tags import enrich_eval_attr_fields

    enriched = enrich_eval_attr_fields(eval_result)
    enriched.pop("_attr_enrich_skipped", None)
    enriched.pop("_attr_enrich_skip_reason", None)
    enriched.pop("_attr_enrich_partial", None)
    # Keep caller dict in sync so split_eval_storage persists attr + hard-gate fields.
    for key in (
        "hallucination_failure_modes",
        "traceability_failure_reasons",
        "hard_gate_reasons",
        "hard_gate",
        "hallucination_severity",
    ):
        if key in enriched:
            eval_result[key] = enriched[key]
    return {
        **segment_row_fields(enriched),
        **resolution_row_fields(enriched),
        **compute_issue_tags(enriched),
        **handoff_row_fields(enriched),
    }


def merge_eval_detail_json(
    eval_json: dict[str, Any] | None,
    *,
    chat_json: dict[str, Any] | None,
    ground_truth_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reconstruct API-facing eval dict from split storage."""
    ev = dict(eval_json or {})
    if chat_json is not None:
        ev["chat"] = chat_json
    if ground_truth_json is not None:
        ev["eval_ground_truth"] = ground_truth_json
    return ev
