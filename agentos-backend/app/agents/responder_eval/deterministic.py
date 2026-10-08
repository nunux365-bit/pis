"""Deterministic evals: structural, guardrails, traceability, hallucination pre-check.

Deterministic checks are intentionally narrow — obvious keyword mismatches only.
Ambiguous, date-specific, or paraphrased claims are left to the LLM judge
(eval_ground_truth + chat).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.agents.responder_eval.constants import CHAT_ROLE_AGENT, CHAT_ROLE_USER, is_agent_side_role, is_bot_role

_BANNED_ADJ = re.compile(r"\b(friendly|helpful|kind|nice)\b", re.I)
_SOLUTION_VERBS = re.compile(r"\b(refund|cancel|replace|reship|delivered|shipped)\b", re.I)
_ACTION_VERBS = re.compile(r"\b(refund|cancel|replace|reship)\b", re.I)
_ABSENCE_RE = re.compile(
    r"\b(?:don't|do not|cannot|can't|unable to|not available|no information|don't have)\b",
    re.I,
)
_ORDER_SELECT_RE = re.compile(r"order|select_order|choose_order", re.I)
_ORDER_ID_IN_TEXT = re.compile(r"\bPO\d{10,}\b", re.I)
_POLICY_CITE = re.compile(r"\b(eta|refund|policy|status)\b", re.I)

# Factual claim roots in agent text → predicate on normalized ground-truth blob.
_CLAIM_CHECKS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("delivered", lambda t: "deliver" in t),
    ("delivery", lambda t: "deliver" in t),
    ("shipped", lambda t: "ship" in t),
    ("dispatch", lambda t: "ship" in t or "dispatch" in t),
    ("dispatched", lambda t: "ship" in t or "dispatch" in t),
    ("cancelled", lambda t: "cancel" in t),
    ("canceled", lambda t: "cancel" in t),
)

_TRACEABILITY_PASS_SCORE = 80


def _messages(chat: dict[str, Any]) -> list[dict[str, Any]]:
    return list(chat.get("messages") or [])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _refund_grounded(artifact: dict[str, Any]) -> bool:
    payment = (artifact.get("order_ops") or {}).get("payment_summary") or {}
    for key in ("total_refund_due", "total_refunded_amount", "online_refund_due"):
        try:
            if float(payment.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    if payment.get("online_refund_initiated"):
        return True
    truth = _norm(str((artifact.get("preflight") or {}).get("order_status") or ""))
    return "refund" in truth


def _ground_truth_blob(artifact: dict[str, Any]) -> str:
    pf = artifact.get("preflight") or {}
    ops = artifact.get("operations") or {}
    order_ops = artifact.get("order_ops") or {}
    payment = order_ops.get("payment_summary") or {}
    shipment = order_ops.get("shipment_detail") or {}
    parts = [
        str(pf.get("order_status") or ""),
        str(pf.get("is_eta_breached") or ""),
        str(pf.get("return_reason") or ""),
        str(ops.get("status_chronology") or ""),
        str(ops.get("status_transitions") or ""),
        str(ops.get("shipping_summary") or ""),
        str(ops.get("return_followed") or ""),
        str(ops.get("return_note") or ""),
        str(ops.get("segments") or ""),
        str(ops.get("clickpost_events") or ""),
        str(shipment.get("status") or ""),
        str(shipment.get("delivery_date") or ""),
        str(payment.get("total_refund_due") or ""),
        str(payment.get("total_refunded_amount") or ""),
        str(payment.get("online_refund_initiated") or ""),
        str(payment.get("online_refund_due") or ""),
        str(artifact.get("rca_verdict") or ""),
    ]
    return _norm(" ".join(parts))


def _has_policy_metadata(chat: dict[str, Any]) -> bool:
    return any((m.get("metadata") or {}).get("policy_version_id") for m in _messages(chat))


def _metadata_traceability(chat: dict[str, Any]) -> dict[str, Any]:
    msgs = _messages(chat)
    policy_turns = [
        i
        for i, m in enumerate(msgs)
        if is_agent_side_role(m.get("role"))
        and (m.get("metadata") or {}).get("policy_version_id")
    ]
    cites_policy = any(
        _POLICY_CITE.search(str(m.get("content") or ""))
        for m in msgs
        if is_agent_side_role(m.get("role"))
    )
    if cites_policy and not policy_turns:
        return {
            "traceability_pass": False,
            "traceability_score": 0,
            "traceability_mode": "metadata",
            "missing_policy_version_turns": True,
        }
    return {
        "traceability_pass": True,
        "traceability_score": 100,
        "traceability_mode": "metadata",
    }


def _claim_supported(claim: str, truth_blob: str, artifact: dict[str, Any]) -> bool:
    if claim == "refund":
        return _refund_grounded(artifact)
    if not truth_blob:
        return False
    for needle, matcher in _CLAIM_CHECKS:
        if claim == needle:
            return matcher(truth_blob)
    return claim in truth_blob


def _claim_in_text(text: str, claim: str) -> bool:
    normalized = _norm(text)
    if not re.search(rf"\b{re.escape(claim)}\b", normalized):
        return False
    if re.search(
        rf"\b(?:not|no|never|without)\s+(?:\w+\s+){{0,3}}{re.escape(claim)}\b",
        normalized,
    ):
        return False
    if re.search(rf"\b{re.escape(claim)}\s+not\b", normalized):
        return False
    return True


def _refund_processed_ungrounded(text: str, artifact: dict[str, Any]) -> bool:
    """Assertive refund-processed claim while GT shows no refund activity."""
    norm_text = _norm(text)
    if "processed" not in norm_text or not re.search(r"\brefund", norm_text):
        return False
    if not _claim_in_text(norm_text, "refund"):
        return False
    return not _refund_grounded(artifact)


def _extract_claims(text: str) -> list[str]:
    normalized = _norm(text)
    if not normalized:
        return []
    claims: list[str] = []
    for needle, _matcher in _CLAIM_CHECKS:
        if _claim_in_text(normalized, needle):
            claims.append(needle)
    return claims


def _ground_truth_traceability(chat: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """Traceability via Order RCA ground truth (default path — chat has no policy ids)."""
    truth_blob = _ground_truth_blob(artifact)
    if not truth_blob.strip():
        return {
            "traceability_pass": True,
            "traceability_score": 100,
            "traceability_mode": "ground_truth",
            "traceability_skipped": True,
            "traceability_skip_reason": "no_ground_truth",
            "claims_checked": 0,
        }

    supported = 0
    unsupported = 0
    untraceable_turns: list[int] = []

    for i, m in enumerate(_messages(chat)):
        if not is_agent_side_role(m.get("role")):
            continue
        text = str(m.get("content") or "")
        claims = _extract_claims(text)
        refund_ungrounded = _refund_processed_ungrounded(text, artifact)
        if not claims and not refund_ungrounded:
            continue
        turn_ok = True
        for claim in claims:
            if _claim_supported(claim, truth_blob, artifact):
                supported += 1
            else:
                unsupported += 1
                turn_ok = False
        if refund_ungrounded:
            unsupported += 1
            turn_ok = False
        if not turn_ok:
            untraceable_turns.append(i)

    checked = supported + unsupported
    if checked == 0:
        return {
            "traceability_pass": True,
            "traceability_score": 100,
            "traceability_mode": "ground_truth",
            "claims_checked": 0,
            "claims_supported": 0,
        }

    score = int(round(100 * supported / checked))
    return {
        "traceability_pass": score >= _TRACEABILITY_PASS_SCORE,
        "traceability_score": score,
        "traceability_mode": "ground_truth",
        "claims_checked": checked,
        "claims_supported": supported,
        "untraceable_turns": sorted(set(untraceable_turns)),
    }


def traceability_check(chat: dict[str, Any], artifact: dict[str, Any] | None = None) -> dict[str, Any]:
    if _has_policy_metadata(chat):
        return _metadata_traceability(chat)
    return _ground_truth_traceability(chat, artifact or {})


def structural_signals(chat: dict[str, Any]) -> dict[str, Any]:
    msgs = _messages(chat)
    bot = [m for m in msgs if is_bot_role(m.get("role"))]
    human = [m for m in msgs if str(m.get("role")).lower() == CHAT_ROLE_AGENT]
    user = [m for m in msgs if str(m.get("role")).lower() == CHAT_ROLE_USER]
    bot_words = sum(len(str(m.get("content") or "").split()) for m in bot)
    user_words = sum(len(str(m.get("content") or "").split()) for m in user) or 1
    flags: list[str] = []
    if len(msgs) < 4:
        flags.append("low_turn_count")
    if bot_words > 3 * user_words:
        flags.append("imbalanced_talk_ratio")
    max_bot = max((len(str(m.get("content") or "").split()) for m in bot), default=0)
    if max_bot > 120:
        flags.append("long_bot_monologue")
    return {
        "turn_count": len(msgs),
        "bot_turns": len(bot),
        "human_agent_turns": len(human),
        "user_turns": len(user),
        "flags": flags,
    }


def _user_order_selected_turn(msgs: list[dict[str, Any]]) -> int | None:
    for i, m in enumerate(msgs):
        if str(m.get("role")).lower() != "user":
            continue
        meta = m.get("metadata") or {}
        key = str(meta.get("key") or "")
        content = str(m.get("content") or "")
        if _ORDER_SELECT_RE.search(key) or _ORDER_ID_IN_TEXT.search(content):
            return i
    return None


def _absence_fingerprint(text: str) -> str | None:
    if not _ABSENCE_RE.search(text):
        return None
    return _norm(text)[:100]


def guardrail_checks(chat: dict[str, Any]) -> dict[str, Any]:
    msgs = _messages(chat)
    violations: list[dict[str, Any]] = []
    user_idxs = [i for i, m in enumerate(msgs) if str(m.get("role")).lower() == "user"]
    agent_idxs = [i for i, m in enumerate(msgs) if is_agent_side_role(m.get("role"))]
    first_user = user_idxs[0] if user_idxs else None
    for i in agent_idxs:
        text = str(msgs[i].get("content") or "")
        if first_user is None and _SOLUTION_VERBS.search(text):
            violations.append({"rule": "no_solution_before_concern", "turns": [i]})
            break
        if first_user is not None and i < first_user and _SOLUTION_VERBS.search(text):
            violations.append({"rule": "no_solution_before_concern", "turns": [i]})
            break
    for i in agent_idxs[:3]:
        text = str(msgs[i].get("content") or "")
        if _BANNED_ADJ.search(text) and "i am" in text.lower():
            violations.append({"rule": "no_subjective_self_description", "turns": [i]})
            break

    absence_prints: dict[str, list[int]] = {}
    for i in agent_idxs:
        fp = _absence_fingerprint(str(msgs[i].get("content") or ""))
        if fp:
            absence_prints.setdefault(fp, []).append(i)
    for turns in absence_prints.values():
        if len(turns) >= 2:
            violations.append({"rule": "explain_absence_dont_repeat", "turns": turns[:2]})
            break

    order_select_turn = _user_order_selected_turn(msgs)
    for i in agent_idxs:
        text = str(msgs[i].get("content") or "")
        if not _ACTION_VERBS.search(text):
            continue
        if order_select_turn is None or i < order_select_turn:
            violations.append({"rule": "order_selection_before_action", "turns": [i]})
            break

    return {"guardrail_violations": len({v["rule"] for v in violations}), "violations": violations}


def hallucination_precheck(chat: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """Flag unsupported factual claims vs Order RCA ground truth."""
    truth_blob = _ground_truth_blob(artifact)
    flagged_turns: list[int] = []
    facts_missed = 0

    if not truth_blob.strip():
        return {
            "hallucination_flagged": False,
            "hallucination_turns": [],
            "hallucination_facts_missed": 0,
        }

    for i, m in enumerate(_messages(chat)):
        if not is_agent_side_role(m.get("role")):
            continue
        text = str(m.get("content") or "")
        turn_missed = 0
        for claim in _extract_claims(text):
            if not _claim_supported(claim, truth_blob, artifact):
                turn_missed += 1
        if _refund_processed_ungrounded(text, artifact):
            turn_missed += 1
        if turn_missed > 0:
            flagged_turns.append(i)
            facts_missed += turn_missed

    return {
        "hallucination_flagged": bool(flagged_turns),
        "hallucination_turns": sorted(set(flagged_turns)),
        "hallucination_facts_missed": facts_missed,
    }


def run_deterministic(chat: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out.update(structural_signals(chat))
    out.update(guardrail_checks(chat))
    out.update(traceability_check(chat, artifact))
    out.update(hallucination_precheck(chat, artifact))
    return out
