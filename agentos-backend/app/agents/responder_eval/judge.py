"""gpt-5.4-mini judge (mockable) — compact structured output."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.responder_eval.constants import chat_speaker_for_judge
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT, segment_turn_indexes
from app.config.settings import settings
from app.infra.openai_async_client import get_shared_openai_client

log = logging.getLogger(__name__)

_REFUND_FACT_CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claim_kind": {
            "type": "string",
            "enum": ["none", "option", "question", "status"],
        },
        "severity": {"type": "string", "enum": ["none", "P1", "P0"]},
        "turns": {"type": "array", "items": {"type": "integer"}},
        "failure_mode": {
            "type": "string",
            "enum": ["none", "false_refund", "false_refund_processed"],
        },
    },
    "required": ["claim_kind", "severity", "turns", "failure_mode"],
}

_JUDGE_SEGMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "present": {"type": "boolean"},
        "resolution": {"type": "string", "enum": ["yes", "partial", "no", "not_applicable"]},
        "policy_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "tonality_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "sentiment_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "hallucination_flagged": {"type": "boolean"},
        "hallucination_turns": {"type": "array", "items": {"type": "integer"}},
        "traceability_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "untraceable_turns": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "present",
        "resolution",
        "policy_score",
        "tonality_score",
        "sentiment_score",
        "hallucination_flagged",
        "hallucination_turns",
        "traceability_score",
        "untraceable_turns",
    ],
}

_JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "policy_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "policy_violations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "rule": {"type": "string"},
                    "turns": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["rule", "turns"],
            },
        },
        "resolution": {"type": "string", "enum": ["yes", "partial", "no", "not_applicable"]},
        "hallucination_flagged": {"type": "boolean"},
        "hallucination_turns": {"type": "array", "items": {"type": "integer"}},
        "traceability_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "untraceable_turns": {"type": "array", "items": {"type": "integer"}},
        "ground_truth_citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "turn": {"type": "integer"},
                    "field": {"type": "string"},
                },
                "required": ["turn", "field"],
            },
        },
        "tonality_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "tonality_turns": {"type": "array", "items": {"type": "integer"}},
        "sentiment_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "sentiment_turns": {"type": "array", "items": {"type": "integer"}},
        "policy_lifecycle_stage": {
            "type": "string",
            "enum": [
                "pre_delivery",
                "in_transit",
                "delivered",
                "refund",
                "cancellation",
                "unknown",
                "cannot_assess",
            ],
        },
        "segment_evals": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "bot": _JUDGE_SEGMENT_SCHEMA,
                "human_agent": _JUDGE_SEGMENT_SCHEMA,
            },
            "required": ["bot", "human_agent"],
        },
        "refund_fact_check": _REFUND_FACT_CHECK_SCHEMA,
    },
    "required": [
        "policy_score",
        "policy_violations",
        "resolution",
        "hallucination_flagged",
        "hallucination_turns",
        "traceability_score",
        "untraceable_turns",
        "ground_truth_citations",
        "tonality_score",
        "tonality_turns",
        "sentiment_score",
        "sentiment_turns",
        "policy_lifecycle_stage",
        "segment_evals",
        "refund_fact_check",
    ],
}

_SYSTEM = (
    "You are a customer-support chat quality judge. Output JSON only. "
    "Cite turn indexes only — never quote message text. "
    "Use eval_ground_truth as source of truth for factual checks. "
    "deterministic_flags are advisory hints only; you are authoritative on ambiguous or "
    "paraphrased claims (dates, ETAs, return eligibility, partial delivery). "
    "Transcript speakers (each message has role + speaker): "
    "customer (role=user) is the end customer; "
    "bot (role=bot or assistant) is the automated IVA/scripted bot including queue and system copy; "
    "human_agent (role=agent) is a live human support agent after handoff. "
    "Customer turns establish the issue; do not score them for factual traceability. "
    "For traceability and hallucination: score factual claims on bot and human_agent turns "
    "(order status, refund, ETA, cancellation, delivery timing, return workflow) against "
    "eval_ground_truth. Prefer operations.status_chronology and status_transitions for status/timing; "
    "operations.segments and clickpost_events for logistics; operations.return_followed and "
    "preflight.return_reason for returns; order_ops.shipment_detail and payment_summary for "
    "delivery/refund facts; sku_price_increases / sku_price_decreases for per-SKU MRP changes; "
    "post_order_sku_removals.items for high-confidence Deleted sku history and cart_vs_order_qty "
    "reductions; ambiguous_not_on_order when gone from family without delete proof. "
    "Cite supporting field paths in ground_truth_citations. "
    "Empathy or generic replies are N/A for traceability. "
    "For resolution: assess whether the customer's concern was addressed end-to-end (top-level "
    "resolution field). Also fill segment_evals.bot and segment_evals.human_agent: set present=true "
    "only when that speaker has turns; score each segment independently (bot handling vs live agent). "
    "Use not_applicable on a segment when that speaker has no turns. "
    "Weight human_agent turns most heavily for overall resolution once live support is involved; "
    "if only bot spoke, overall resolution reflects bot handling only. "
    "Top-level not_applicable only when no bot or human_agent turn exists in the transcript. "
    "For tonality and sentiment: prioritize human_agent professionalism and empathy when present. "
    "For policy_lifecycle_stage: infer from eval_ground_truth.preflight.order_status and "
    "payment/refund fields (pre_delivery, in_transit, delivered, refund, cancellation). "
    "Use cannot_assess when unclear. "
    "refund_fact_check: classify refund-related bot/human_agent language only. "
    "claim_kind=status only when the speaker asserts refund state (due, initiated, processed, "
    "amount credited) as fact — not when offering return/refund as an option (claim_kind=option), "
    "asking preference (question), or when refund is not discussed (none). "
    "Set severity P0/P1 only for status claims that contradict eval_ground_truth payment_summary "
    "and order status; use failure_mode false_refund_processed when a processed refund is claimed "
    "with zero refund due/refunded. Mentioning return/refund in e.g. menus must be option/none. "
    "Be very concise — use the fewest words possible. "
    "Keep each turn-index array and ground_truth_citations to at most 15 items "
    "(worst violations only). Keep policy_violations.rule strings short (a few words). "
    "Output must be complete, valid JSON — never truncate mid-string."
)

JUDGE_SYSTEM = _SYSTEM
JUDGE_SCHEMA = _JUDGE_SCHEMA
JUDGE_MAX_COMPLETION_TOKENS = 12_000
_JUDGE_PARSE_RETRIES = 2
_RETRY_OUTPUT_SUFFIX = (
    " Previous output was too long or invalid. "
    "Be even more concise; cap each turn-index array and ground_truth_citations at 10 items."
)


class JudgeError(RuntimeError):
    """OpenAI judge call failed in production mode."""


def _mock_segment_eval(*, present: bool, resolution: str, policy_score: int) -> dict[str, Any]:
    return {
        "present": present,
        "resolution": resolution,
        "policy_score": policy_score,
        "tonality_score": 80 if present else 0,
        "sentiment_score": 90 if present else 0,
        "hallucination_flagged": False,
        "hallucination_turns": [],
        "traceability_score": 100 if present else 0,
        "untraceable_turns": [],
    }


def _mock_refund_fact_check(chat: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    from app.agents.responder_eval.constants import is_agent_side_role
    from app.agents.responder_eval.deterministic import _norm, _refund_grounded, _refund_processed_ungrounded

    none = {
        "claim_kind": "none",
        "severity": "none",
        "turns": [],
        "failure_mode": "none",
    }
    for i, m in enumerate(chat.get("messages") or []):
        if not is_agent_side_role(m.get("role")):
            continue
        text = str(m.get("content") or "")
        if _refund_processed_ungrounded(text, artifact):
            return {
                "claim_kind": "status",
                "severity": "P0",
                "turns": [i],
                "failure_mode": "false_refund_processed",
            }
        norm = _norm(text)
        if "refund" not in norm:
            continue
        if any(tok in norm for tok in ("e.g.", "eg.", "such as", "return/refund")) or "what you want" in norm:
            return {
                "claim_kind": "option",
                "severity": "none",
                "turns": [],
                "failure_mode": "none",
            }
        if not _refund_grounded(artifact) and not norm.endswith("?"):
            return {
                "claim_kind": "status",
                "severity": "P0",
                "turns": [i],
                "failure_mode": "false_refund",
            }
    return none


def mock_judge_result(chat: dict[str, Any], artifact: dict[str, Any], det: dict[str, Any]) -> dict[str, Any]:
    msgs = chat.get("messages") or []
    indexes = segment_turn_indexes(chat)
    has_bot = bool(indexes[SEGMENT_BOT])
    has_human = bool(indexes[SEGMENT_HUMAN_AGENT])
    has_agent = has_bot or has_human
    po = (artifact.get("perfect_order") or {})
    resolution = "not_applicable" if not has_agent else ("yes" if po.get("overall_pass") else "partial")
    hal_flag = bool(det.get("hallucination_flagged"))
    det_trace = det.get("traceability_score")
    if det_trace is None:
        det_trace = 100 if det.get("traceability_pass", True) else 0
    seg_resolution = resolution if resolution != "not_applicable" else "not_applicable"
    return {
        "policy_score": 85 if has_agent else 0,
        "policy_violations": [],
        "resolution": resolution,
        "hallucination_flagged": hal_flag,
        "hallucination_turns": det.get("hallucination_turns") or [],
        "hallucination_facts_missed": int(det.get("hallucination_facts_missed") or 0),
        "traceability_score": int(det_trace),
        "untraceable_turns": det.get("untraceable_turns") or [],
        "ground_truth_citations": [],
        "tonality_score": 80,
        "tonality_turns": [],
        "sentiment_score": 90,
        "sentiment_turns": [],
        "policy_lifecycle_stage": _infer_lifecycle_stage(artifact),
        "refund_fact_check": _mock_refund_fact_check(chat, artifact),
        "segment_evals": {
            SEGMENT_BOT: _mock_segment_eval(
                present=has_bot,
                resolution=seg_resolution if has_bot else "not_applicable",
                policy_score=85 if has_bot else 0,
            ),
            SEGMENT_HUMAN_AGENT: _mock_segment_eval(
                present=has_human,
                resolution=seg_resolution if has_human else "not_applicable",
                policy_score=80 if has_human else 0,
            ),
        },
    }


def _infer_lifecycle_stage(artifact: dict[str, Any]) -> str:
    status = str((artifact.get("preflight") or {}).get("order_status") or "").lower()
    payment = (artifact.get("order_ops") or {}).get("payment_summary") or {}
    try:
        refund_due = float(payment.get("total_refund_due") or 0)
    except (TypeError, ValueError):
        refund_due = 0.0
    if refund_due > 0 or payment.get("online_refund_initiated"):
        return "refund"
    if "cancel" in status:
        return "cancellation"
    if "deliver" in status:
        return "delivered"
    if any(k in status for k in ("transit", "ship", "dispatch", "out for")):
        return "in_transit"
    if status:
        return "pre_delivery"
    return "unknown"


def prepare_chat_for_judge(chat: dict[str, Any]) -> dict[str, Any]:
    """Attach speaker labels so the judge can distinguish customer, bot, and human agent."""
    messages = chat.get("messages") or []
    prepared: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        enriched = dict(msg)
        enriched["speaker"] = chat_speaker_for_judge(role)
        prepared.append(enriched)
    out = {
        **chat,
        "role_legend": {
            "customer": "End customer (role=user)",
            "bot": "Automated IVA / scripted bot (role=bot or assistant)",
            "human_agent": "Live human support agent (role=agent)",
        },
        "messages": prepared,
    }
    meta = out.get("metadata")
    if isinstance(meta, dict):
        cleaned = {
            k: v
            for k, v in meta.items()
            if k not in ("handoff_bucket", "handoff_sub_bucket", "seed_seq")
        }
        if cleaned:
            out["metadata"] = cleaned
        else:
            out.pop("metadata", None)
    return out


def _deterministic_flags(det: dict[str, Any]) -> dict[str, Any]:
    return {
        "hallucination_flagged": det.get("hallucination_flagged"),
        "hallucination_turns": det.get("hallucination_turns"),
        "hallucination_facts_missed": det.get("hallucination_facts_missed"),
        "guardrail_violations": det.get("guardrail_violations", 0),
        "violations": det.get("violations") or [],
        "traceability_pass": det.get("traceability_pass"),
        "traceability_score": det.get("traceability_score"),
        "traceability_mode": det.get("traceability_mode"),
        "traceability_skipped": det.get("traceability_skipped"),
        "untraceable_turns": det.get("untraceable_turns"),
        "claims_checked": det.get("claims_checked"),
    }


def _parse_judge_response(raw: str, *, finish_reason: str | None) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty judge response")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if finish_reason == "length":
            raise ValueError(
                f"truncated judge response (finish_reason=length, len={len(text)}): {exc}"
            ) from exc
        raise ValueError(f"invalid judge JSON (len={len(text)}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("judge response must be a JSON object")
    if finish_reason == "length":
        log.warning(
            "responder_eval judge finish_reason=length but JSON parsed OK (len=%s)",
            len(text),
        )
    return data


async def run_judge(
    chat: dict[str, Any],
    artifact: dict[str, Any],
    det: dict[str, Any],
) -> dict[str, Any]:
    if settings.responder_eval_mock_judge:
        return mock_judge_result(chat, artifact, det)
    if not (settings.openai_api_key or "").strip():
        raise JudgeError(
            "OPENAI_API_KEY is required for responder eval judge "
            "(set RESPONDER_EVAL_MOCK_JUDGE=true for local tests)"
        )

    user = json.dumps(
        {
            "chat": prepare_chat_for_judge(chat),
            "eval_ground_truth": artifact,
            "deterministic_flags": _deterministic_flags(det),
        },
        default=str,
    )
    model = settings.responder_eval_openai_model or "gpt-5.4-mini"
    last_parse_err: Exception | None = None
    try:
        client = get_shared_openai_client()
        for attempt in range(_JUDGE_PARSE_RETRIES):
            system = _SYSTEM + (_RETRY_OUTPUT_SUFFIX if attempt > 0 else "")
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "responder_eval",
                        "strict": True,
                        "schema": _JUDGE_SCHEMA,
                    },
                },
                max_completion_tokens=JUDGE_MAX_COMPLETION_TOKENS,
                temperature=0,
            )
            choice = resp.choices[0]
            raw = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
            try:
                return _parse_judge_response(raw, finish_reason=finish_reason)
            except ValueError as exc:
                last_parse_err = exc
                if attempt + 1 < _JUDGE_PARSE_RETRIES:
                    log.warning(
                        "responder_eval judge output invalid attempt=%s/%s; retrying: %s",
                        attempt + 1,
                        _JUDGE_PARSE_RETRIES,
                        exc,
                    )
                    continue
        raise JudgeError(str(last_parse_err)) from last_parse_err
    except JudgeError:
        raise
    except Exception as exc:
        log.exception("responder_eval judge failed")
        raise JudgeError(str(exc)) from exc
