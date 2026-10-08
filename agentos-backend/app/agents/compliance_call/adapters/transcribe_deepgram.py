"""Deepgram prerecorded STT — canonical transcript + compact summary for storage.

Uses the official **async** Deepgram SDK (:class:`deepgram.AsyncDeepgramClient`) with the same
listen options as ``frontend/scripts/transcribe-deepgram-india-once.mjs`` so canonical transcripts
stay comparable to reference ``*.txt`` files.

We **do not** persist the full Deepgram JSON on ``workflow_runs``. The graph stores
``transcript_text`` plus a small ``deepgram_summary`` (duration, request id, structural metrics).

Call :func:`transcribe_wav_to_result` with ``await`` from async LangGraph nodes or other asyncio
contexts (native async I/O — no thread offload required).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from deepgram import AsyncDeepgramClient
from deepgram.core.request_options import RequestOptions
from openai import AsyncOpenAI

from app.agents.compliance_call.api_retry import (
    async_call_with_retry,
    retryable_deepgram_error,
    vendor_client_refresh_recommended,
)
from app.agents.compliance_call.adapters.speaker_roles_openai import infer_clinician_patient_speakers
from app.agents.compliance_call.glp_scoring import (
    asr_word_quality_from_words,
    deepgram_summary_for_persist,
    merge_clinician_patient_into_structural,
    speaker_seconds_from_payload,
    structural_from_deepgram,
)
from app.agents.compliance_call.vendor_clients import (
    close_openai_client_safely,
    create_compliance_openai_client,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

# Deepgram redaction: repeated ``redact=`` query params (SDK list → encode_query).
_DEEPGRAM_NAME_ENTITY_REDACTS = ("name", "name_given", "name_family", "name_medical_professional")


def _merge_deepgram_redact_into_listen(*, extra_listen_kwargs: dict[str, Any], aq: dict[str, Any]) -> None:
    raw = (settings.compliance_deepgram_redact or "").strip().lower()
    if not raw or raw in ("off", "none", "false", "0"):
        return
    if raw == "names":
        aq["redact"] = list(_DEEPGRAM_NAME_ENTITY_REDACTS)
        return
    if raw == "pii":
        extra_listen_kwargs["redact"] = "pii"
        return
    extra_listen_kwargs["redact"] = raw


def words_from_deepgram_channel0(payload: dict[str, Any]) -> list[Any]:
    """Word objects from first channel alternative (confidence per token)."""
    ch = (payload.get("results") or {}).get("channels") or []
    if not ch:
        return []
    alts = (ch[0] or {}).get("alternatives") or []
    if not alts:
        return []
    w = (alts[0] or {}).get("words")
    return w if isinstance(w, list) else []


def utterance_labeled_transcript(payload: dict[str, Any]) -> str:
    """Speaker-prefixed lines when utterances are present (helps attribution in rubric)."""
    ut = (payload.get("results") or {}).get("utterances") or []
    if not isinstance(ut, list) or not ut:
        return ""
    lines: list[str] = []
    for u in ut:
        if not isinstance(u, dict):
            continue
        t = (u.get("transcript") or "").strip()
        if not t:
            continue
        sp = u.get("speaker")
        if sp is None or sp == "":
            lines.append(t)
        else:
            lines.append(f"[S{sp}] {t}")
    return "\n".join(lines).strip()


def _grading_transcript_choice(*, canonical: str, payload: dict[str, Any]) -> str:
    mode = (settings.compliance_grading_transcript_source or "utterances").strip().lower()
    if mode == "utterances":
        alt = utterance_labeled_transcript(payload)
        if alt:
            return alt
    return canonical.strip()


def canonical_transcript_from_deepgram_response(payload: dict[str, Any]) -> str:
    """Extract the same string the reference script uses for ``*.txt`` files."""
    ch = (payload.get("results") or {}).get("channels") or []
    if not ch:
        return ""
    alts = (ch[0] or {}).get("alternatives") or []
    if not alts:
        return ""
    raw = alts[0].get("transcript")
    return (raw or "").strip()


def _deepgram_listen_query_params() -> list[tuple[str, str]]:
    """Human-readable query pairs (logging / validation scripts — mirrors REST params)."""
    model = (settings.compliance_deepgram_model or "nova-3-medical").strip()
    pairs: list[tuple[str, str]] = [
        ("model", model),
        ("language", "multi"),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("numerals", "true"),
        ("paragraphs", "true"),
        ("utterances", "true"),
        ("diarize", "true" if settings.compliance_deepgram_diarize else "false"),
    ]
    raw_kt = (settings.compliance_deepgram_keyterms or "").strip()
    if not raw_kt and (os.environ.get("DEEPGRAM_KEYTERMS") or "").strip():
        raw_kt = (os.environ.get("DEEPGRAM_KEYTERMS") or "").strip()
    for term in (t.strip() for t in raw_kt.split(",") if t.strip()):
        pairs.append(("keyterm", term))
    if settings.compliance_deepgram_diarize:
        mx = int(settings.compliance_deepgram_max_speakers or 0)
        if mx > 0:
            pairs.append(("max_speakers", str(mx)))
    mode = (settings.compliance_deepgram_redact or "").strip().lower()
    if not mode or mode in ("off", "none", "false", "0"):
        pass
    elif mode == "names":
        for ent in _DEEPGRAM_NAME_ENTITY_REDACTS:
            pairs.append(("redact", ent))
    elif mode == "pii":
        pairs.append(("redact", "pii"))
    else:
        pairs.append(("redact", mode))
    return pairs


def _payload_from_listen_response(body: Any) -> dict[str, Any]:
    """Normalize SDK model to the loose dict shape used by scoring helpers."""
    dump = body.model_dump(mode="json")
    if isinstance(dump, dict):
        return dump
    raise RuntimeError("Deepgram listen response could not be converted to dict")


async def transcribe_wav_to_result(
    wav_path: Path,
    *,
    timeout_sec: float = 600.0,
    doctor_name_hint: str = "",
    deepgram_client: AsyncDeepgramClient | None = None,
    openai_client: AsyncOpenAI | None = None,
) -> dict[str, Any]:
    """
    Sends WAV to Deepgram via ``AsyncDeepgramClient``; returns ``transcript_text`` and a small
    ``deepgram_summary`` for persistence (no full vendor JSON blob).
    """
    key = (settings.compliance_deepgram_api_key or "").strip()
    if not key:
        raise RuntimeError("COMPLIANCE_DEEPGRAM_API_KEY is not set")

    data = wav_path.read_bytes()
    model = (settings.compliance_deepgram_model or "nova-3-medical").strip()
    raw_kt = (settings.compliance_deepgram_keyterms or "").strip()
    if not raw_kt and (os.environ.get("DEEPGRAM_KEYTERMS") or "").strip():
        raw_kt = (os.environ.get("DEEPGRAM_KEYTERMS") or "").strip()
    keyterm_list = [t.strip() for t in raw_kt.split(",") if t.strip()] or None
    diarize = settings.compliance_deepgram_diarize

    extra_listen_kwargs: dict[str, Any] = {}
    aq: dict[str, Any] = {}
    if diarize:
        mx = int(settings.compliance_deepgram_max_speakers or 0)
        if mx > 0:
            aq["max_speakers"] = str(mx)
    _merge_deepgram_redact_into_listen(extra_listen_kwargs=extra_listen_kwargs, aq=aq)

    req_opts: RequestOptions | None = None
    if aq:
        req_opts = {"additional_query_parameters": aq}

    dg = deepgram_client or AsyncDeepgramClient(api_key=key, timeout=timeout_sec)

    async def _listen_once():
        kwargs: dict[str, Any] = {
            "request": data,
            "model": model,
            "language": "multi",
            "smart_format": True,
            "punctuate": True,
            "numerals": True,
            "paragraphs": True,
            "utterances": True,
            "diarize": diarize,
            "request_options": req_opts,
            **extra_listen_kwargs,
        }
        if keyterm_list:
            kwargs["keyterm"] = keyterm_list
        http_resp = await dg.listen.v1.media.transcribe_file(**kwargs)  # type: ignore[arg-type]
        # ``ListenV1Response`` is the body (metadata + results); there is no ``.data``.
        return _payload_from_listen_response(http_resp)

    payload = await async_call_with_retry(
        "deepgram.listen.transcribe_file",
        _listen_once,
        is_retryable=retryable_deepgram_error,
    )

    text = canonical_transcript_from_deepgram_response(payload)
    if not text:
        log.warning("deepgram returned empty transcript (request ok); check audio/model")

    grading = _grading_transcript_choice(canonical=text, payload=payload)
    struct = structural_from_deepgram(payload, diarize=diarize)
    secs = speaker_seconds_from_payload(payload)
    roles_doc = None
    oai_for_roles = openai_client
    if settings.compliance_infer_speaker_roles and diarize and len(secs) >= 2:
        snippet = utterance_labeled_transcript(payload)
        if openai_client is not None:
            for attempt in range(2):
                try:
                    roles_doc = await infer_clinician_patient_speakers(
                        speaker_seconds=secs,
                        utterance_snippet=snippet,
                        doctor_name_hint=(doctor_name_hint or "").strip(),
                        openai_client=oai_for_roles,
                    )
                    break
                except Exception as e:
                    if attempt == 0 and vendor_client_refresh_recommended(e, vendor="openai"):
                        log.warning(
                            "speaker_roles: refreshing OpenAI client after %s: %s",
                            type(e).__name__,
                            e,
                        )
                        try:
                            replacement = create_compliance_openai_client(timeout_sec=120.0)
                        except Exception:
                            log.exception("speaker_roles: failed to recreate OpenAI client")
                            roles_doc = None
                            break
                        await close_openai_client_safely(oai_for_roles)
                        oai_for_roles = replacement
                        continue
                    log.warning(
                        "speaker_roles: inference failed after retries (%s): %s",
                        type(e).__name__,
                        e,
                    )
                    roles_doc = None
                    break
        else:
            roles_doc = await infer_clinician_patient_speakers(
                speaker_seconds=secs,
                utterance_snippet=snippet,
                doctor_name_hint=(doctor_name_hint or "").strip(),
                openai_client=None,
            )
        struct = merge_clinician_patient_into_structural(struct, secs, roles_doc)
    words = words_from_deepgram_channel0(payload)
    wq = asr_word_quality_from_words(words)
    summary = deepgram_summary_for_persist(payload, struct, asr_word_quality=wq)
    if roles_doc:
        summary["speaker_roles"] = roles_doc
    out: dict[str, Any] = {
        "transcript_text": text,
        "grading_transcript": grading,
        "deepgram_summary": summary,
    }
    if openai_client is not None and oai_for_roles is not openai_client:
        out["openai_client"] = oai_for_roles
    return out


async def transcribe_file_to_text_async(wav_path: Path, *, timeout_sec: float = 600.0) -> str:
    """Canonical trimmed transcript only (async)."""
    tr = await transcribe_wav_to_result(wav_path, timeout_sec=timeout_sec)
    return str(tr.get("transcript_text") or "")


def transcribe_file_to_text(wav_path: Path, *, timeout_sec: float = 600.0) -> str:
    """Sync entrypoint for CLI scripts outside an event loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(transcribe_wav_to_result(wav_path, timeout_sec=timeout_sec))[
            "transcript_text"
        ]
    raise RuntimeError(
        "transcribe_file_to_text cannot run inside an active event loop; "
        "await transcribe_wav_to_result(...) instead."
    )
