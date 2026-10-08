from pathlib import Path

from app.agents.compliance_call.adapters.transcribe_deepgram import (
    canonical_transcript_from_deepgram_response,
)
from app.agents.compliance_call.doctor_slug import (
    doctor_name_from_filename,
    doctor_slug_from_filename,
)
from app.agents.compliance_call.fingerprint import make_ingest_fingerprint, make_ingest_fingerprint_mysql
from app.agents.compliance_call.glp_sheet_rows import (
    ROLLUP_SCORED_ITEM_IDS,
    bonus_ids_in_rubric_order,
    build_glp_detailed_row,
    build_glp_rollup_row,
)
from app.agents.compliance_call.glp_scoring import (
    README_CALIBRATION_COMPOSITES,
    README_CALIBRATION_DOMAIN_PCTS,
    asr_word_quality_from_words,
    clinician_patient_talk_ratio_string,
    composite_pct_from_domain_pcts,
    grade_from_composite,
    load_glp_rubric,
    merge_clinician_patient_into_structural,
    structural_from_deepgram,
)


def test_doctor_slug_from_filename() -> None:
    assert doctor_slug_from_filename("Copy of Dr Duggirala recording.m4a") == "duggirala"
    assert doctor_slug_from_filename("Dr._Smith_call.mp3") == "smith"


def test_doctor_name_from_filename_glp_pattern() -> None:
    assert doctor_name_from_filename("Copy of Dr Duggirala..mpeg") == "Dr Duggirala"
    assert doctor_name_from_filename("Copy of Dr jitendra.mpeg") == "Dr jitendra"
    assert doctor_name_from_filename("Copy of Dr Yallala Anil..mpeg") == "Dr Yallala Anil"


def test_canonical_transcript_matches_reference_selection() -> None:
    """Same path as ``transcribe-deepgram-india-once.mjs``: alternatives[0].transcript."""
    payload = {
        "metadata": {"duration": 99.0},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "  Hello world.  ",
                            "words": [{"word": "ignored"}],
                        }
                    ]
                }
            ]
        },
    }
    assert canonical_transcript_from_deepgram_response(payload) == "Hello world."


def test_ingest_fingerprint_stable() -> None:
    a = make_ingest_fingerprint(drive_file_id="abc", revision="2024-01-01T00:00:00.000Z")
    b = make_ingest_fingerprint(drive_file_id="abc", revision="2024-01-01T00:00:00.000Z")
    assert a == b
    assert len(a) == 64


def test_mysql_ingest_fingerprint_stable() -> None:
    from app.agents.compliance_call.fingerprint import make_ingest_fingerprint_mysql_conversation

    a = make_ingest_fingerprint_mysql(mysql_call_id=40999648)
    b = make_ingest_fingerprint_mysql(mysql_call_id=40999648)
    assert a == b
    assert len(a) == 64
    assert a != make_ingest_fingerprint_mysql(mysql_call_id=40999649)
    conv = make_ingest_fingerprint_mysql_conversation(conversation_id=1001)
    assert conv != a


def test_compliance_api_retry_flags() -> None:
    from app.agents.compliance_call.api_retry import retryable_http_status

    assert retryable_http_status(429)
    assert retryable_http_status(503)
    assert not retryable_http_status(400)
    assert not retryable_http_status(401)


def test_readme_calibration_composite_math() -> None:
    """README §13: composite from published domain rows within ±0.1."""
    rubric_path = Path(__file__).resolve().parents[1] / "app/agents/compliance_call/rubrics/glp1_consult_rubric.json"
    rubric = load_glp_rubric(rubric_path)

    for i, dmap in enumerate(README_CALIBRATION_DOMAIN_PCTS):
        comp = composite_pct_from_domain_pcts(rubric, dmap)
        exp_c, exp_g = README_CALIBRATION_COMPOSITES[i]
        assert abs(comp - exp_c) <= 0.11, (i, comp, exp_c)
        gletter, _ = grade_from_composite(rubric, comp)
        assert gletter == exp_g, (i, gletter, exp_g, comp)


def test_clinician_patient_talk_ratio_and_merge() -> None:
    assert clinician_patient_talk_ratio_string({0: 60.0, 1: 40.0}, 0, 1) == "60.0:40.0"
    base = {"total_speaking_turns": 3}
    merged = merge_clinician_patient_into_structural(
        base,
        {0: 50.0, 1: 50.0},
        {"clinician_speaker": 1, "patient_speaker": 0},
    )
    assert merged["clinician_patient_talk_ratio"] == "50.0:50.0"
    assert merged["clinician_speaker_id"] == 1
    assert merged["patient_speaker_id"] == 0
    assert merge_clinician_patient_into_structural(base, {0: 1.0}, None) == base


def test_glp_rollup_prefers_clinician_patient_ratio_column() -> None:
    rubric_path = Path(__file__).resolve().parents[1] / "app/agents/compliance_call/rubrics/glp1_consult_rubric.json"
    rubric = load_glp_rubric(rubric_path)
    ev = {
        "patient_summary": "Test patient",
        "comments": "ok",
        "scored_at": "2026-05-01T12:00:00Z",
        "composite_pct": 35.9,
        "grade": "D",
        "grade_label": "Below Standard",
        "domain_pcts": README_CALIBRATION_DOMAIN_PCTS[0],
        "scored_items": [
            {"item_id": iid, "status": "NO", "evidence": "x"} for iid in ROLLUP_SCORED_ITEM_IDS
        ],
        "bonus_counseling_items": [
            {"item_id": iid, "status": "NO", "evidence": "n"} for iid in bonus_ids_in_rubric_order(rubric)
        ],
    }
    st = {
        "duration_minutes": 8.5,
        "total_speaking_turns": 1,
        "clinician_patient_talk_ratio": "55.0:45.0",
        "diarized_s0_to_others_ratio": "90.0:10.0",
    }
    row = build_glp_rollup_row(
        serial_no="1",
        doctor_name="Dr X",
        patient_summary="p",
        date_scored="2026-05-01T12:00:00Z",
        call_timestamp_utc="2026-04-30 11:39:44 UTC",
        structural=st,
        domain_pcts=README_CALIBRATION_DOMAIN_PCTS[0],
        composite_pct=35.9,
        grade="D",
        grade_label="Below Standard",
        status_text="D (Below Standard)",
        eval_doc=ev,
        comments="c",
    )
    assert row[10] == "55.0:45.0"


def test_structural_from_deepgram_turn_clustering() -> None:
    payload = {
        "metadata": {"duration": 10.0},
        "results": {
            "utterances": [
                {"start": 0.0, "end": 1.0, "speaker": 0},
                {"start": 3.0, "end": 4.0, "speaker": 0},
            ]
        },
    }
    s = structural_from_deepgram(payload, diarize=False)
    assert s["total_speaking_turns"] == 2
    assert s["doctor_to_patient_ratio"] == "NA"
    assert "speaker_role_note" in s


def test_asr_word_quality_from_words() -> None:
    assert not asr_word_quality_from_words([])
    q = asr_word_quality_from_words(
        [{"confidence": 0.9}, {"confidence": 0.4}, {"confidence": 0.8}]
    )
    assert q["word_count"] == 3
    assert q["pct_words_below_0_5"] > 30


def test_glp_rollup_and_detailed_row_widths() -> None:
    rubric_path = Path(__file__).resolve().parents[1] / "app/agents/compliance_call/rubrics/glp1_consult_rubric.json"
    rubric = load_glp_rubric(rubric_path)
    ev = {
        "patient_summary": "Test patient",
        "comments": "ok",
        "scored_at": "2026-05-01T12:00:00Z",
        "composite_pct": 35.9,
        "grade": "D",
        "grade_label": "Below Standard",
        "domain_pcts": README_CALIBRATION_DOMAIN_PCTS[0],
        "scored_items": [
            {"item_id": iid, "status": "NO", "evidence": "x"} for iid in ROLLUP_SCORED_ITEM_IDS
        ],
        "bonus_counseling_items": [
            {"item_id": iid, "status": "NO", "evidence": "n"} for iid in bonus_ids_in_rubric_order(rubric)
        ],
    }
    st = {"duration_minutes": 8.5, "total_speaking_turns": 1, "doctor_to_patient_ratio": "NA"}
    r49 = build_glp_rollup_row(
        serial_no="1",
        doctor_name="Dr X",
        patient_summary="p",
        date_scored="2026-05-01T12:00:00Z",
        call_timestamp_utc="2026-04-30 11:39:44 UTC",
        structural=st,
        domain_pcts=README_CALIBRATION_DOMAIN_PCTS[0],
        composite_pct=35.9,
        grade="D",
        grade_label="Below Standard",
        status_text="D (Below Standard)",
        eval_doc=ev,
        comments="c",
    )
    assert len(r49) == 49
    r93 = build_glp_detailed_row(
        rubric=rubric,
        serial_no="1",
        doctor_name="Dr X",
        patient_summary="p",
        date_scored="2026-05-01T12:00:00Z",
        call_timestamp_utc="2026-04-30 11:39:44 UTC",
        structural=st,
        domain_pcts=README_CALIBRATION_DOMAIN_PCTS[0],
        composite_pct=35.9,
        grade="D",
        grade_label="Below Standard",
        status_text="D (Below Standard)",
        eval_doc=ev,
        comments="c",
    )
    assert len(r93) == 93


def test_glp_rubric_loads_bundled() -> None:
    rubric = load_glp_rubric()
    assert str((rubric.get("meta") or {}).get("version")) == "1.2"


def test_glp_rubric_defines_consult_type_detection() -> None:
    """v1.2 adds consult_type detection that gates the initiation-only na_rules."""
    rubric = load_glp_rubric()
    ctd = (rubric.get("scoring") or {}).get("consult_type_detection") or {}
    assert ctd.get("values") == ["initial", "follow_up"]
    assert ctd.get("default") == "initial"
    assert "follow_up" in (ctd.get("follow_up_rule") or "")


def test_initiation_only_items_are_na_for_followup() -> None:
    """The five initiation-only items must reference consult_type/follow_up in their na_rule."""
    rubric = load_glp_rubric()
    na_by_id = {
        str(it.get("id")): str(it.get("na_rule") or "")
        for dom in (rubric.get("scoring") or {}).get("domains") or []
        for it in dom.get("checklist") or []
        if it.get("id")
    }
    for item_id in (
        "bmi_weight_target",
        "prior_wl_attempts",
        "dose_escalation",
        "mechanism_of_action",
        "lab_baseline",
    ):
        na = na_by_id[item_id].lower()
        assert "follow_up" in na, f"{item_id} na_rule should reference follow_up: {na!r}"
        assert na != "never na."


def test_compact_for_llm_passes_consult_type_detection() -> None:
    """The model payload must carry consult_type_detection (meta/scope are not sent)."""
    from app.agents.compliance_call.adapters.rubric_openai import _compact_for_llm

    compact = _compact_for_llm(load_glp_rubric())
    assert "consult_type_detection" in compact
    assert compact["consult_type_detection"].get("default") == "initial"


def test_sheets_append_a1_column_letter() -> None:
    from app.agents.compliance_call.adapters.sheets_append import _a1_column_letter

    assert _a1_column_letter(1) == "A"
    assert _a1_column_letter(26) == "Z"
    assert _a1_column_letter(27) == "AA"
    assert _a1_column_letter(49) == "AW"
    assert _a1_column_letter(93) == "CO"
