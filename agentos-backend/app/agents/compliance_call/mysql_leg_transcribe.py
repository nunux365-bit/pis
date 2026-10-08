"""Download, normalize, and transcribe MySQL call legs; merge transcripts in time order."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agents.compliance_call.adapters.normalize_ffmpeg import normalize_audio_file
from app.agents.compliance_call.adapters.telephony_ingest_media import resolve_mysql_call_media_file
from app.agents.compliance_call.adapters.transcribe_deepgram import transcribe_wav_to_result
from app.agents.compliance_call.mysql_conversation import (
    leg_sort_key,
    merge_deepgram_summaries,
    merge_transcript_parts,
)
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)


def _cleanup_paths(*paths: str | None) -> None:
    for p in paths:
        if not p:
            continue
        try:
            Path(str(p)).unlink(missing_ok=True)
        except Exception:
            pass


async def transcribe_mysql_legs_merged(
    legs: list[dict[str, Any]],
    *,
    doctor_name: str,
    deepgram_client: Any,
    openai_client: Any,
) -> dict[str, Any]:
    """Process each leg; skip failed/empty; return merged transcript fields."""
    ordered = sorted(legs, key=leg_sort_key)
    parts: list[tuple[int, str, str]] = []
    summaries: list[dict[str, Any]] = []
    used_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    oai = openai_client

    for leg in ordered:
        cid = int(leg.get("mysql_call_id") or 0)
        if cid <= 0:
            continue
        media_path: str | None = None
        wav_path: str | None = None
        try:
            path = await resolve_mysql_call_media_file(
                room_name=leg.get("mysql_room_name") or None,
                metadata_raw=leg.get("mysql_metadata"),
            )
            media_path = str(path)

            def _norm() -> Path:
                return normalize_audio_file(
                    Path(media_path),
                    loudnorm=settings.compliance_ffmpeg_loudnorm,
                    streaming_mode=False,
                )

            wav = await run_blocking(_norm)
            wav_path = str(wav)

            tr = await transcribe_wav_to_result(
                Path(wav_path),
                doctor_name_hint=doctor_name,
                deepgram_client=deepgram_client,
                openai_client=oai,
            )
            refreshed = tr.get("openai_client")
            if refreshed is not None:
                oai = refreshed

            canon = str(tr.get("transcript_text") or "").strip()
            grading = str(tr.get("grading_transcript") or canon).strip()
            if not canon and not grading:
                skipped.append({"mysql_call_id": cid, "reason": "empty_transcript"})
                continue

            dg = tr.get("deepgram_summary")
            if isinstance(dg, dict) and dg:
                summaries.append(dg)

            parts.append((cid, canon, grading))
            used_ids.append(cid)
            log.info("compliance mysql leg ok call_id=%s canon_chars=%d", cid, len(canon))
        except Exception as e:
            log.warning(
                "compliance mysql leg skipped call_id=%s: %s: %s",
                cid,
                type(e).__name__,
                e,
            )
            skipped.append({"mysql_call_id": cid, "reason": f"{type(e).__name__}: {e}"[:500]})
        finally:
            _cleanup_paths(media_path, wav_path)

    if not parts:
        raise RuntimeError(
            f"mysql_conversation_merge: no usable legs (skipped={len(skipped)} legs={len(ordered)})"
        )

    canon_m, grad_m = merge_transcript_parts(parts)
    return {
        "transcript_text": canon_m,
        "grading_transcript": grad_m,
        "deepgram_summary": merge_deepgram_summaries(summaries),
        "mysql_merged_call_ids": used_ids,
        "mysql_skipped_call_ids": skipped,
        "openai_client": oai,
    }
