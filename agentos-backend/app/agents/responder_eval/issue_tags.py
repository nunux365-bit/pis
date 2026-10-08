"""Pre-computed issue tags for responder eval dashboard aggregates.

Hierarchical TEXT[] tags (parents + attributes) for C/D/E chart nesting.
Charts aggregate these arrays only — never eval_json.
"""

from __future__ import annotations

from typing import Any

from app.agents.responder_eval.constants import LETTER_GRADE_NOT_GRADED
from app.agents.responder_eval.hard_gates import HALLUCINATION_FAILURE_MODES
from app.agents.responder_eval.policy_buckets import normalize_policy_rule
from app.agents.responder_eval.segments import (
    SEGMENT_BOT,
    SEGMENT_HUMAN_AGENT,
    segment_row_fields,
    segment_turn_indexes,
)
from app.agents.responder_eval.traceability_reasons import (
    TRACEABILITY_REASONS,
    compute_traceability_failure_reasons,
)

_LOW_GRADES = frozenset({"C", "D", "E"})

_LIFECYCLE_STAGES = frozenset(
    {
        "pre_delivery",
        "in_transit",
        "delivered",
        "refund",
        "cancellation",
        "unknown",
        "cannot_assess",
    }
)

# Judge often echoes deterministic guardrail rule names into policy_violations.
# Keys are closed policy buckets; values are matching guardrail rule ids.
_POLICY_DUP_OF_GUARDRAIL = {
    "order_selection": "order_selection_before_action",
    "explain_absence": "explain_absence_dont_repeat",
}


def _turn_set(raw: Any) -> set[int]:
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for t in raw:
        try:
            out.add(int(t))
        except (TypeError, ValueError):
            continue
    return out


def _hard_gate_reason_set(ev: dict[str, Any]) -> set[str]:
    return {str(r or "").strip() for r in (ev.get("hard_gate_reasons") or []) if str(r or "").strip()}


def _chat_has_hard_hal(ev: dict[str, Any]) -> bool:
    return any(r.startswith("hallucination_") for r in _hard_gate_reason_set(ev))


def _chat_has_hard_pii(ev: dict[str, Any]) -> bool:
    return "pii_incident" in _hard_gate_reason_set(ev)


def _dedupe_policy_leaked_from_guardrails(issues: list[str]) -> list[str]:
    guard_rules = {t.split(":", 1)[1] for t in issues if t.startswith("guardrail:")}
    if not guard_rules:
        return issues
    out: list[str] = []
    for tag in issues:
        if tag.startswith("policy:"):
            bucket = tag.split(":", 1)[1]
            mirrored = _POLICY_DUP_OF_GUARDRAIL.get(bucket)
            if mirrored and mirrored in guard_rules:
                continue
        out.append(tag)
    return out


def _issues_from_guardrails(items: Any, *, allowed_turns: set[int] | None) -> list[str]:
    issues: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        if not rule:
            continue
        turns = _turn_set(item.get("turns"))
        if allowed_turns is not None:
            # Empty turns = speaker-unknown → composite only (not every segment).
            if not turns or not (turns & allowed_turns):
                continue
        issues.append(f"guardrail:{rule}")
    return issues


def _issues_from_policy(items: Any, *, allowed_turns: set[int] | None) -> list[str]:
    issues: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        if not rule:
            continue
        turns = _turn_set(item.get("turns"))
        if allowed_turns is not None:
            # Empty turns = speaker-unknown → composite only (not every segment).
            if not turns or not (turns & allowed_turns):
                continue
        bucket = normalize_policy_rule(rule)
        issues.append(f"policy:{bucket}")
    return issues


def _severity_tags(ev: dict[str, Any], reasons: set[str]) -> list[str]:
    sev = ev.get("hallucination_severity")
    if sev in ("P0", "P1"):
        return [f"hallucination_severity:{sev}"]
    out: list[str] = []
    if "hallucination_P0" in reasons:
        out.append("hallucination_severity:P0")
    elif "hallucination_P1" in reasons:
        out.append("hallucination_severity:P1")
    return out


def _mode_tags(modes: Any) -> list[str]:
    out: list[str] = []
    for mode in modes or []:
        key = str(mode or "").strip()
        if key in HALLUCINATION_FAILURE_MODES:
            out.append(f"hallucination:{key}")
    return out


def _hal_turns_for_segment(
    ev: dict[str, Any], seg: dict[str, Any], allowed: set[int]
) -> set[int]:
    """Hallucination turns belonging to this speaker segment."""
    local = _turn_set(seg.get("hallucination_turns"))
    chat_hal = _turn_set(ev.get("hallucination_turns"))
    if local:
        hit = local & allowed if allowed else local
        if hit:
            return hit
        # Non-empty local disjoint from allowed — fall through to chat ∩ allowed.
    if chat_hal and allowed:
        return chat_hal & allowed
    return set()


def _hall_attr_tags_for_segment(
    ev: dict[str, Any],
    seg: dict[str, Any],
    allowed: set[int],
    *,
    soft: bool,
) -> list[str]:
    """Severity + mode attrs scoped to this speaker's hallucination turns.

    Prefer recompute via classify on intersecting turns so Bot/Human do not
    inherit each other's modes/severity. Without chat/GT, attach chat-level
    modes/severity only when this segment owns every chat hallucination turn.
    """
    turns = _hal_turns_for_segment(ev, seg, allowed)
    if not turns:
        return []

    tags: list[str] = []
    if soft:
        tags.append("hallucination_severity:soft")

    chat = ev.get("chat") if isinstance(ev.get("chat"), dict) else None
    artifact = (
        ev.get("eval_ground_truth")
        if isinstance(ev.get("eval_ground_truth"), dict)
        else None
    )
    if chat is not None and artifact is not None:
        from app.agents.responder_eval.hard_gates import (
            artifact_has_claim_evidence,
            resolve_hallucination_gate,
        )

        if artifact_has_claim_evidence(artifact):
            scoped: dict[str, Any] = {
                **ev,
                "hallucination_flagged": True,
                "hallucination_turns": sorted(turns),
            }
            rfc = ev.get("refund_fact_check")
            if isinstance(rfc, dict):
                seg_refund_turns = sorted(_turn_set(rfc.get("turns")) & set(turns))
                if seg_refund_turns:
                    scoped["refund_fact_check"] = {**rfc, "turns": seg_refund_turns}
                else:
                    scoped["refund_fact_check"] = {
                        "claim_kind": "none",
                        "severity": "none",
                        "turns": [],
                        "failure_mode": "none",
                    }
            sev, modes = resolve_hallucination_gate(chat, artifact, scoped)
            if not soft and sev in ("P0", "P1"):
                tags.append(f"hallucination_severity:{sev}")
            tags.extend(_mode_tags(modes))
            return tags
        # Sparse GT: fall through to stored chat modes when this segment owns
        # every chat hallucination turn (safe); otherwise parent-only.

    chat_hal = _turn_set(ev.get("hallucination_turns"))
    # Prefer exact ownership; else attach stored modes only for single-speaker
    # intersection when the chat list equals this segment's turns.
    if chat_hal and (chat_hal <= allowed or chat_hal == turns):
        if not soft:
            tags.extend(_severity_tags(ev, _hard_gate_reason_set(ev)))
        local_modes = seg.get("hallucination_failure_modes")
        modes = (
            list(local_modes)
            if isinstance(local_modes, list) and local_modes
            else list(ev.get("hallucination_failure_modes") or [])
        )
        tags.extend(_mode_tags(modes))
    return tags


def _hard_gate_issues(ev: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    reasons = _hard_gate_reason_set(ev)
    if "pii_incident" in reasons:
        issues.append("hard_gate:pii")
        for t in ev.get("pii_incident_types") or []:
            key = str(t or "").strip()
            if key:
                issues.append(f"pii:{key}")
    if any(r.startswith("hallucination_") for r in reasons):
        # Unified parent with soft hallucination so charts have one top-level tag.
        issues.append("hallucination")
        issues.extend(_severity_tags(ev, reasons))
        issues.extend(_mode_tags(ev.get("hallucination_failure_modes")))
    return issues


def _resolution_issues(ev: dict[str, Any], *, lifecycle: str | None) -> list[str]:
    issues: list[str] = []
    resolution = str(ev.get("resolution") or "").lower()
    if resolution == "no":
        issues.append("resolution:no")
    elif resolution == "partial":
        issues.append("resolution:partial")
    else:
        return issues
    stage = str(lifecycle or ev.get("policy_lifecycle_stage") or "").strip()
    if stage in _LIFECYCLE_STAGES:
        issues.append(f"resolution_lifecycle:{stage}")
    elif stage:
        issues.append("resolution_lifecycle:unknown")
    return issues


def _traceability_issues(
    ev: dict[str, Any],
    *,
    allowed_turns: set[int] | None = None,
) -> list[str]:
    if ev.get("traceability_pass") is not False:
        return []
    issues = ["traceability_failure"]
    untr = _turn_set(ev.get("untraceable_turns"))
    # When scoping to a segment, only attach reason attrs if turns intersect
    # (or there are no untraceable turn indexes to scope with).
    if allowed_turns is not None and untr and not (untr & allowed_turns):
        return issues
    for reason in ev.get("traceability_failure_reasons") or []:
        key = str(reason or "").strip()
        if key in TRACEABILITY_REASONS:
            issues.append(f"traceability:{key}")
    return issues


def _segment_turns_implicated_in_hal(
    ev: dict[str, Any], seg: dict[str, Any], allowed: set[int]
) -> bool:
    """True when hallucination turn indexes intersect this segment's turns.

    Soft LLM flags without turns must not inherit chat-level hard severity.
    """
    if not allowed:
        return False
    hal_turns = _turn_set(seg.get("hallucination_turns")) or _turn_set(
        ev.get("hallucination_turns")
    )
    if not hal_turns:
        return False
    return bool(hal_turns & allowed)


def _soft_hallucination_tags(attr_tags: list[str]) -> list[str]:
    """Soft parent + severity/mode attrs (attr_tags already includes severity:soft)."""
    return ["hallucination", *attr_tags]


def _issues_from_rubric(ev: dict[str, Any], *, lifecycle: str | None = None) -> list[str]:
    """Composite (chat-level) rubric tags including hard gates."""
    issues = _hard_gate_issues(ev)
    # Soft parent requires turn evidence (parity with segment tags / scoring merge).
    if (
        ev.get("hallucination_flagged")
        and not _chat_has_hard_hal(ev)
        and _turn_set(ev.get("hallucination_turns"))
    ):
        soft_attrs = ["hallucination_severity:soft"]
        soft_attrs.extend(_mode_tags(ev.get("hallucination_failure_modes") or []))
        issues.extend(_soft_hallucination_tags(soft_attrs))
    issues.extend(_resolution_issues(ev, lifecycle=lifecycle))
    issues.extend(_traceability_issues(ev, allowed_turns=None))
    return issues


def composite_issues(ev: dict[str, Any]) -> list[str]:
    lifecycle = str(ev.get("policy_lifecycle_stage") or "").strip() or None
    issues = _issues_from_guardrails(ev.get("violations"), allowed_turns=None)
    issues.extend(_issues_from_policy(ev.get("policy_violations"), allowed_turns=None))
    issues.extend(_issues_from_rubric(ev, lifecycle=lifecycle))
    return _dedupe_policy_leaked_from_guardrails(issues)


def _segment_untraceable_turns(ev: dict[str, Any], seg: dict[str, Any], allowed: set[int]) -> set[int]:
    """Untraceable turns belonging to this speaker segment."""
    local = _turn_set(seg.get("untraceable_turns"))
    chat_untr = _turn_set(ev.get("untraceable_turns"))
    if local:
        hit = local & allowed if allowed else local
        if hit:
            return hit
        # Corrupt local disjoint from turn_indexes — fall back to chat ∩ allowed.
    if chat_untr and allowed:
        return chat_untr & allowed
    return set()


def _segment_score_disagreement_tag(seg: dict[str, Any], ev: dict[str, Any]) -> list[str]:
    det = seg.get("det_traceability_score", ev.get("det_traceability_score"))
    llm = seg.get("llm_traceability_score", ev.get("llm_traceability_score"))
    try:
        if det is not None and llm is not None:
            det_i, llm_i = int(det), int(llm)
            if (det_i >= 80 and llm_i < 80) or (llm_i >= 80 and det_i < 80):
                return ["traceability:score_disagreement"]
    except (TypeError, ValueError):
        pass
    return []


def _segment_trace_reason_tags(
    ev: dict[str, Any], seg: dict[str, Any], allowed: set[int]
) -> list[str]:
    """Reason attrs for a failing segment, scoped to that speaker's turns.

    Prefers recompute from chat+GT on segment untraceable turns so Bot/Human
    charts nest correctly without copying chat-wide reasons onto both speakers.
    Empty untraceable turns (LLM-score-only fails) still emit score_disagreement
    from segment det/llm scores when those disagree.
    Without chat/GT, falls back to stored chat reasons only when every chat
    untraceable turn belongs to this segment.
    """
    seg_untr = _segment_untraceable_turns(ev, seg, allowed)
    chat = ev.get("chat") if isinstance(ev.get("chat"), dict) else None
    artifact = (
        ev.get("eval_ground_truth")
        if isinstance(ev.get("eval_ground_truth"), dict)
        else None
    )
    if chat is not None and artifact is not None:
        scoped_ev = {
            **ev,
            "traceability_pass": False,
            "det_traceability_score": seg.get(
                "det_traceability_score", ev.get("det_traceability_score")
            ),
            "llm_traceability_score": seg.get(
                "llm_traceability_score", ev.get("llm_traceability_score")
            ),
        }
        if seg_untr:
            # Segment det/llm already injected — allow score_disagreement from them.
            reasons = compute_traceability_failure_reasons(
                chat,
                artifact,
                scoped_ev,
                untraceable_turns=sorted(seg_untr),
                include_score_disagreement=True,
            )
            return [f"traceability:{r}" for r in reasons if r in TRACEABILITY_REASONS]
        # LLM/score-only failure with no turns: parent already emitted; attrs = score gate.
        return _segment_score_disagreement_tag(seg, ev)

    chat_untr = _turn_set(ev.get("untraceable_turns"))
    if chat_untr and chat_untr <= allowed:
        out: list[str] = []
        for reason in ev.get("traceability_failure_reasons") or []:
            key = str(reason or "").strip()
            if key in TRACEABILITY_REASONS:
                out.append(f"traceability:{key}")
        return out
    return _segment_score_disagreement_tag(seg, ev)


def segment_scoped_issues(ev: dict[str, Any], segment_key: str) -> list[str]:
    """Segment tags: turn-scoped rules + hard gates only when implicated.

    Hard gates still force all graded segments to E in scoring. Issue tags stay
    turn-scoped for speaker-owned failures; unimplicated speakers get an
    ``hard_gate:inherited_*`` parent so Grade E charts explain chat-level
    inheritance. PII stays on composite only (not speaker-attributed).
    """
    segs = ev.get("segment_evals")
    if not isinstance(segs, dict):
        return []
    seg = segs.get(segment_key)
    if not isinstance(seg, dict) or not seg.get("graded"):
        return []

    allowed = _turn_set(seg.get("turn_indexes"))
    lifecycle = str(ev.get("policy_lifecycle_stage") or "").strip() or None
    issues = _issues_from_guardrails(ev.get("violations"), allowed_turns=allowed)
    issues.extend(_issues_from_policy(ev.get("policy_violations"), allowed_turns=allowed))

    # PII is a chat-level incident — surface on composite charts only so Bot/Human
    # panels do not imply both speakers leaked.

    if _chat_has_hard_hal(ev):
        # Parent + attrs only when this segment's turns participate — avoids
        # orphan/wrong-speaker hallucination from soft flags without turns.
        if _segment_turns_implicated_in_hal(ev, seg, allowed):
            issues.append("hallucination")
            issues.extend(
                _hall_attr_tags_for_segment(ev, seg, allowed, soft=False)
            )
        else:
            # Grade was forced to E by chat hard gate; explain inheritance.
            issues.append("hard_gate:inherited_hallucination")
    elif seg.get("hallucination_flagged"):
        # Soft must also be turn-implicated — empty soft flags on the wrong
        # speaker must not create a soft hallucination parent.
        if _segment_turns_implicated_in_hal(ev, seg, allowed):
            issues.extend(
                _soft_hallucination_tags(
                    _hall_attr_tags_for_segment(ev, seg, allowed, soft=True)
                )
            )

    if _chat_has_hard_pii(ev):
        # Speaker charts: explain Grade E without claiming this speaker leaked.
        issues.append("hard_gate:inherited_pii")

    issues.extend(_resolution_issues(seg, lifecycle=lifecycle))
    if seg.get("traceability_pass") is False:
        issues.append("traceability_failure")
        issues.extend(_segment_trace_reason_tags(ev, seg, allowed))

    if int(seg.get("guardrail_violations") or 0) > 0 and not any(
        t.startswith("guardrail:") for t in issues
    ):
        issues.append("guardrail_violations")

    return _dedupe_policy_leaked_from_guardrails(issues)



def ensure_segment_turn_indexes(
    ev: dict[str, Any],
    *,
    chat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill missing segment ``turn_indexes`` from chat roles (backfill / sparse JSON)."""
    out = dict(ev)
    chat_blob = chat if isinstance(chat, dict) else out.get("chat")
    if not isinstance(chat_blob, dict):
        return out
    segs = out.get("segment_evals")
    if not isinstance(segs, dict):
        return out
    indexes = segment_turn_indexes(chat_blob)
    segs = dict(segs)
    changed = False
    for key in (SEGMENT_BOT, SEGMENT_HUMAN_AGENT):
        seg = segs.get(key)
        if not isinstance(seg, dict):
            continue
        if _turn_set(seg.get("turn_indexes")):
            continue
        rebuilt = list(indexes.get(key) or [])
        if rebuilt:
            segs[key] = {**seg, "turn_indexes": rebuilt}
            changed = True
    if changed:
        out["segment_evals"] = segs
    return out


def eval_dict_for_issue_tags(
    eval_json: dict[str, Any] | None,
    *,
    letter_grade: str,
    bot_grade: str,
    human_grade: str,
    bot_score: int | None,
    human_score: int | None,
) -> dict[str, Any]:
    """Merge denormalized grade columns into rubric JSON for tag computation."""
    ev = ensure_segment_turn_indexes(dict(eval_json or {}))
    ev["letter_grade"] = letter_grade
    segs = ev.get("segment_evals")
    if not isinstance(segs, dict):
        segs = {}
        ev["segment_evals"] = segs

    def _inject(seg_key: str, grade: str, score: int | None) -> None:
        if grade == LETTER_GRADE_NOT_GRADED and score is None:
            return
        seg = segs.get(seg_key)
        if not isinstance(seg, dict):
            seg = {}
            segs[seg_key] = seg
        if grade != LETTER_GRADE_NOT_GRADED:
            seg["letter_grade"] = grade
        if score is not None:
            seg["composite_score"] = score
            seg["graded"] = True

    _inject(SEGMENT_BOT, bot_grade, bot_score)
    _inject(SEGMENT_HUMAN_AGENT, human_grade, human_score)
    return ev


def enrich_eval_attr_fields(
    ev: dict[str, Any],
    *,
    chat: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fill hallucination modes / traceability reasons (write + backfill).

    When ``force`` is True (backfill), recompute even if fields already exist so
    stale values from older classifier bugs are corrected.
    """
    out = dict(ev)
    chat_blob = chat if isinstance(chat, dict) else out.get("chat")
    art_blob = artifact if isinstance(artifact, dict) else out.get("eval_ground_truth")
    if isinstance(chat_blob, dict):
        out = ensure_segment_turn_indexes(out, chat=chat_blob)

    reasons = _hard_gate_reason_set(out)
    want_modes = any(r.startswith("hallucination_") for r in reasons) or out.get(
        "hallucination_flagged"
    )
    have_modes = bool(out.get("hallucination_failure_modes") or [])
    want_trace = out.get("traceability_pass") is False
    have_trace = bool(out.get("traceability_failure_reasons") or [])
    from app.agents.responder_eval.hard_gates import (
        artifact_has_claim_evidence,
        chat_has_messages,
        resolve_hallucination_gate,
    )

    chat_ok = chat_has_messages(chat_blob if isinstance(chat_blob, dict) else None)
    art_dict = art_blob if isinstance(art_blob, dict) else None
    # Missing chat or missing artifact blob ⇒ cannot safely recompute.
    if not chat_ok or art_dict is None:
        out["_attr_enrich_skipped"] = True
        out["_attr_enrich_skip_reason"] = "missing_chat_or_ground_truth"
        return out

    gt_ok = artifact_has_claim_evidence(art_dict)

    def _reconcile_hard_from_sev(sev: str | None, modes: list[str]) -> None:
        out["hallucination_failure_modes"] = modes
        reconciled = [
            r
            for r in (out.get("hard_gate_reasons") or [])
            if not str(r).startswith("hallucination_")
        ]
        if sev in ("P0", "P1"):
            reconciled.append(f"hallucination_{sev}")
            out["hallucination_severity"] = sev
        else:
            out["hallucination_severity"] = None
        out["hard_gate_reasons"] = reconciled
        out["hard_gate"] = bool(reconciled)
        # Grades/scores are not rewritten here — validate warns on E↔gate drift.

    if want_modes and (force or not have_modes):
        if gt_ok:
            _sev, modes = resolve_hallucination_gate(chat_blob, art_dict, out)
            _reconcile_hard_from_sev(_sev, modes)
        elif force:
            # Sparse/empty GT: do NOT invent and do NOT strip existing hard reasons
            # (would leave Grade E with soft/empty tags while validate stays green).
            out["_attr_enrich_partial"] = "sparse_ground_truth_no_reclassify"
        # Live path without GT and without modes: leave as-is (no invent).
    elif force and not want_modes:
        out["hallucination_failure_modes"] = []
        out["hallucination_severity"] = None

    if want_trace and (force or not have_trace):
        if gt_ok:
            from app.agents.responder_eval.traceability_reasons import (
                compute_traceability_failure_reasons,
            )

            out["traceability_failure_reasons"] = compute_traceability_failure_reasons(
                chat_blob, art_dict, out
            )
        elif force:
            out["_attr_enrich_partial"] = out.get("_attr_enrich_partial") or (
                "sparse_ground_truth_no_trace_reclassify"
            )
            # Keep existing trace reasons; do not invent or wipe without GT.
    elif force and not want_trace:
        out["traceability_failure_reasons"] = []

    return out


def compute_issue_tags(eval_result: dict[str, Any]) -> dict[str, list[str]]:
    """Return denormalized issue arrays for persist / backfill."""
    eval_result = ensure_segment_turn_indexes(eval_result)
    letter = str(eval_result.get("letter_grade") or "-")
    segments = segment_row_fields(eval_result)
    bot_grade = str(segments.get("bot_grade") or "-")
    human_grade = str(segments.get("human_grade") or "-")
    bot_score = segments.get("bot_score")
    human_score = segments.get("human_score")

    composite = sorted(set(composite_issues(eval_result))) if letter in _LOW_GRADES else []
    bot = (
        sorted(set(segment_scoped_issues(eval_result, SEGMENT_BOT)))
        if bot_grade in _LOW_GRADES and bot_score is not None
        else []
    )
    human = (
        sorted(set(segment_scoped_issues(eval_result, SEGMENT_HUMAN_AGENT)))
        if human_grade in _LOW_GRADES and human_score is not None
        else []
    )
    return {
        "composite_issues": composite,
        "bot_issues": bot,
        "human_issues": human,
    }
