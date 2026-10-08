"""GLP v1.1 rubric: OpenAI applies YES/NO/NA + evidence; Python applies deterministic math.

Uses the **Responses** API: upload ``files.create(...)`` with ``purpose="user_data"``, pass
``input_file`` by ``file_id``, ``text.format`` as **json_schema** (strict) when enabled, else
``json_object``. Deletes uploaded files after all turns.

Repair strategy:
1. **Continuation** — ``previous_response_id`` + short ``input_text`` so the model can fill gaps
   without a second upload (same in-context transcript + rubric as the primary response).
2. **Fallback** — standalone Responses call with fresh uploads (repair JSON + transcript) if
   continuation fails or items remain missing.

Structured outputs fall back to ``json_object`` automatically when the API rejects the schema
(e.g. model mismatch).

**Async:** Uses :class:`openai.AsyncOpenAI` — call :func:`run_rubric_eval` with ``await`` from
async LangGraph nodes or other asyncio contexts.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import APIError, AsyncOpenAI

from app.agents.compliance_call.api_retry import async_call_with_retry, retryable_openai_error
from app.agents.compliance_call.glp_sheet_rows import ROLLUP_SCORED_ITEM_IDS, bonus_ids_in_rubric_order
from app.agents.compliance_call.glp_scoring import (
    apply_deterministic_scores,
    bonus_item_ids,
    iter_scored_checklist,
    load_glp_rubric,
    scored_item_ids,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

# ASR post-edit for GLP compliance: rubric grades this text—fidelity > fluency.
_TRANSCRIPT_NORMALIZE_SYSTEM = (
    "You are a clinical transcript post-editor. Output one block of plain transcript text only—"
    "no title, no preamble, no bullet list, no markdown fences."
)

_TRANSCRIPT_NORMALIZE_USER_PREFIX = """\
## Role
You normalize machine-transcribed (ASR) audio from **telehealth GLP-1 / weight-management e-consults**.
The text is used **only** as faithful dialogue for downstream **automated quality rubric** scoring ( \
safety screening, counseling, follow-up). Errors in normalization can change compliance scores— \
**preserve clinical meaning exactly**.

## Medical context (for interpretation only—do not inject content)
Typical topics: GLP-1 receptor agonists (e.g. semaglutide, tirzepatide, liraglutide), obesity/T2DM context, \
weekly injectable pens, dose titration (often mg steps), common GI side effects, when to seek care, \
diet/activity advice, cost/brand discussion, follow-up timing. \
**If the speaker did not say something, it must not appear in your output.**

## Input
- May be **Hinglish** (Hindi + English), code-mixed scripts, disfluencies, and ASR garbage.
- May include **speaker labels** `[S0]`, `[S1]`, … — copy them **verbatim** when present.
- May include **privacy redactions** `[NAME_*]`, `[NAME_GIVEN_*]`, `[NAME_MEDICAL_PROFESSIONAL_*]`, etc. — copy **character-for-character**, never expand or replace.

## Allowed edits (minimal span, high confidence only)
- Fix **obvious ASR typos** when the intended word is clear from immediate context (especially drug/brand **spelling**, not wholesale rewrites).
- Normalize **numbers & doses**: spacing (e.g. `1 26` → `126` for weight), obvious dose tokenization (`0 0.2 5` → `0.25` when clearly a mg dose in context), **without** inventing units the speaker did not imply.
- If fillers split digits in a number, you may re-join when context is clearly a number field (weight/height/dose), e.g. `1 yeah 72` → `172`, `1 hmm 26` → `126`.
- Improve **punctuation, line breaks, fillers** (`yeah`, `ok`) only lightly so the dialogue reads naturally—**do not delete** clinically relevant phrases.
- Fix **obvious English grammar** that does not change who said what or what was medically stated.

## Forbidden (hard)
- **No new clinical facts**: no added symptoms, diagnoses, labs, medications, doses the patient/doctor did not say.
- **No negation flips**: preserve `no`, `not`, `never`, `denies`, `nothing` and affirmative answers as stated.
- **No summarization** or bullet-style visit note: keep **conversational transcript** form, same **order** of turns.
- **Do not translate** the whole consult to one language; keep code-mixing natural unless a single word is an obvious ASR error for a clearly intended English/Hindi term.
- **Do not rationalize** vague or incomplete medical statements into textbook-perfect counseling.
- **Organizations / brands / proper names in non-Latin script**: do **not** transliterate or “fix” to Latin unless the **same line** already makes the intended spelling unambiguous; if unsure, **leave the token unchanged**.
- **Never** replace redaction tokens or speaker tags.

## Self-check before you output (silent; do not print)
1) Every `[NAME_…]` and `[S#]` from the input appears unchanged in the output.
2) No new numbers, drugs, or advice compared to what the source plausibly states.
3) Negations and answers (yes/no) still match the source.
4) Output is **only** the cleaned transcript text.

## Transcript to normalize
---


"""


@asynccontextmanager
async def _compliance_openai_session(api_key: str, injected: AsyncOpenAI | None):
    """Use an injected long-lived client for LangGraph, or a short-lived one for scripts."""
    if injected is not None:
        yield injected
    else:
        async with AsyncOpenAI(api_key=api_key, timeout=600.0) as client:
            yield client


_EVIDENCE_RULES = (
    "For each item, evidence MUST be: if YES, a short verbatim substring from the transcript "
    "that appears in the attached transcript text (if you cannot cite such a substring, answer NO); "
    "you may append a brief parenthetical only for obvious speech-to-text misspellings of drugs or "
    "medical terms when intent is clear from context; if NO, state what is missing or contradicted; "
    "if NA, cite which na_rule applies."
)

_ITEM_INDEPENDENCE = (
    "Evaluate each checklist item independently — do not let your judgment on one item influence "
    "another."
)

_STATUS_PRECEDENCE = (
    "For each item, decide status in this order: (1) If na_rule applies, choose NA and stop. "
    "(2) Else if yes_rule is satisfied, YES. (3) Else NO."
)

# consult_type gates the na_rule of the initiation-only items (v1.2+ rubric).
_CONSULT_TYPE_NOTE = (
    "Before scoring items, determine consult_type (initial vs follow_up) from the transcript using "
    "the rubric's consult_type_detection block: classify as follow_up ONLY when the doctor asks "
    "whether the patient is already on a GLP-1 / looking to increase their dose / doing a follow-on "
    "consult AND the patient confirms yes; otherwise treat it as initial (default to initial when "
    "ambiguous). Some na_rules depend on consult_type — apply them accordingly. State the determined "
    "consult_type and the one-line reason at the start of the comments field."
)

# Grader guidance: STT is noisy; models should interpret intent without inventing facts.
_ASR_TRANSCRIPT_NOTE = (
    "The transcript is machine-transcribed (ASR). It may mis-spell drug or brand names, mix "
    "languages, or garble numbers or doses. Use context for obvious ASR fixes, but apply "
    "yes_rule / no_rule / na_rule strictly: do not mark YES unless the transcript content "
    "(after resolving obvious ASR errors) satisfies the yes_rule. Do not invent dialogue. "
    "If numbers or doses are garbled and the rule requires a specific disclosure, do not mark YES "
    "unless the requirement is clearly met in the text. "
    "If overlapping speech makes attribution unclear and the item depends on who said what, prefer NO "
    "and explain in evidence. "
    "If audio intent is ambiguous, prefer NO and say why in evidence."
)


def _rubric_path() -> Path:
    raw = (settings.compliance_rubric_json_path or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "rubrics" / "glp1_consult_rubric.json"


def _effective_model() -> str:
    m = (settings.compliance_openai_model or "").strip()
    if m:
        return m
    return (settings.openai_chat_model or "gpt-5.4").strip()


def _max_output_tokens() -> int:
    n = int(settings.compliance_openai_max_output_tokens or 0)
    if n > 0:
        return n
    return int(settings.o2c_llm_max_output_tokens)


def _compact_for_llm(rubric: dict[str, Any]) -> dict[str, Any]:
    """Rules-only payload for the model (full text, no truncation)."""
    scoring = rubric.get("scoring") or {}
    # consult_type_detection (v1.2+): shared definition that some na_rules reference.
    # meta/scope are not sent to the model, so pass this through explicitly.
    consult_type_detection = scoring.get("consult_type_detection")
    scored: list[dict[str, Any]] = []
    for dom in scoring.get("domains") or []:
        did = str(dom.get("id") or "")
        for it in dom.get("checklist") or []:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            scored.append(
                {
                    "item_id": str(it.get("id")),
                    "domain_id": did,
                    "item": str(it.get("item") or ""),
                    "yes_rule": str(it.get("yes_rule") or ""),
                    "no_rule": str(it.get("no_rule") or ""),
                    "na_rule": str(it.get("na_rule") or ""),
                }
            )
    bonus_block = scoring.get("bonus_counseling_quality") or {}
    bonus: list[dict[str, Any]] = []
    for it in bonus_block.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        bonus.append(
            {
                "item_id": str(it.get("id")),
                "name": str(it.get("name") or ""),
                "yes_rule": str(it.get("yes_rule") or ""),
                "no_rule": str(it.get("no_rule") or ""),
                "na_rule": str(it.get("na_rule") or ""),
            }
        )
    payload: dict[str, Any] = {"scored_checklist": scored, "bonus_counseling_quality": bonus}
    if isinstance(consult_type_detection, dict) and consult_type_detection:
        payload["consult_type_detection"] = consult_type_detection
    return payload


def _eval_item_row_schema(item_ids: list[str]) -> dict[str, Any]:
    ids_sorted = sorted(set(item_ids))
    return {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "enum": ids_sorted},
            "status": {"type": "string", "enum": ["YES", "NO", "NA"]},
            "evidence": {"type": "string"},
        },
        "required": ["item_id", "status", "evidence"],
        "additionalProperties": False,
    }


def _schema_glp_full_eval(scored_ids: list[str], bonus_ids: list[str]) -> dict[str, Any]:
    """JSON Schema for primary rubric JSON (strict structured outputs)."""
    si = sorted(set(scored_ids))
    bi = sorted(set(bonus_ids))
    scored_row = _eval_item_row_schema(si)
    if bi:
        bonus_prop: dict[str, Any] = {
            "type": "array",
            "items": _eval_item_row_schema(bi),
            "minItems": len(bi),
            "maxItems": len(bi),
        }
    else:
        bonus_prop = {
            "type": "array",
            "items": _eval_item_row_schema(["__no_bonus_items__"]),
            "minItems": 0,
            "maxItems": 0,
        }
    return {
        "type": "object",
        "properties": {
            "patient_summary": {"type": "string"},
            "comments": {"type": "string"},
            "scored_items": {
                "type": "array",
                "items": scored_row,
                "minItems": len(si),
                "maxItems": len(si),
            },
            "bonus_items": bonus_prop,
        },
        "required": ["patient_summary", "comments", "scored_items", "bonus_items"],
        "additionalProperties": False,
    }


def _schema_scored_items_only(missing_ids: list[str]) -> dict[str, Any]:
    mi = sorted(set(missing_ids))
    row = _eval_item_row_schema(mi)
    return {
        "type": "object",
        "properties": {
            "scored_items": {
                "type": "array",
                "items": row,
                "minItems": len(mi),
                "maxItems": len(mi),
            },
        },
        "required": ["scored_items"],
        "additionalProperties": False,
    }


def _schema_bonus_items_only(missing_ids: list[str]) -> dict[str, Any]:
    mi = sorted(set(missing_ids))
    row = _eval_item_row_schema(mi)
    return {
        "type": "object",
        "properties": {
            "bonus_items": {
                "type": "array",
                "items": row,
                "minItems": len(mi),
                "maxItems": len(mi),
            },
        },
        "required": ["bonus_items"],
        "additionalProperties": False,
    }


def _should_fallback_structured(exc: BaseException) -> bool:
    if isinstance(exc, APIError):
        code = getattr(exc, "status_code", None)
        if code is not None and int(code) < 500:
            return True
    return False


def _normalize_row(it: dict[str, Any]) -> dict[str, str] | None:
    if not isinstance(it, dict):
        return None
    iid = str(it.get("item_id") or "").strip()
    if not iid:
        return None
    st = str(it.get("status") or "NO").strip().upper()
    if st not in ("YES", "NO", "NA"):
        st = "NO"
    ev = it.get("evidence")
    if ev is None:
        ev = it.get("rationale")
    ev_s = str(ev or "")
    return {"item_id": iid, "status": st, "evidence": ev_s}


def _item_rule_blob(rubric: dict[str, Any], item_id: str) -> str:
    for _d, _w, it in iter_scored_checklist(rubric):
        if str(it.get("id")) == item_id:
            return json.dumps(
                {
                    "yes_rule": it.get("yes_rule"),
                    "no_rule": it.get("no_rule"),
                    "na_rule": it.get("na_rule"),
                },
                ensure_ascii=False,
            )
    scoring = rubric.get("scoring") or {}
    for it in (scoring.get("bonus_counseling_quality") or {}).get("items") or []:
        if isinstance(it, dict) and str(it.get("id")) == item_id:
            return json.dumps(
                {
                    "yes_rule": it.get("yes_rule"),
                    "no_rule": it.get("no_rule"),
                    "na_rule": it.get("na_rule"),
                },
                ensure_ascii=False,
            )
    return "{}"


async def _upload_user_data_file(client: AsyncOpenAI, *, filename: str, data: bytes) -> str:
    """Same tuple upload shape as ``llm_extract`` (OpenAI ``user_data`` files)."""

    async def _go() -> str:
        up = await client.files.create(file=(filename, data), purpose="user_data")
        fid = getattr(up, "id", None)
        if not fid:
            raise RuntimeError("OpenAI files.create returned no id")
        return str(fid)

    return await async_call_with_retry(
        f"openai.files.create({filename})",
        _go,
        is_retryable=retryable_openai_error,
    )


async def _delete_openai_files(client: AsyncOpenAI, file_ids: list[str]) -> None:
    for fid in file_ids:
        try:
            await client.files.delete(fid)
        except Exception:
            log.debug("OpenAI file delete failed file_id=%s", fid)


def _parse_response_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    return json.loads(raw)


async def _responses_json_primary(
    client: AsyncOpenAI,
    *,
    model: str,
    max_output_tokens: int,
    system: str,
    user_text: str,
    file_ids: list[str],
    format_inner: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """First turn: system + user with ``input_file`` parts then ``input_text`` (matches ``llm_extract``)."""
    content: list[dict[str, Any]] = []
    for fid in file_ids:
        content.append({"type": "input_file", "file_id": fid})
    content.append({"type": "input_text", "text": user_text})

    fin = format_inner if format_inner is not None else {"type": "json_object"}

    async def _go() -> tuple[dict[str, Any], str | None]:
        resp = await client.responses.create(
            model=model,
            temperature=0,
            max_output_tokens=max_output_tokens,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            text={"format": fin},
        )
        raw = (getattr(resp, "output_text", None) or "").strip()
        rid = getattr(resp, "id", None)
        return _parse_response_json(raw), (str(rid) if rid else None)

    return await async_call_with_retry(
        "openai.responses.create(primary)",
        _go,
        is_retryable=retryable_openai_error,
    )


async def _responses_json_continue(
    client: AsyncOpenAI,
    *,
    model: str,
    max_output_tokens: int,
    previous_response_id: str,
    user_text: str,
    format_inner: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Follow-up turn: chain from prior response (no new file uploads)."""

    fin = format_inner if format_inner is not None else {"type": "json_object"}

    async def _go() -> tuple[dict[str, Any], str | None]:
        resp = await client.responses.create(
            model=model,
            temperature=0,
            max_output_tokens=max_output_tokens,
            previous_response_id=previous_response_id,
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_text}],
                }
            ],
            text={"format": fin},
        )
        raw = (getattr(resp, "output_text", None) or "").strip()
        rid = getattr(resp, "id", None)
        return _parse_response_json(raw), (str(rid) if rid else None)

    return await async_call_with_retry(
        "openai.responses.create(continue)",
        _go,
        is_retryable=retryable_openai_error,
    )


async def run_transcript_normalize(
    *,
    transcript_text: str,
    openai_client: AsyncOpenAI | None = None,
) -> str:
    """Normalize noisy ASR transcript text while preserving clinical meaning."""
    raw = str(transcript_text or "").strip()
    if not raw:
        return ""

    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    model = (settings.compliance_transcript_normalize_model or "").strip() or _effective_model()
    max_out = _max_output_tokens()
    temp = float(settings.compliance_transcript_normalize_temperature or 0.2)
    user_text = _TRANSCRIPT_NORMALIZE_USER_PREFIX + raw

    async with _compliance_openai_session(key, openai_client) as client:
        async def _go() -> str:
            resp = await client.responses.create(
                model=model,
                temperature=temp,
                max_output_tokens=max_out,
                input=[
                    {"role": "system", "content": _TRANSCRIPT_NORMALIZE_SYSTEM},
                    {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
                ],
            )
            return str(getattr(resp, "output_text", "") or "").strip()

        out = await async_call_with_retry(
            "openai.responses.create(transcript_normalize)",
            _go,
            is_retryable=retryable_openai_error,
        )
        return out or raw


def _merge_scored(
    by_scored: dict[str, dict[str, str]],
    repair: dict[str, Any],
    allowed_ids: set[str],
) -> None:
    for it in repair.get("scored_items") or []:
        row = _normalize_row(it)
        if row and row["item_id"] in allowed_ids:
            by_scored[row["item_id"]] = row


def _merge_bonus(
    by_bonus: dict[str, dict[str, str]],
    repair: dict[str, Any],
    allowed_ids: set[str],
) -> None:
    for it in repair.get("bonus_items") or []:
        row = _normalize_row(it)
        if row and row["item_id"] in allowed_ids:
            by_bonus[row["item_id"]] = row


async def run_rubric_eval(
    *,
    transcript_text: str,
    doctor_slug: str,
    doctor_name: str,
    rubric_version: str | None = None,
    deepgram_summary: dict[str, Any] | None = None,
    openai_client: AsyncOpenAI | None = None,
) -> dict[str, Any]:
    path = _rubric_path()
    rubric = load_glp_rubric(path)
    meta = rubric.get("meta") or {}
    authority_version = str(meta.get("version") or "1.1")

    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    compact = _compact_for_llm(rubric)
    scored_ids = set(scored_item_ids(rubric))
    bonus_ids = set(bonus_item_ids(rubric))

    struct_note = ""
    if deepgram_summary:
        struct = deepgram_summary.get("structural") if isinstance(deepgram_summary, dict) else None
        if struct:
            struct_note = (
                "\nStructural metrics (advisory context only; scoring is from transcript file):\n"
                + json.dumps(struct, ensure_ascii=False, indent=2)
            )

    model = _effective_model()
    max_out = _max_output_tokens()

    schema = (
        '{"patient_summary":"string","comments":"string",'
        '"scored_items":[{"item_id":"string","status":"YES|NO|NA","evidence":"string"}],'
        '"bonus_items":[{"item_id":"string","status":"YES|NO|NA","evidence":"string"}]}'
    )
    user1 = (
        f"You grade a GLP-1 e-consult transcript. Return ONLY JSON matching: {schema}\n"
        f"{_ASR_TRANSCRIPT_NOTE}\n"
        f"{_ITEM_INDEPENDENCE}\n"
        f"{_CONSULT_TYPE_NOTE}\n"
        f"{_STATUS_PRECEDENCE}\n"
        "Apply each yes_rule / no_rule / na_rule strictly using the attached rubric rules file. "
        "Use NA only when na_rule applies.\n"
        f"{_EVIDENCE_RULES}\n"
        "You MUST include one scored_items entry for every item_id in scored_checklist "
        "and one bonus_items entry for every item in bonus_counseling_quality.\n\n"
        f"doctor_slug={doctor_slug!s} doctor_name={doctor_name!s}\n"
        "Attached files (in order): (1) transcript.txt — full call transcript "
        "(may include [Sn] speaker prefixes when present). "
        "(2) glp_rubric_rules.json — scored_checklist and bonus_counseling_quality definitions.\n"
        f"{struct_note}\n"
    )

    rubric_bytes = json.dumps(compact, ensure_ascii=False, indent=2).encode("utf-8")
    transcript_bytes = transcript_text.encode("utf-8")
    log.info(
        "compliance rubric OpenAI Responses: model=%s transcript=%d bytes rubric_json=%d bytes",
        model,
        len(transcript_bytes),
        len(rubric_bytes),
    )

    async with _compliance_openai_session(key, openai_client) as client:
        all_uploaded: list[str] = []
        last_response_id: str | None = None
        response_ids: list[str] = []
        # False after a standalone file-repair Responses call (new thread without full rubric JSON).
        bonus_continuation_ok: bool = True
        try:
            all_uploaded.append(await _upload_user_data_file(client, filename="transcript.txt", data=transcript_bytes))
            all_uploaded.append(await _upload_user_data_file(client, filename="glp_rubric_rules.json", data=rubric_bytes))
            fmt_primary: dict[str, Any] | None = None
            if settings.compliance_openai_structured_outputs:
                fmt_primary = {
                    "type": "json_schema",
                    "name": "glp_rubric_eval",
                    "strict": True,
                    "schema": _schema_glp_full_eval(list(scored_ids), list(bonus_ids)),
                }
            try:
                parsed, rid0 = await _responses_json_primary(
                    client,
                    model=model,
                    max_output_tokens=max_out,
                    system="You output only compact JSON for machine parsing. No markdown.",
                    user_text=user1,
                    file_ids=all_uploaded,
                    format_inner=fmt_primary,
                )
            except APIError as e:
                if fmt_primary and _should_fallback_structured(e):
                    log.warning(
                        "compliance rubric primary: structured outputs unavailable (%s); using json_object",
                        e,
                    )
                    parsed, rid0 = await _responses_json_primary(
                        client,
                        model=model,
                        max_output_tokens=max_out,
                        system="You output only compact JSON for machine parsing. No markdown.",
                        user_text=user1,
                        file_ids=all_uploaded,
                        format_inner=None,
                    )
                else:
                    raise
            last_response_id = rid0
            if rid0:
                response_ids.append(rid0)
        except Exception:
            await _delete_openai_files(client, all_uploaded)
            raise
    
        patient_summary = str(parsed.get("patient_summary") or "").strip()
        comments = str(parsed.get("comments") or "").strip()
    
        by_scored: dict[str, dict[str, str]] = {}
        for it in parsed.get("scored_items") or []:
            row = _normalize_row(it)
            if row:
                by_scored[row["item_id"]] = row
    
        missing = scored_ids - set(by_scored.keys())
        if missing:
            log.warning("compliance rubric missing %d scored items; trying continuation repair", len(missing))
            missing_list = ", ".join(sorted(missing))
            cont_text = (
                "Your previous JSON omitted scored checklist rows. "
                f"Return ONLY JSON with key scored_items: one object per item_id in this exact set: {missing_list}. "
                "Each object: item_id, status (YES, NO, or NA), evidence. "
                f"{_EVIDENCE_RULES} "
                "Use the same transcript and rubric context from this conversation; do not invent item_ids."
            )
            cont_ok = False
            if last_response_id:
                try:
                    fmt_cont: dict[str, Any] | None = None
                    if settings.compliance_openai_structured_outputs:
                        fmt_cont = {
                            "type": "json_schema",
                            "name": "glp_scored_repair",
                            "strict": True,
                            "schema": _schema_scored_items_only(sorted(missing)),
                        }
                    try:
                        repair_c, rid_c = await _responses_json_continue(
                            client,
                            model=model,
                            max_output_tokens=max_out,
                            previous_response_id=last_response_id,
                            user_text=cont_text,
                            format_inner=fmt_cont,
                        )
                    except APIError as e:
                        if fmt_cont and _should_fallback_structured(e):
                            log.warning(
                                "compliance scored continuation: structured outputs unavailable (%s); json_object",
                                e,
                            )
                            repair_c, rid_c = await _responses_json_continue(
                                client,
                                model=model,
                                max_output_tokens=max_out,
                                previous_response_id=last_response_id,
                                user_text=cont_text,
                                format_inner=None,
                            )
                        else:
                            raise
                    _merge_scored(by_scored, repair_c, missing)
                    if rid_c:
                        last_response_id = rid_c
                        response_ids.append(rid_c)
                    cont_ok = len(scored_ids - set(by_scored.keys())) == 0
                except Exception as e:
                    log.warning("compliance scored continuation repair failed: %s", e)
    
            still = scored_ids - set(by_scored.keys())
            if not cont_ok or still:
                log.warning(
                    "compliance rubric scored file fallback (%d still missing after continuation)",
                    len(still),
                )
                miss_rules = {mid: _item_rule_blob(rubric, mid) for mid in sorted(still or missing)}
                repair_rules_bytes = json.dumps(miss_rules, ensure_ascii=False, indent=2).encode("utf-8")
                transcript_bytes2 = transcript_text.encode("utf-8")
                fb_uploaded: list[str] = []
                try:
                    fb_uploaded.append(
                        await _upload_user_data_file(client, filename="transcript.txt", data=transcript_bytes2)
                    )
                    fb_uploaded.append(
                        await _upload_user_data_file(client, filename="repair_item_rules.json", data=repair_rules_bytes)
                    )
                    all_uploaded.extend(fb_uploaded)
                    repair_text = (
                        'Return only JSON {"scored_items":[...]} — one object per missing checklist item.\n'
                        f"doctor_slug={doctor_slug!s} doctor_name={doctor_name!s}\n"
                        f"{_EVIDENCE_RULES}\n"
                        f"You MUST include exactly these item_id values (no others): {missing_list}\n"
                        "Attached: (1) transcript.txt — same call. "
                        "(2) repair_item_rules.json — yes_rule / no_rule / na_rule for each missing item_id only.\n"
                        f"{struct_note}\n"
                    )
                    ids_for_schema = sorted(still or missing)
                    fmt_fb: dict[str, Any] | None = None
                    if settings.compliance_openai_structured_outputs:
                        fmt_fb = {
                            "type": "json_schema",
                            "name": "glp_scored_fallback",
                            "strict": True,
                            "schema": _schema_scored_items_only(ids_for_schema),
                        }
                    try:
                        repair_f, rid_f = await _responses_json_primary(
                            client,
                            model=model,
                            max_output_tokens=max_out,
                            system=(
                                "Return only JSON with key scored_items: array of objects, each with "
                                "item_id, status (YES, NO, or NA), and evidence."
                            ),
                            user_text=repair_text,
                            file_ids=fb_uploaded,
                            format_inner=fmt_fb,
                        )
                    except APIError as e:
                        if fmt_fb and _should_fallback_structured(e):
                            log.warning(
                                "compliance scored file fallback: structured outputs unavailable (%s); json_object",
                                e,
                            )
                            repair_f, rid_f = await _responses_json_primary(
                                client,
                                model=model,
                                max_output_tokens=max_out,
                                system=(
                                    "Return only JSON with key scored_items: array of objects, each with "
                                    "item_id, status (YES, NO, or NA), and evidence."
                                ),
                                user_text=repair_text,
                                file_ids=fb_uploaded,
                                format_inner=None,
                            )
                        else:
                            raise
                    if rid_f:
                        last_response_id = rid_f
                        response_ids.append(rid_f)
                    bonus_continuation_ok = False
                    _merge_scored(by_scored, repair_f, still or missing)
                except Exception:
                    await _delete_openai_files(client, all_uploaded)
                    raise
    
        for mid in scored_ids:
            if mid not in by_scored:
                log.warning("compliance rubric defaulting missing item %s to NO", mid)
                by_scored[mid] = {
                    "item_id": mid,
                    "status": "NO",
                    "evidence": "missing_from_model",
                }
    
        by_bonus: dict[str, dict[str, str]] = {}
        for it in parsed.get("bonus_items") or []:
            row = _normalize_row(it)
            if row:
                by_bonus[row["item_id"]] = row
    
        missing_b = bonus_ids - set(by_bonus.keys())
        if missing_b:
            log.warning("compliance rubric missing %d bonus items; trying continuation repair", len(missing_b))
            missing_b_list = ", ".join(sorted(missing_b))
            b_cont = (
                "Your previous JSON omitted bonus counseling items. "
                f"Return ONLY JSON with key bonus_items: one object per item_id in this exact set: {missing_b_list}. "
                "Each object: item_id, status (YES, NO, or NA), evidence. "
                f"{_EVIDENCE_RULES} "
                "Use the same transcript and rubric from this conversation."
            )
            b_ok = False
            if bonus_continuation_ok and last_response_id:
                try:
                    fmt_b: dict[str, Any] | None = None
                    if settings.compliance_openai_structured_outputs:
                        fmt_b = {
                            "type": "json_schema",
                            "name": "glp_bonus_repair",
                            "strict": True,
                            "schema": _schema_bonus_items_only(sorted(missing_b)),
                        }
                    try:
                        repair_b, rid_b = await _responses_json_continue(
                            client,
                            model=model,
                            max_output_tokens=max_out,
                            previous_response_id=last_response_id,
                            user_text=b_cont,
                            format_inner=fmt_b,
                        )
                    except APIError as e:
                        if fmt_b and _should_fallback_structured(e):
                            log.warning(
                                "compliance bonus continuation: structured outputs unavailable (%s); json_object",
                                e,
                            )
                            repair_b, rid_b = await _responses_json_continue(
                                client,
                                model=model,
                                max_output_tokens=max_out,
                                previous_response_id=last_response_id,
                                user_text=b_cont,
                                format_inner=None,
                            )
                        else:
                            raise
                    _merge_bonus(by_bonus, repair_b, missing_b)
                    if rid_b:
                        last_response_id = rid_b
                        response_ids.append(rid_b)
                    b_ok = len(bonus_ids - set(by_bonus.keys())) == 0
                except Exception as e:
                    log.warning("compliance bonus continuation repair failed: %s", e)
    
            still_b = bonus_ids - set(by_bonus.keys())
            if not b_ok or still_b:
                log.warning(
                    "compliance rubric bonus file fallback (%d still missing after continuation)",
                    len(still_b),
                )
                bonus_compact = {"bonus_counseling_quality": compact.get("bonus_counseling_quality") or []}
                bonus_rules_bytes = json.dumps(bonus_compact, ensure_ascii=False, indent=2).encode("utf-8")
                transcript_bytes3 = transcript_text.encode("utf-8")
                b_uploaded: list[str] = []
                try:
                    b_uploaded.append(
                        await _upload_user_data_file(client, filename="transcript.txt", data=transcript_bytes3)
                    )
                    b_uploaded.append(
                        await _upload_user_data_file(client, filename="glp_bonus_rules.json", data=bonus_rules_bytes)
                    )
                    all_uploaded.extend(b_uploaded)
                    bonus_repair_text = (
                        'Return only JSON {"bonus_items":[...]} — one object per missing bonus item.\n'
                        f"doctor_slug={doctor_slug!s} doctor_name={doctor_name!s}\n"
                        f"{_EVIDENCE_RULES}\n"
                        f"You MUST include exactly these item_id values (no others): {missing_b_list}\n"
                        "Attached: (1) transcript.txt — same call. "
                        "(2) glp_bonus_rules.json — bonus_counseling_quality definitions.\n"
                        f"{struct_note}\n"
                    )
                    ids_b = sorted(still_b or missing_b)
                    fmt_bb: dict[str, Any] | None = None
                    if settings.compliance_openai_structured_outputs:
                        fmt_bb = {
                            "type": "json_schema",
                            "name": "glp_bonus_fallback",
                            "strict": True,
                            "schema": _schema_bonus_items_only(ids_b),
                        }
                    try:
                        repair_bonus, rid_b2 = await _responses_json_primary(
                            client,
                            model=model,
                            max_output_tokens=max_out,
                            system=(
                                "Return only JSON with key bonus_items: array of objects, each with "
                                "item_id, status (YES, NO, or NA), and evidence."
                            ),
                            user_text=bonus_repair_text,
                            file_ids=b_uploaded,
                            format_inner=fmt_bb,
                        )
                    except APIError as e:
                        if fmt_bb and _should_fallback_structured(e):
                            log.warning(
                                "compliance bonus file fallback: structured outputs unavailable (%s); json_object",
                                e,
                            )
                            repair_bonus, rid_b2 = await _responses_json_primary(
                                client,
                                model=model,
                                max_output_tokens=max_out,
                                system=(
                                    "Return only JSON with key bonus_items: array of objects, each with "
                                    "item_id, status (YES, NO, or NA), and evidence."
                                ),
                                user_text=bonus_repair_text,
                                file_ids=b_uploaded,
                                format_inner=None,
                            )
                        else:
                            raise
                    if rid_b2:
                        last_response_id = rid_b2
                        response_ids.append(rid_b2)
                    _merge_bonus(by_bonus, repair_bonus, still_b or missing_b)
                except Exception:
                    await _delete_openai_files(client, all_uploaded)
                    raise
    
        for mid in bonus_ids:
            if mid not in by_bonus:
                log.warning("compliance rubric defaulting missing bonus item %s to NO", mid)
                by_bonus[mid] = {"item_id": mid, "status": "NO", "evidence": "missing_from_model"}
    
        await _delete_openai_files(client, all_uploaded)
    
        status_for_math = {row["item_id"]: row["status"] for row in by_scored.values()}
        det = apply_deterministic_scores(rubric, status_for_math)
    
        scored_list = [by_scored[i] for i in ROLLUP_SCORED_ITEM_IDS if i in by_scored]
        bonus_list = [by_bonus[i] for i in bonus_ids_in_rubric_order(rubric) if i in by_bonus]
        items_flat: list[dict[str, str]] = []
        for x in scored_list:
            items_flat.append({"item_id": x["item_id"], "status": x["status"], "evidence": x.get("evidence", "")})
        for x in bonus_list:
            items_flat.append({"item_id": x["item_id"], "status": x["status"], "evidence": x.get("evidence", "")})
    
        scored_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    
        return {
            "rubric_version": (rubric_version or settings.compliance_rubric_version or "glp1").strip(),
            "rubric_authority_version": authority_version,
            "rubric_source_path": str(path),
            "model": model,
            "patient_summary": patient_summary,
            "comments": comments,
            "scored_at": scored_at,
            "composite_pct": det["composite_pct"],
            "grade": det["grade"],
            "grade_label": det["grade_label"],
            "domain_pcts": det["domain_pcts"],
            "doctor_name": doctor_name,
            "items": items_flat,
            "scored_items": scored_list,
            "bonus_counseling_items": bonus_list,
            "openai_response_ids": response_ids,
        }
