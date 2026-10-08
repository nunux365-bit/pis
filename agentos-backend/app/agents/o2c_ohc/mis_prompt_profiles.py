"""
MIS Summary LLM prompts composed by **billing_profile** (mirrors contract ``llm_extract`` split).

**Profiles:** ``tcs`` | ``taco`` | ``generic`` (see ``contract_terms_version.billing_profile`` — enum values only).

Authoritative **full user** prompt bundles on disk (under ``app/agents/o2c_ohc/``):

| ``billing_profile`` | Module | Bundle file |
|---------------------|--------|-------------|
| ``tcs`` | ``mis_prompt_tcs_v30`` | ``_mis_tcs_worldclass_from_code_prompt.txt`` (deterministic MIS engine) |
| ``taco`` | ``mis_prompt_taco_from_code`` | ``_mis_taco_worldclass_from_code_prompt.txt`` (deterministic MIS engine) |
| ``generic`` | ``mis_prompt_generic_from_code`` | ``_mis_generic_worldclass_slim_prompt.txt`` |

Non-``generic`` profiles use **deterministic** single-file ``from_code`` templates. ``generic`` uses the **worldclass slim**
bundle (§0–§2.5 envelope, ``{prompt_bundle_version}``, generic profile + shared technical tail).

See ``MIS_USER_PROMPT_BUNDLE_BY_PROFILE`` for the same mapping in code.

**Regeneration impact:** Prompt version changes affect **new** MIS drafts only. Rows already stored in
``o2c_mis_summary_row`` / ``summary_json`` are unchanged until a site/period is **re-run**. All three on-disk user bundles share the same **``MAP-STAFF-CAL``** / calendar-proxy **``C_SHIFT_MISS``** rule: **T** = Context
**calendar_days** only; fixed per-line SC defaults per DEFINITIONS. Visit/session and ``as_per_actuals`` rows use
other classes.

**Mental model (maintainers):** see ``MIS_ATTENDANCE_BILLING_MENTAL_MODEL.md`` in this package — high-frequency vs
visit-based default, profile table, explicit overrides, per-visit amount discipline.

**Profile blocks** (``mis_profile_prompt_block``): non-generic profiles use inline strings for reference and tests;
**generic** is sliced from ``_mis_generic_worldclass_slim_prompt.txt`` (see ``mis_generic_profile_block_for_prompt_profiles``).
"""

from __future__ import annotations

from pathlib import Path

from app.agents.o2c_ohc.billing_profile import (
    BILLING_PROFILE_GENERIC,
    BILLING_PROFILE_TACO,
    BILLING_PROFILE_TCS,
    normalize_billing_profile,
)
from app.agents.o2c_ohc.mis_prompt_taco_from_code import (
    MIS_PROMPT_TACO_POLICY_VERSION,
    build_mis_taco_worldclass_user_prompt,
)
from app.agents.o2c_ohc.mis_prompt_tcs_v30 import (
    MIS_PROMPT_TCS_POLICY_VERSION,
    build_mis_tcs_v30_user_prompt,
)
from app.agents.o2c_ohc.mis_prompt_generic_from_code import (
    MIS_PROMPT_GENERIC_POLICY_VERSION,
    build_mis_generic_worldclass_user_prompt,
    mis_generic_profile_block_for_prompt_profiles,
)

_MIS_PKG_DIR = Path(__file__).resolve().parent

# Filenames for the three MIS user-message bundles (worldclass from_code ×2 + generic slim).
MIS_USER_PROMPT_BUNDLE_BY_PROFILE: dict[str, str] = {
    BILLING_PROFILE_TCS: "_mis_tcs_worldclass_from_code_prompt.txt",
    BILLING_PROFILE_TACO: "_mis_taco_worldclass_from_code_prompt.txt",
    BILLING_PROFILE_GENERIC: "_mis_generic_worldclass_slim_prompt.txt",
}


def mis_user_prompt_bundle_path(billing_profile: str | None) -> Path:
    """Absolute path to the on-disk user prompt template for ``billing_profile``."""
    bp = normalize_billing_profile(billing_profile)
    name = MIS_USER_PROMPT_BUNDLE_BY_PROFILE[bp]
    return _MIS_PKG_DIR / name


# Legacy label before bundled generic worldclass prompts; prefer ``MIS_PROMPT_GENERIC_POLICY_VERSION``.
MIS_PROMPT_POLICY_VERSION = "v2.35"

# --- Profile-specific bodies (disjoint; one billing_profile value per contract version) ---

_MIS_BLOCK_TCS = """
**Profile-specific rules (``billing_profile=tcs`` — site-scoped IT OHC annexures):**
- **Site-scoped rates:** Each ``rate_line`` applies to **this site’s** MIS only. Do **not** apply another site’s tier or annexure rate to this attendance.
- **Type 3 shared fees:** When the contract encodes **K** parallel lines (one INR amount split across K sites), emit **K** rows with the given ``contract_rate_line_id`` values; do not invent allocation.
- **Physician routing (FMO vs MO):**
  - Staffing rows must use ``employee_external_id`` from ``attendance_records``. Align ``FMO_*`` / ``MO_*`` attendance ``role_code`` with the same prefix on ``rate_lines.role_code`` when present.
  - Do **not** map ``FMO_*`` persons onto ``MO_*`` contract lines unless ``billing_rule_text`` explicitly allows it.
  - If ``mis_staffing_contract_gap`` = ``fmo_labeled_attendance_no_fmo_contract_line`` → ``validation.status=needs_human_review``; do not substitute an MO unit rate for an FMO seat.
  - Use ``mis_staffing_slot_hint`` / ``mis_staffing_fmo_role_code`` only to break ties when the contract lists both FMO and MO lines.
  - **One employee → one staffing row:** each ``employee_external_id`` at most one non-omitted staffing ``summary_rows`` line per site/period (no duplicate ID across two physician ``contract_rate_line_id`` values); **not** a cap of one person **per** line — with ``contracted_quantity`` = N, bill **top N** distinct qualified people when M ≥ N exist on the roll.
  - Respect ``contracted_quantity`` for caps.
- **Per-line service charge:** When ``rate_lines`` show ``service_charge_type`` other than ``none``, include SC in ``final_amount`` per contract and show **base** vs **SC** in ``calc_notes``.
- **``OHC_ADMIN_INVOICE_PCT``:** If present, exactly **one** non-employee row for that ``contract_rate_line_id``; ``final_amount`` = (sum of **non-omitted** staffing ``final_amount`` × ``invoice_admin_pct`` / 100). Staffing rows that are **base-only** must **not** include this global percentage inside each line.
- **Low- vs high-frequency:** True **C_VISIT_SESSION** needs **rate_unit=visit** (or explicit each-visit rupee) + small **V** as **fee cap**; **per month** / **rate_unit=month** on **per_visit** + duty JSON → **``MONTHLY_RETAINER_INGEST``** even when **V** is 4/8/20 (prod-shaped physician rows). **≥ ~5** days/week + **rate_attendance** + MAP fingerprint → **MAP-STAFF-CAL**; **per_visit** + monthly seat + duty JSON → **``MONTHLY_RETAINER_INGEST``** — **not** **rate_amount × V** from duty JSON when monthly text governs. **``MONTHLY_RETAINER_INGEST``** has **two amount modes** (full bundle): **Mode 1** flat **rate_amount**; **Mode 2** same **``MAP-STAFF-CAL``**-numeric **``prorated_base``** on the monthly INR when high-frequency + contract-backed **absent_days** on the base — classifier **stays** **``MONTHLY_RETAINER_INGEST``**.
- **Monthly fee + visit cap (trichotomy):** Do not infer **rate_amount ÷ V** from monthly INR + counters alone. **(1)** Duty/coverage cap → **MAP_STAFF_MONTHLY_DUTY_CAP** when **rate_attendance** + **rate_unit=month** + MAP fingerprint + prose is monthly retainer (calendar **T**, **prorated_base**); **(2)** explicit bundled pack → **C_VISIT_SESSION** with **÷ N**; **(3)** true per-visit + cap → **rate_unit=visit**.
- **``MONTHLY_RETAINER_INGEST`` (one classifier, two amount modes):** ``billing_model=per_visit`` + (**rate_unit=month** OR clear per-physician-per-month / monthly retainer prose) + duty-frequency JSON without explicit per-visit rupee **each** unit → **no** **C_VISIT_SESSION** **×** visit units from ingest; **Mode 1** → **flat** **rate_amount** for part-time / low-frequency / waiver / no absence-backed LOP on the monthly fee; **Mode 2** → **``MAP-STAFF-CAL``**-numeric **``prorated_base``** on **``absent_days``** (calendar **T**) when high-frequency seat-like + contract backs base deduction — row class **remains** **``MONTHLY_RETAINER_INGEST``**; tags **monthly_retainer_duty_frequency_guard** (+ **monthly_retainer_highfreq_map_absence_spine** for Mode 2); **pro_rata_scheduled_sessions** must not mean **× billed_units** when monthly prose governs.
- **Structured visit cadence (``rate_attendance``):** If ``schedule_config`` has **visits_per_month** or **visits_per_week** **> 0**, bill as **C_VISIT_SESSION** **unless** **MAP_STAFF_MONTHLY_DUTY_CAP** applies — **unit_fee = rate_amount** when per-visit; **unit_fee = rate_amount ÷ V** only for explicit **bundled** reading **(2)**, **not** from **rate_unit=month** + counters alone. (**per_visit** monthly seat: **``MONTHLY_RETAINER_INGEST``** bullet above — **not** **C_VISIT_SESSION**.)
- **``MAP-STAFF-CAL`` (see shared DEFINITIONS):** For this profile, same as the shared classifier — **T = context ``calendar_days``**, base ``rate_amount × max(0,T−absent_days)÷T``, mandatory ``calc_notes`` phrase **India calendar-day manpower override** and ``T=calendar_days (N)``. Do **not** use context ``working_days`` as **T** for ``MAP-STAFF-CAL`` rows.
"""

_MIS_BLOCK_TACO = """
**Profile-specific rules (``billing_profile=taco`` — bundled deterministic MIS engine prompt):**
- **Global:** one billed staffing row per employee per site/period (routing_precedence); every ``rate_line`` appears; ``calc_notes_amount`` = ``final_amount`` when schema has it.
- **Addenda:** FMO vs MO routing + gap/hint flags; AS_PER_ACTUALS / BMW / lower-of / markup-with-base; MAP row SC (percentage on ``base``; fixed SC full unless explicit SC proration; ``none`` + admin in STEP 6 only).
- **STEP 1:** ``billing_model`` (as_per_actuals → addendum; per_visit; admin defer; else continue).
- **STEP 2–3:** ``per_shift_missed`` → **3C**; visit counters → **3B** (MODE SWITCH calendar if **>16** / **expected_days≥16**); else **3A**: MAP fingerprint → **calendar T** if **high-frequency** = **any** of **expected_days≥16**, **paid_days≥16**, **days_per_week≥5** (missing **days_per_week** does **not** block the attendance disjuncts); else MAP low-freq → **T=working_days**; **3C** proxy → calendar **T**.
- **STEP 4:** Service charge (percentage on base; fixed usually full).
- **STEP 5:** Headcount cap (paid_days, final_amount, id).
- **STEP 6:** ``OHC_ADMIN_INVOICE_PCT`` from sum_B × pct.
- **Human corrections:** server may append after user message — recompute admin if needed.
- **Site-scoped:** this run's ``rate_lines`` + attendance JSON only.
"""

_PROFILE_BLOCKS: dict[str, str] = {
    BILLING_PROFILE_TCS: _MIS_BLOCK_TCS,
    BILLING_PROFILE_TACO: _MIS_BLOCK_TACO,
}


def mis_profile_prompt_block(billing_profile: str | None) -> str:
    """Profile-specific section: non-``generic`` profiles from inline strings; ``generic`` from bundled prompt file."""
    bp = normalize_billing_profile(billing_profile)
    if bp == BILLING_PROFILE_GENERIC:
        return mis_generic_profile_block_for_prompt_profiles()
    return _PROFILE_BLOCKS.get(bp, mis_generic_profile_block_for_prompt_profiles())


def mis_prompt_policy_version_for_profile(billing_profile: str | None) -> str:
    """Stored in ``mis_summary.mis_prompt_policy_version`` after LLM generation."""
    bp = normalize_billing_profile(billing_profile)
    if bp == BILLING_PROFILE_TCS:
        return MIS_PROMPT_TCS_POLICY_VERSION
    if bp == BILLING_PROFILE_TACO:
        return MIS_PROMPT_TACO_POLICY_VERSION
    return MIS_PROMPT_GENERIC_POLICY_VERSION


def build_mis_summary_user_prompt(
    *,
    billing_profile: str | None,
    client_site_key: str,
    period_start: str,
    period_end: str,
    working_days: int,
    calendar_days: int,
    schema_json: str,
    attendance_json: str,
    rate_lines_json: str,
) -> str:
    bp = normalize_billing_profile(billing_profile)
    if bp == BILLING_PROFILE_TCS:
        return build_mis_tcs_v30_user_prompt(
            client_site_key=client_site_key,
            period_start=period_start,
            period_end=period_end,
            working_days=int(working_days),
            calendar_days=int(calendar_days),
            schema_json=schema_json,
            attendance_json=attendance_json,
            rate_lines_json=rate_lines_json,
        )
    if bp == BILLING_PROFILE_TACO:
        return build_mis_taco_worldclass_user_prompt(
            client_site_key=client_site_key,
            period_start=period_start,
            period_end=period_end,
            working_days=int(working_days),
            calendar_days=int(calendar_days),
            schema_json=schema_json,
            attendance_json=attendance_json,
            rate_lines_json=rate_lines_json,
        )
    return build_mis_generic_worldclass_user_prompt(
        client_site_key=client_site_key,
        period_start=period_start,
        period_end=period_end,
        working_days=int(working_days),
        calendar_days=int(calendar_days),
        schema_json=schema_json,
        attendance_json=attendance_json,
        rate_lines_json=rate_lines_json,
    )


def mis_system_prompt_for_profile(billing_profile: str | None) -> str:
    """Short system message; JSON-only, multi-pass; MAP-STAFF-CAL = calendar T (all profiles)."""
    bp = normalize_billing_profile(billing_profile)
    core = (
        "Output one MIS Summary JSON object only. "
        "Run the user message MULTI-PASS INTERNAL REVIEW silently, then PRE-OUTPUT GATE. "
        "Act as a strict math reviewer: Pass 3 is the only pass that sets row totals per user MATH-PRIMARY (final_amount canonical; calc_notes transcript). Where the schema includes calc_notes_amount, set it only to the terminal final_amount= numeric from calc_notes (§2.1 in generic bundle — mirror only; not for billing math). Post-check, Pass 4–5, self_checks, PRE-OUTPUT GATE: read-only for row totals—never replace JSON final_amount from a validator recomputation; if wrong, redo Pass 3 (rewrite notes+JSON together) or needs_human_review. "
        "Same JSON inputs must yield stable amounts (temperature 0, no discretionary overrides). "
        "Human monthly or >=5d/week staffing (MAP-STAFF-CAL): T=calendar_days from Context only; billing_rule_text 'working days' does not change T; "
        "prorated_base=rate_amount×max(0,T−absent_days)÷T; fixed SC: full service_charge_value by default; prorate fixed SC only if contract explicitly requires proportional/prorata SC (not base-only wording). "
        "calc_notes must contain India calendar-day manpower override for MAP-STAFF-CAL. "
        "Attendance-frequency default: high-frequency/seat-like monthly staffing → calendar T and MAP-class paths when the line matches that classifier; "
        "low-frequency visit-capped lines → visit-unit spine (C_VISIT_SESSION or rate_unit=visit) per the user bundle; "
        "explicit session/visit rules in rate_lines JSON (scheduled_sessions, visits_per_month/week, billing_rules) override this default when they clearly govern. "
        "Per-visit money: use rate_unit=visit or explicit each-visit rupee, or bundled rate_amount÷V when the monthly headline is allocated across V units — "
        "do not use the full undivided monthly rate as one visit unit when V>1 unless bundled rules apply. "
        "For as_per_actuals, read billing_rule_text and billing_rules before choosing zero."
    )
    if bp == BILLING_PROFILE_TACO:
        return (
            "Output one MIS Summary JSON only; temperature 0; final_amount canonical; "
            "calc_notes transcript; calc_notes_amount must match final_amount when schema includes it. "
            "Bundled annexure profile: follow user GLOBAL HARD RULES and STEPs 1–7 (billing_model, per_shift_missed→3C C_SHIFT_MISS, "
            "TIME vs FREQUENCY, SC, headcount cap, OHC admin); MAP calendar T only when high-frequency (≥16 commitment days or days_per_week≥5); else MAP low-freq T=working_days; C_SHIFT_MISS proxy uses calendar_days; "
            "precedence billing_rules > billing_rule_text > description; needs_human_review if still ambiguous."
        )
    if bp == BILLING_PROFILE_TCS:
        return core + " Annexure-scoped profile: site-scoped annexure lines only; no cross-site tiers."
    return (
        core
        + " Profile=generic: user prompt is shared v2.38-class technical core + generic profile; §2.1 — calc_notes_amount mirror; §2.2 — single 2dp round per row, sum_B scope for admin %, lexicographic physician tie-break when ambiguous; Two-phase before OHC_ADMIN_INVOICE_PCT; "
        "staffing base-only where service_charge_type=none; "
        "MAP-STAFF-CAL: calendar T; "
        "map DOCTOR/PHYSICIAN/VISITING_MEDICAL_OFFICER to sole physician rate_attendance line when contract lists exactly one."
    )


__all__ = [
    "MIS_PROMPT_GENERIC_POLICY_VERSION",
    "MIS_PROMPT_POLICY_VERSION",
    "MIS_PROMPT_TACO_POLICY_VERSION",
    "MIS_PROMPT_TCS_POLICY_VERSION",
    "MIS_USER_PROMPT_BUNDLE_BY_PROFILE",
    "build_mis_summary_user_prompt",
    "mis_profile_prompt_block",
    "mis_prompt_policy_version_for_profile",
    "mis_system_prompt_for_profile",
    "mis_user_prompt_bundle_path",
]
