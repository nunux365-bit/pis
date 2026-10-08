"""MIS prompt composition: profiles stay disjoint for safe iteration."""

from __future__ import annotations

from app.agents.o2c_ohc.billing_profile import BILLING_PROFILE_GENERIC, BILLING_PROFILE_TACO, BILLING_PROFILE_TCS
from app.agents.o2c_ohc.mis_prompt_generic_from_code import MIS_PROMPT_GENERIC_POLICY_VERSION
from app.agents.o2c_ohc.mis_prompt_profiles import (
    MIS_USER_PROMPT_BUNDLE_BY_PROFILE,
    build_mis_summary_user_prompt,
    mis_profile_prompt_block,
    mis_prompt_policy_version_for_profile,
    mis_system_prompt_for_profile,
    mis_user_prompt_bundle_path,
)


def test_user_prompt_bundle_files_exist_on_disk() -> None:
    for bp, expected_name in MIS_USER_PROMPT_BUNDLE_BY_PROFILE.items():
        p = mis_user_prompt_bundle_path(bp)
        assert p.name == expected_name
        assert p.is_file(), f"missing MIS user prompt bundle: {p}"


def test_profile_blocks_are_distinct_strings() -> None:
    tcs = mis_profile_prompt_block(BILLING_PROFILE_TCS)
    taco = mis_profile_prompt_block(BILLING_PROFILE_TACO)
    gen = mis_profile_prompt_block(BILLING_PROFILE_GENERIC)
    assert tcs != taco
    assert tcs != gen
    assert taco != gen
    assert "billing_profile=tcs" in tcs
    assert "billing_profile=taco" in taco
    assert "billing_profile=generic" in gen


def test_system_prompt_varies_by_profile() -> None:
    assert "Bundled annexure profile" in mis_system_prompt_for_profile(BILLING_PROFILE_TACO)
    assert "Annexure-scoped profile" in mis_system_prompt_for_profile(BILLING_PROFILE_TCS)
    assert "Profile=generic" in mis_system_prompt_for_profile(BILLING_PROFILE_GENERIC)


def test_generic_bundled_user_prompt_formats_and_policy_version() -> None:
    assert mis_prompt_policy_version_for_profile(BILLING_PROFILE_GENERIC) == MIS_PROMPT_GENERIC_POLICY_VERSION
    s = build_mis_summary_user_prompt(
        billing_profile=BILLING_PROFILE_GENERIC,
        client_site_key="site-a",
        period_start="2025-01-01",
        period_end="2025-01-31",
        working_days=23,
        calendar_days=31,
        schema_json="{}",
        attendance_json="[]",
        rate_lines_json="[]",
    )
    assert "{schema_json}" not in s
    assert "site-a" in s
    assert MIS_PROMPT_GENERIC_POLICY_VERSION in s
    assert "billing_profile=generic" in s


def test_tcs_as_per_actuals_lower_of_cap_missing_actuals_not_zero_by_conservative() -> None:
    tcs = mis_user_prompt_bundle_path(BILLING_PROFILE_TCS).read_text(encoding="utf-8")
    assert "lower of (actual, cap)" in tcs.lower()
    assert "forbidden" in tcs.lower() and "conservative" in tcs.lower()


def test_generic_per_visit_month_null_counters_spine_anchors() -> None:
    """Regression: generic slim must keep MAP (calendar T) before C_WORKING_DAY for high-frequency lines."""
    gen = mis_user_prompt_bundle_path(BILLING_PROFILE_GENERIC).read_text(encoding="utf-8")
    assert "C_HIGH_FREQUENCY_MONTHLY" in gen and "C_WORKING_DAY" in gen
    assert gen.index("C_HIGH_FREQUENCY_MONTHLY") < gen.index("C_WORKING_DAY")
    assert "C_MAP_STAFF_CAL" in gen and "calendar_days" in gen
    assert "FIRST MATCH WINS" in gen or "first match wins" in gen.lower()
    assert "per_visit" in gen.lower()


def test_monthly_retainer_ingest_template_subordination_in_tcs_and_generic() -> None:
    """Regression: prod-shaped per_visit+month+visits_per_month rows must not use C_VISIT_SESSION × from billing_rules alone."""
    needle = "Ingest-template subordination"
    for bp in (BILLING_PROFILE_TCS, BILLING_PROFILE_GENERIC):
        text = mis_user_prompt_bundle_path(bp).read_text(encoding="utf-8")
        assert needle in text, f"missing {needle!r} in bundle for {bp}"
        assert "Narrow carve-out" in text or "Carve-out" in text, f"missing precedence carve-out in bundle for {bp}"


def test_taco_ingest_template_subordination_line() -> None:
    taco = mis_user_prompt_bundle_path(BILLING_PROFILE_TACO).read_text(encoding="utf-8")
    assert "Ingest-template subordination" in taco


def test_tcs_monthly_retainer_highfreq_map_absence_spine() -> None:
    """tcs bundle: MONTHLY_RETAINER_INGEST Mode 2 reuses MAP-STAFF-CAL numeric spine for absent_days — not C_VISIT_SESSION × visit units."""
    tcs = mis_user_prompt_bundle_path(BILLING_PROFILE_TCS).read_text(encoding="utf-8")
    assert "monthly_retainer_highfreq_map_absence_spine" in tcs
    assert "MONTHLY_RETAINER_INGEST" in tcs
    assert "Mode 2" in tcs
    assert "MAP-STAFF-CAL" in tcs
    assert "prorated_base" in tcs


def test_tcs_deterministic_engine_prompt_anchors() -> None:
    """tcs bundle: deterministic ROUTINE, PV-LADDER per_visit step, MAP calendar T, schema placeholder."""
    tcs = mis_user_prompt_bundle_path(BILLING_PROFILE_TCS).read_text(encoding="utf-8")
    assert "deterministic mis billing engine" in tcs.lower()
    assert "tcs-ohc-deterministic-engine-v10" in tcs
    assert "deterministic routine (procedural spine" in tcs.lower()
    assert "this routine wins" in tcs.lower()
    assert "step pv-ladder" in tcs.lower()
    assert "bill-all-then-cap" in tcs.lower()
    assert "numerator lock" in tcs.lower()
    assert "do not mean" in tcs.lower() and "n_wd" in tcs.lower()
    assert "map_staff_monthly_duty_cap" in tcs.lower()
    assert "prescribed_slots_pro_rata_scheduled_sessions" in tcs
    assert "row-field lock" in tcs.lower()
    assert "per-seat contract fee" in tcs.lower()
    assert "{schema_json}" in tcs


def test_taco_deterministic_engine_prompt_anchors() -> None:
    """taco bundle: deterministic engine STEPs, C_SHIFT_MISS 3C, schema placeholder."""
    taco = mis_user_prompt_bundle_path(BILLING_PROFILE_TACO).read_text(encoding="utf-8")
    assert "deterministic mis billing engine" in taco.lower()
    assert "STEP 1" in taco and "STEP 7" in taco
    assert "STEP 3C" in taco and "C_SHIFT_MISS" in taco
    assert "per_shift_missed" in taco
    assert "calendar absence proxy" in taco.lower()
    assert "TIME LOGIC" in taco and "FREQUENCY LOGIC" in taco
    assert "MAP-STAFF-CAL" in taco
    assert "OHC_ADMIN_INVOICE_PCT" in taco
    assert "{schema_json}" in taco
    assert "taco-ohc-deterministic-engine-v13" in taco
    assert "is_omitted=false" in taco.lower()
    assert "treating missing" in taco.lower() and "days_per_week" in taco.lower()
    assert "bill-all-then-cap" in taco.lower()
    assert "do not under-bill" in taco.lower()
    assert "deterministic routine (procedural spine" in taco.lower()
    assert "this routine wins" in taco.lower()
    assert "numerator lock" in taco.lower()
    assert "pro_rata_working_days" in taco and "does not mean" in taco.lower()
    assert "high_frequency" in taco.lower() and ">= 16" in taco
    assert "scheduled_units > 16" in taco
    assert "mathematical minimum" in taco.lower()
    assert "per-seat contract fee" in taco.lower()


def test_taco_policy_version_constant_matches_bundle() -> None:
    from app.agents.o2c_ohc.mis_prompt_taco_from_code import MIS_PROMPT_TACO_POLICY_VERSION

    assert mis_prompt_policy_version_for_profile(BILLING_PROFILE_TACO) == MIS_PROMPT_TACO_POLICY_VERSION
    taco = mis_user_prompt_bundle_path(BILLING_PROFILE_TACO).read_text(encoding="utf-8")
    assert MIS_PROMPT_TACO_POLICY_VERSION in taco


def test_tcs_policy_version_constant_matches_bundle() -> None:
    from app.agents.o2c_ohc.mis_prompt_tcs_v30 import MIS_PROMPT_TCS_POLICY_VERSION

    assert mis_prompt_policy_version_for_profile(BILLING_PROFILE_TCS) == MIS_PROMPT_TCS_POLICY_VERSION
    tcs = mis_user_prompt_bundle_path(BILLING_PROFILE_TCS).read_text(encoding="utf-8")
    assert MIS_PROMPT_TCS_POLICY_VERSION in tcs
