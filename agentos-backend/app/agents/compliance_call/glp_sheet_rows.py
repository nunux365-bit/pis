"""README §10.1 Rollup (49 cols) and §10.2 Detailed (93 cols) as flat value rows for Sheets/CSV."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Rubric checklist order = README §10.1 sub-metrics cols 21–48.
ROLLUP_SCORED_ITEM_IDS: tuple[str, ...] = (
    "mtc_men2_family_hx",
    "pancreatitis_hx",
    "insulin_su_meds",
    "pregnancy_contraception",
    "gallstones_hx",
    "bmi_weight_target",
    "comorbidities",
    "ckd_renal",
    "prior_wl_attempts",
    "thyroid_hx_own",
    "eating_patterns",
    "mental_health",
    "physical_activity",
    "alcohol_intake",
    "sleep_osa",
    "dose_escalation",
    "common_aes",
    "serious_aes",
    "intolerance_pause",
    "mechanism_of_action",
    "protein_target",
    "long_term_diet",
    "foods_to_avoid",
    "hydration",
    "lab_baseline",
    "followup_schedule",
    "escalation_path",
    "metrics_to_track",
)

BONUS_ITEM_IDS: frozenset[str] = frozenset(
    {
        "drug_choice_rationale",
        "storage_and_injection",
        "fatty_liver_nash",
        "cost_and_adherence",
        "injection_anxiety",
        "realistic_expectations",
        "plateaus",
        "patient_goals",
    }
)


def bonus_ids_in_rubric_order(rubric: dict[str, Any]) -> list[str]:
    scoring = rubric.get("scoring") or {}
    out: list[str] = []
    for it in (scoring.get("bonus_counseling_quality") or {}).get("items") or []:
        if isinstance(it, dict):
            iid = str(it.get("id") or "")
            if iid in BONUS_ITEM_IDS:
                out.append(iid)
    return out

DOMAIN_KEYS_ORDER: tuple[str, ...] = (
    "safety_screening",
    "clinical_assessment",
    "psychosocial_lifestyle",
    "drug_education_informed_consent",
    "nutritional_counseling",
    "followup_planning_monitoring",
)


def _structural_ratio_cell(st: dict[str, Any]) -> str:
    """Prefer inferred clinician:patient %; else Deepgram S0 vs others."""
    v = st.get("clinician_patient_talk_ratio")
    if v is None or v == "":
        v = st.get("diarized_s0_to_others_ratio")
    if v is None or v == "":
        v = st.get("doctor_to_patient_ratio")
    if v is None or v == "":
        return "NA"
    return str(v)


def glp_rollup_headers() -> tuple[str, ...]:
    """49 column titles aligned with README §10.1 (conversation_id: ``second_opinion_conversations.id`` for MySQL ingest, else blank)."""
    h = [
        "conversation_id",
        "Doctor",
        "Patient",
        "Date Scored",
        "Call Timestamp (UTC)",
        "Duration (min)",
        "Total Turns",
        "Avg Turn (s)",
        "Longest Turn (s)",
        "Speech Activity (%)",
        "Clinician:patient %",
        "Safety (%)",
        "Clinical (%)",
        "Psychosoc (%)",
        "Drug Ed (%)",
        "Nutrition (%)",
        "Follow-up (%)",
        "Composite (%)",
        "Grade",
        "Status",
    ]
    labels = (
        "Safety: MTC/MEN2",
        "Safety: Pancreatitis hx",
        "Safety: Insulin/SU",
        "Safety: Pregnancy",
        "Safety: Gallstones",
        "Clin: BMI/wt/target",
        "Clin: Comorbidities",
        "Clin: Renal/eGFR",
        "Clin: Prior WL attempts",
        "Clin: Thyroid hx",
        "Psych: Eating patterns",
        "Psych: Mental health",
        "Psych: Activity",
        "Psych: Alcohol",
        "Psych: Sleep/OSA",
        "DrugEd: Dose escalation",
        "DrugEd: Common AEs",
        "DrugEd: Serious AEs",
        "DrugEd: Intolerance",
        "DrugEd: Mechanism",
        "Nutri: Protein target",
        "Nutri: LT diet strat",
        "Nutri: Foods to avoid",
        "Nutri: Hydration",
        "FollowUp: Lab baseline",
        "FollowUp: Schedule",
        "FollowUp: Escalation",
        "FollowUp: Metrics",
    )
    for lab in labels:
        h.append(lab)
    h.append("Comments")
    return tuple(h)


def _item_maps(eval_doc: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """item_id -> status, item_id -> evidence text."""
    by_status: dict[str, str] = {}
    by_ev: dict[str, str] = {}
    for key in ("scored_items", "items"):
        rows = eval_doc.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            iid = str(row.get("item_id") or "").strip()
            if not iid:
                continue
            st = str(row.get("status") or "NO").strip().upper()
            if st not in ("YES", "NO", "NA"):
                st = "NO"
            ev = row.get("evidence")
            if ev is None:
                ev = row.get("rationale")
            by_status[iid] = st
            by_ev[iid] = str(ev or "")
    for row in eval_doc.get("bonus_counseling_items") or []:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("item_id") or "").strip()
        if not iid:
            continue
        st = str(row.get("status") or "NO").strip().upper()
        if st not in ("YES", "NO", "NA"):
            st = "NO"
        ev = row.get("evidence")
        if ev is None:
            ev = row.get("rationale")
        by_status[iid] = st
        by_ev[iid] = str(ev or "")
    return by_status, by_ev


def _fmt_ts_utc(iso_z: str | None) -> str:
    if not iso_z:
        return ""
    s = iso_z.strip()
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso_z


def build_glp_rollup_row(
    *,
    serial_no: str,
    doctor_name: str,
    patient_summary: str,
    date_scored: str,
    call_timestamp_utc: str,
    structural: dict[str, Any],
    domain_pcts: dict[str, Any],
    composite_pct: Any,
    grade: str,
    grade_label: str,
    status_text: str,
    eval_doc: dict[str, Any],
    comments: str,
) -> list[str]:
    """49 string cells (materialized composite/grade; Status is plain text, not an Excel formula)."""
    st = structural if isinstance(structural, dict) else {}
    dp = domain_pcts if isinstance(domain_pcts, dict) else {}
    by_s, _by_e = _item_maps(eval_doc)

    dr_ratio = _structural_ratio_cell(st)

    row: list[str] = [
        serial_no,
        doctor_name,
        patient_summary,
        date_scored.partition("T")[0] if date_scored else "",
        call_timestamp_utc,
        str(st.get("duration_minutes", "")),
        str(st.get("total_speaking_turns", "")),
        str(st.get("avg_turn_length_seconds", "")),
        str(st.get("longest_turn_seconds", "")),
        str(st.get("speech_activity_pct", "")),
        dr_ratio,
    ]
    for dk in DOMAIN_KEYS_ORDER:
        v = dp.get(dk)
        row.append("" if v is None else str(v))
    row.append("" if composite_pct is None else str(composite_pct))
    row.append(grade or "")
    row.append(status_text or grade_label or "")
    for iid in ROLLUP_SCORED_ITEM_IDS:
        row.append(by_s.get(iid, ""))
    row.append(comments or str(eval_doc.get("comments") or ""))
    if len(row) != 49:
        raise RuntimeError(f"glp rollup row must be 49 cols, got {len(row)}")
    return row


def _find_scored_item(rubric: dict[str, Any], item_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    scoring = rubric.get("scoring") or {}
    for dom in scoring.get("domains") or []:
        if not isinstance(dom, dict):
            continue
        for it in dom.get("checklist") or []:
            if isinstance(it, dict) and str(it.get("id")) == item_id:
                return dom, it
    return None, None


def _find_bonus_item(rubric: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    scoring = rubric.get("scoring") or {}
    for it in (scoring.get("bonus_counseling_quality") or {}).get("items") or []:
        if isinstance(it, dict) and str(it.get("id")) == item_id:
            return it
    return None


def glp_detailed_headers(rubric: dict[str, Any]) -> tuple[str, ...]:
    """93 headers: prefix 20 + per-item Status/Evidence + bonus Status/Note + Comments."""
    prefix = glp_rollup_headers()[:20]
    headers: list[str] = list(prefix)
    short_dom = {
        "safety_screening": "Safety",
        "clinical_assessment": "Clin",
        "psychosocial_lifestyle": "Psych",
        "drug_education_informed_consent": "DrugEd",
        "nutritional_counseling": "Nutri",
        "followup_planning_monitoring": "FollowUp",
    }
    for iid in ROLLUP_SCORED_ITEM_IDS:
        dom, it = _find_scored_item(rubric, iid)
        if not it:
            lab = iid
            sw = "[?=?]"
            pre = "?"
        else:
            did = str((dom or {}).get("id") or "")
            pre = short_dom.get(did, did)
            lab = str(it.get("item") or iid).replace("\n", " ")
            tw = it.get("tier_weight")
            tier = str(it.get("tier") or "")
            sw = f"[{tier}={tw}]" if tw is not None else f"[{tier}]"
        headers.append(f"{pre}: {lab} — Status {sw}")
        headers.append(f"{pre}: {lab} — Evidence")
    for iid in bonus_ids_in_rubric_order(rubric):
        it = _find_bonus_item(rubric, iid)
        nm = str((it or {}).get("name") or iid).replace("\n", " ")
        headers.append(f"Bonus: {nm} — Status [BONUS]")
        headers.append(f"Bonus: {nm} — Note")
    headers.append("Comments")
    if len(headers) != 93:
        raise RuntimeError(f"glp detailed headers must be 93 cols, got {len(headers)}")
    return tuple(headers)


def build_glp_detailed_row(
    *,
    rubric: dict[str, Any],
    serial_no: str,
    doctor_name: str,
    patient_summary: str,
    date_scored: str,
    call_timestamp_utc: str,
    structural: dict[str, Any],
    domain_pcts: dict[str, Any],
    composite_pct: Any,
    grade: str,
    grade_label: str,
    status_text: str,
    eval_doc: dict[str, Any],
    comments: str,
) -> list[str]:
    """93 cells: README §10.2."""
    st = structural if isinstance(structural, dict) else {}
    dp = domain_pcts if isinstance(domain_pcts, dict) else {}
    by_s, by_e = _item_maps(eval_doc)

    dr_ratio = _structural_ratio_cell(st)

    row: list[str] = [
        serial_no,
        doctor_name,
        patient_summary,
        date_scored.partition("T")[0] if date_scored else "",
        call_timestamp_utc,
        str(st.get("duration_minutes", "")),
        str(st.get("total_speaking_turns", "")),
        str(st.get("avg_turn_length_seconds", "")),
        str(st.get("longest_turn_seconds", "")),
        str(st.get("speech_activity_pct", "")),
        dr_ratio,
    ]
    for dk in DOMAIN_KEYS_ORDER:
        v = dp.get(dk)
        row.append("" if v is None else str(v))
    row.append("" if composite_pct is None else str(composite_pct))
    row.append(grade or "")
    row.append(status_text or grade_label or "")

    for iid in ROLLUP_SCORED_ITEM_IDS:
        row.append(by_s.get(iid, ""))
        row.append(by_e.get(iid, ""))
    for iid in bonus_ids_in_rubric_order(rubric):
        row.append(by_s.get(iid, ""))
        row.append(by_e.get(iid, ""))
    row.append(comments or str(eval_doc.get("comments") or ""))
    if len(row) != 93:
        raise RuntimeError(f"glp detailed row must be 93 cols, got {len(row)}")
    return row
