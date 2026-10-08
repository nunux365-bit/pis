"""Deterministic GLP v1.1 math + structural metrics from Deepgram (README §6 / §8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Bundled authority (same as Downloads/glp/glp1_consult_rubric.json).
_DEFAULT_RUBRIC = Path(__file__).resolve().parent / "rubrics" / "glp1_consult_rubric.json"


def load_glp_rubric(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT_RUBRIC
    if not p.is_file():
        raise FileNotFoundError(f"GLP rubric not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def iter_scored_checklist(rubric: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    """Yield (domain_id, domain_weight_pct, item_dict) for each scored checklist item."""
    scoring = rubric.get("scoring") or {}
    for dom in scoring.get("domains") or []:
        dom_id = str(dom.get("id") or "")
        w = float(dom.get("weight_pct") or 0)
        for it in dom.get("checklist") or []:
            if isinstance(it, dict) and it.get("id"):
                yield dom_id, w, it


def scored_item_ids(rubric: dict[str, Any]) -> list[str]:
    return [str(it.get("id")) for _, _, it in iter_scored_checklist(rubric) if it.get("id")]


def bonus_item_ids(rubric: dict[str, Any]) -> list[str]:
    scoring = rubric.get("scoring") or {}
    bonus = (scoring.get("bonus_counseling_quality") or {}).get("items") or []
    return [str(x.get("id")) for x in bonus if isinstance(x, dict) and x.get("id")]


def domain_pct_for_statuses(
    rubric: dict[str, Any],
    status_by_id: dict[str, str],
) -> dict[str, float]:
    """Per-domain % from tier weights; NA excluded from numerator and denominator."""
    tiers = (rubric.get("math") or {}).get("tier_weights") or {"MUST": 10, "SHOULD": 6, "NICE": 2}
    accum: dict[str, tuple[float, float]] = {}
    for dom_id, _dw, it in iter_scored_checklist(rubric):
        iid = str(it.get("id") or "")
        tw = float(it.get("tier_weight") or tiers.get(str(it.get("tier") or ""), 0) or 0)
        if tw <= 0:
            continue
        st = str(status_by_id.get(iid) or "NO").strip().upper()
        if st not in ("YES", "NO", "NA"):
            st = "NO"
        num, den = accum.get(dom_id, (0.0, 0.0))
        if st == "NA":
            accum[dom_id] = (num, den)
            continue
        den += tw
        if st == "YES":
            num += tw
        accum[dom_id] = (num, den)

    out: dict[str, float] = {}
    for dom_id, (num, den) in accum.items():
        pct = round(100.0 * num / den, 1) if den > 0 else 0.0
        out[dom_id] = pct
    return out


def composite_pct_from_domain_pcts(rubric: dict[str, Any], domain_pcts: dict[str, float]) -> float:
    """README: composite = sum(domain_pct * cross_domain_weight_pct) / 100, 1 dp."""
    total = 0.0
    scoring = rubric.get("scoring") or {}
    for dom in scoring.get("domains") or []:
        did = str(dom.get("id") or "")
        w = float(dom.get("weight_pct") or 0)
        dp = float(domain_pcts.get(did) or 0.0)
        total += dp * w / 100.0
    return round(total, 1)


def grade_from_composite(rubric: dict[str, Any], composite_pct: float) -> tuple[str, str]:
    """Returns (letter_grade, label) e.g. ('D', 'Below Standard')."""
    bands = rubric.get("grading_bands") or []
    x = float(composite_pct)
    for b in bands:
        if not isinstance(b, dict):
            continue
        lo = float(b.get("min", 0))
        hi = float(b.get("max", 100))
        if lo <= x <= hi + 1e-6:
            return str(b.get("grade") or "F"), str(b.get("label") or "")
    return "F", ""


def apply_deterministic_scores(
    rubric: dict[str, Any],
    status_by_id: dict[str, str],
) -> dict[str, Any]:
    """Build eval fragment: domain_pcts, composite_pct, grade, grade_label."""
    dmap = domain_pct_for_statuses(rubric, status_by_id)
    comp = composite_pct_from_domain_pcts(rubric, dmap)
    letter, label = grade_from_composite(rubric, comp)
    meta = rubric.get("meta") or {}
    return {
        "rubric_authority_version": str(meta.get("version") or ""),
        "domain_pcts": dmap,
        "composite_pct": comp,
        "grade": letter,
        "grade_label": label,
    }


def asr_word_quality_from_words(words: list[Any]) -> dict[str, Any]:
    """Aggregate confidence from Deepgram ``words`` (channel alternative)."""
    confs: list[float] = []
    low = 0
    for w in words:
        if not isinstance(w, dict):
            continue
        c = float(w.get("confidence") or 0.0)
        confs.append(c)
        if c < 0.5:
            low += 1
    n = len(confs)
    if n == 0:
        return {}
    return {
        "mean_word_confidence": round(sum(confs) / n, 4),
        "pct_words_below_0_5": round(100.0 * low / n, 2),
        "word_count": n,
    }


def speaker_seconds_from_payload(payload: dict[str, Any]) -> dict[int, float]:
    """Aggregate utterance duration per Deepgram ``speaker`` id."""
    ut = (payload.get("results") or {}).get("utterances") or []
    sp: dict[int, float] = {}
    if not isinstance(ut, list):
        return sp
    for u in ut:
        if not isinstance(u, dict):
            continue
        sid = int(u.get("speaker", 0) or 0)
        dur = max(0.0, float(u.get("end", 0.0)) - float(u.get("start", 0.0)))
        sp[sid] = sp.get(sid, 0.0) + dur
    return sp


def clinician_patient_talk_ratio_string(
    speaker_seconds: dict[int, float],
    clinician_id: int,
    patient_id: int,
) -> str:
    """Human-readable split of talk-time between inferred clinician vs patient (two speakers only)."""
    c = float(speaker_seconds.get(clinician_id, 0.0))
    p = float(speaker_seconds.get(patient_id, 0.0))
    tot = c + p
    if tot <= 0:
        return ""
    cp = round(100.0 * c / tot, 1)
    pp = round(100.0 * p / tot, 1)
    return f"{cp}:{pp}"


def merge_clinician_patient_into_structural(
    structural: dict[str, Any],
    speaker_seconds: dict[int, float],
    roles: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach inferred clinician/patient labels and ``clinician_patient_talk_ratio`` when roles present."""
    out = dict(structural)
    if not roles:
        return out
    raw_clin = roles.get("clinician_speaker")
    raw_pat = roles.get("patient_speaker")
    if raw_clin is None or raw_pat is None:
        return out
    try:
        ci = int(raw_clin)
        pi = int(raw_pat)
    except (TypeError, ValueError):
        return out
    ratio = clinician_patient_talk_ratio_string(speaker_seconds, ci, pi)
    if ratio:
        out["clinician_patient_talk_ratio"] = ratio
        out["clinician_speaker_id"] = ci
        out["patient_speaker_id"] = pi
    return out


def structural_from_deepgram(payload: dict[str, Any], *, diarize: bool) -> dict[str, Any]:
    """Structural block per rubric ``structural_indicators`` (advisory; does not change composite)."""
    meta = payload.get("metadata") or {}
    duration = float(meta.get("duration") or 0.0)
    utterances = (payload.get("results") or {}).get("utterances") or []
    if not isinstance(utterances, list):
        utterances = []

    # Cluster utterances into turns: gap > 1.0s between consecutive utterance boundaries.
    turns: list[list[dict[str, Any]]] = []
    if utterances:
        cur: list[dict[str, Any]] = [utterances[0]]
        for u in utterances[1:]:
            if not isinstance(u, dict):
                continue
            gap = float(u.get("start", 0.0)) - float((cur[-1] or {}).get("end", 0.0))
            if gap > 1.0:
                turns.append(cur)
                cur = [u]
            else:
                cur.append(u)
        turns.append(cur)

    turn_lens: list[float] = []
    for t in turns:
        if not t:
            continue
        t0 = float((t[0] or {}).get("start", 0.0))
        t1 = float((t[-1] or {}).get("end", 0.0))
        turn_lens.append(max(0.0, t1 - t0))

    speech_total = 0.0
    for u in utterances:
        if isinstance(u, dict):
            speech_total += max(0.0, float(u.get("end", 0.0)) - float(u.get("start", 0.0)))

    n_turns = len(turn_lens)
    avg_turn = sum(turn_lens) / n_turns if n_turns else 0.0
    longest = max(turn_lens) if turn_lens else 0.0
    speech_pct = round(100.0 * speech_total / duration, 1) if duration > 0 else 0.0

    doctor_ratio: str | float = "NA"
    if diarize and utterances:
        sp: dict[int, float] = {}
        for u in utterances:
            if not isinstance(u, dict):
                continue
            sp_id = int(u.get("speaker", 0) or 0)
            sp[sp_id] = sp.get(sp_id, 0.0) + max(
                0.0, float(u.get("end", 0.0)) - float(u.get("start", 0.0))
            )
        if len(sp) >= 2:
            tot = sum(sp.values()) or 1.0
            keys_sorted = sorted(sp.keys())
            d0 = sp[keys_sorted[0]] / tot
            doctor_ratio = round(d0 / max(1e-6, (1.0 - d0)), 2) if len(keys_sorted) >= 2 else "NA"

    role_note = (
        "Speaker IDs are Deepgram clusters (often S0 vs S1); not verified as clinician vs patient."
        if diarize and utterances
        else "Enable diarization for speaker-relative metrics."
    )

    return {
        "duration_minutes": round(duration / 60.0, 2),
        "total_speaking_turns": n_turns,
        "avg_turn_length_seconds": round(avg_turn, 1),
        "longest_turn_seconds": round(longest, 1),
        "speech_activity_pct": speech_pct,
        # Ratio of Deepgram speaker label S0 talk-time vs all other labels (not clinician/patient).
        "diarized_s0_to_others_ratio": doctor_ratio,
        "doctor_to_patient_ratio": doctor_ratio,
        "speaker_role_note": role_note,
        "deepgram_request_id": meta.get("request_id"),
    }


def deepgram_summary_for_persist(
    payload: dict[str, Any],
    structural: dict[str, Any],
    *,
    asr_word_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Small JSON-safe blob for workflow_runs.output_data (not full vendor JSON)."""
    meta = payload.get("metadata") or {}
    created = meta.get("created")
    out: dict[str, Any] = {
        "duration_sec": round(float(meta.get("duration") or 0.0), 3),
        "request_id": meta.get("request_id"),
        "deepgram_created": created if isinstance(created, str) else str(created or ""),
        "structural": structural,
    }
    if asr_word_quality:
        out["asr_word_quality"] = asr_word_quality
    return out


# README §13 — domain rows published for acceptance (composite is derived in tests).
README_CALIBRATION_DOMAIN_PCTS: list[dict[str, float]] = [
    {
        "safety_screening": 83.3,
        "clinical_assessment": 23.8,
        "psychosocial_lifestyle": 0.0,
        "drug_education_informed_consent": 23.8,
        "nutritional_counseling": 0.0,
        "followup_planning_monitoring": 55.6,
    },
    {
        "safety_screening": 78.3,
        "clinical_assessment": 23.8,
        "psychosocial_lifestyle": 17.6,
        "drug_education_informed_consent": 23.8,
        "nutritional_counseling": 35.7,
        "followup_planning_monitoring": 27.8,
    },
    {
        "safety_screening": 55.6,
        "clinical_assessment": 0.0,
        "psychosocial_lifestyle": 0.0,
        "drug_education_informed_consent": 23.8,
        "nutritional_counseling": 0.0,
        "followup_planning_monitoring": 27.8,
    },
    {
        "safety_screening": 43.5,
        "clinical_assessment": 23.8,
        "psychosocial_lifestyle": 0.0,
        "drug_education_informed_consent": 47.6,
        "nutritional_counseling": 0.0,
        "followup_planning_monitoring": 0.0,
    },
    {
        "safety_screening": 55.6,
        "clinical_assessment": 38.1,
        "psychosocial_lifestyle": 17.6,
        "drug_education_informed_consent": 23.8,
        "nutritional_counseling": 0.0,
        "followup_planning_monitoring": 27.8,
    },
    {
        "safety_screening": 0.0,
        "clinical_assessment": 47.6,
        "psychosocial_lifestyle": 0.0,
        "drug_education_informed_consent": 23.8,
        "nutritional_counseling": 0.0,
        "followup_planning_monitoring": 27.8,
    },
]

README_CALIBRATION_COMPOSITES: list[tuple[float, str]] = [
    (35.9, "D"),
    (38.1, "D"),
    (21.4, "F"),
    (25.2, "D+"),
    (31.7, "D+"),
    (17.1, "F"),
]
