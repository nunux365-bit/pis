"""Infer clinician vs patient Deepgram speaker IDs using async OpenAI (Deepgram only assigns S0, S1, …)."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import APIError, AsyncOpenAI

from app.agents.compliance_call.api_retry import async_call_with_retry, retryable_openai_error
from app.config.settings import settings

log = logging.getLogger(__name__)


def _parse_json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        log.warning("speaker_roles: invalid JSON from model (truncated?)")
        return {}


def _roles_model() -> str:
    m = (settings.compliance_speaker_roles_model or "").strip()
    if m:
        return m
    return (settings.compliance_openai_model or settings.openai_chat_model or "gpt-4o-mini").strip()


def _roles_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "clinician_speaker": {"type": "integer"},
            "patient_speaker": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "rationale": {"type": "string"},
        },
        "required": ["clinician_speaker", "patient_speaker", "confidence", "rationale"],
        "additionalProperties": False,
    }


def _validate_roles(secs: dict[int, float], clin: int, pat: int) -> bool:
    if clin == pat:
        return False
    if clin not in secs or pat not in secs:
        return False
    return True


async def infer_clinician_patient_speakers(
    *,
    speaker_seconds: dict[int, float],
    utterance_snippet: str,
    doctor_name_hint: str,
    openai_client: AsyncOpenAI | None = None,
) -> dict[str, Any] | None:
    """
    Returns a dict suitable for ``deepgram_summary["speaker_roles"]``, or None if skipped/failed.

    Deepgram provides speaker clusters only; this step maps two IDs to clinician vs patient
    using transcript context (best-effort, not ground truth).
    """
    if not settings.compliance_infer_speaker_roles:
        return None
    key = (settings.openai_api_key or "").strip()
    if not key:
        log.warning("speaker_roles: OPENAI_API_KEY missing; skipping inference")
        return None
    if len(speaker_seconds) < 2:
        return None

    stats = {str(k): round(v, 2) for k, v in sorted(speaker_seconds.items())}
    model = _roles_model()
    sys_msg = (
        "You label diarized speakers in a telehealth GLP-1 consultation audio transcript. "
        "Speaker IDs (S0, S1, …) come from automatic clustering — assign clinician vs patient "
        "from dialogue roles (who asks clinical questions, prescribes, vs who receives counseling). "
        "Use the filename doctor hint only as weak context. Output JSON only per schema."
    )
    user_msg = (
        f"doctor_name_hint={doctor_name_hint!s}\n"
        f"seconds_per_speaker={json.dumps(stats)}\n\n"
        "Utterance transcript with [Sn] speaker prefixes (may be truncated):\n---\n"
        f"{(utterance_snippet or '')[:8000]}\n---"
    )

    fmt = {
        "type": "json_schema",
        "name": "speaker_roles",
        "strict": True,
        "schema": _roles_schema(),
    }

    async def _infer_with_client(client: AsyncOpenAI) -> tuple[dict[str, Any], str | None]:
        async def _go() -> tuple[dict[str, Any], str | None]:
            resp = await client.responses.create(
                model=model,
                temperature=0,
                max_output_tokens=512,
                input=[
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
                text={"format": fmt},
            )
            raw = (getattr(resp, "output_text", None) or "").strip()
            rid = getattr(resp, "id", None)
            out = _parse_json_object(raw)
            return out, (str(rid) if rid else None)

        return await async_call_with_retry(
            "openai.speaker_roles",
            _go,
            is_retryable=retryable_openai_error,
        )

    async def _run_roles(client: AsyncOpenAI) -> tuple[dict[str, Any], str | None]:
        try:
            parsed, rid = await _infer_with_client(client)
        except APIError as e:
            log.warning("speaker_roles: structured response failed (%s); retry json_object", e)

            async def _go2() -> tuple[dict[str, Any], str | None]:
                resp = await client.responses.create(
                    model=model,
                    temperature=0,
                    max_output_tokens=512,
                    input=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    text={"format": {"type": "json_object"}},
                )
                raw = (getattr(resp, "output_text", None) or "").strip()
                rid2 = getattr(resp, "id", None)
                out = _parse_json_object(raw)
                return out, (str(rid2) if rid2 else None)

            parsed, rid = await async_call_with_retry(
                "openai.speaker_roles(fallback)",
                _go2,
                is_retryable=retryable_openai_error,
            )
        return parsed, rid

    try:
        rid: str | None = None
        if openai_client is not None:
            parsed, rid = await _run_roles(openai_client)
        else:
            async with AsyncOpenAI(api_key=key, timeout=120.0) as client:
                parsed, rid = await _run_roles(client)
    except Exception:
        if openai_client is not None:
            raise
        log.exception("speaker_roles: inference failed")
        return None

    try:
        clin = int(parsed.get("clinician_speaker"))
        pat = int(parsed.get("patient_speaker"))
    except (TypeError, ValueError):
        return None
    if not _validate_roles(speaker_seconds, clin, pat):
        log.warning(
            "speaker_roles: invalid ids clin=%s pat=%s seconds=%s",
            clin,
            pat,
            list(speaker_seconds.keys()),
        )
        return None

    conf = str(parsed.get("confidence") or "low").strip().lower()
    if conf not in ("low", "medium", "high"):
        conf = "low"
    rationale = str(parsed.get("rationale") or "").strip()[:2000]

    out: dict[str, Any] = {
        "clinician_speaker": clin,
        "patient_speaker": pat,
        "confidence": conf,
        "rationale": rationale,
        "source": "openai",
        "model": model,
    }
    if rid:
        out["openai_response_id"] = rid
    return out
