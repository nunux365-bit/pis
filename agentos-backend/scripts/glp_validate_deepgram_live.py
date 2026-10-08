#!/usr/bin/env python3
"""
Live Deepgram STT vs saved reference ``*.deepgram.json`` / ``*.txt``.

For each ``manifest.json`` entry: normalize the **same** source audio as the reference
pipeline, call Deepgram with **current** ``settings`` (model, diarize, keyterms, etc.),
then compare the canonical transcript string to the reference.

Same audio + identical API options should yield the same transcript; drift usually means
different model version, account defaults, or query params (see printed ``query_params``).

Usage::

  cd agentos-backend && PYTHONPATH=. python scripts/glp_validate_deepgram_live.py \\
    --transcripts-dir /path/to/_transcripts-deepgram

Requires ``COMPLIANCE_DEEPGRAM_API_KEY`` and ``ffmpeg`` on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.compliance_call.adapters.normalize_ffmpeg import normalize_audio_file  # noqa: E402
from app.agents.compliance_call.adapters.transcribe_deepgram import (  # noqa: E402
    _deepgram_listen_query_params,
    canonical_transcript_from_deepgram_response,
    transcribe_wav_to_result,
)
from app.config.settings import settings  # noqa: E402


def _ref_json_path(transcripts_dir: Path, rel_json: str) -> Path:
    return transcripts_dir / Path(rel_json).name


def _similarity(a: str, b: str) -> float:
    return round(SequenceMatcher(a=a, b=b).ratio(), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts-dir", type=Path, required=True)
    ap.add_argument(
        "--min-similarity",
        type=float,
        default=0.999,
        help="Flag mismatch if SequenceMatcher ratio is below this (1.0 = identical).",
    )
    args = ap.parse_args()

    td = args.transcripts_dir.expanduser().resolve()
    if not (settings.compliance_deepgram_api_key or "").strip():
        print("ERROR: COMPLIANCE_DEEPGRAM_API_KEY is not set.", file=sys.stderr)
        return 1

    manifest = json.loads((td / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files") or []

    params = _deepgram_listen_query_params()
    query_params = dict(params)
    out: dict[str, Any] = {
        "deepgram_model": (settings.compliance_deepgram_model or "nova-3-medical").strip(),
        "compliance_deepgram_diarize": settings.compliance_deepgram_diarize,
        "query_params": query_params,
        "files": [],
        "all_match_exact": True,
        "all_above_threshold": True,
    }

    for ent in files:
        rel_json = str(ent.get("json") or "")
        src = Path(str(ent.get("source") or "")).expanduser()
        jpath = _ref_json_path(td, rel_json)
        if not jpath.is_file():
            out["files"].append({"error": f"missing reference json {jpath}"})
            out["all_match_exact"] = False
            out["all_above_threshold"] = False
            continue
        if not src.is_file():
            out["files"].append({"error": f"missing audio {src}", "reference_json": jpath.name})
            out["all_match_exact"] = False
            out["all_above_threshold"] = False
            continue

        ref_payload = json.loads(jpath.read_text(encoding="utf-8"))
        expected = canonical_transcript_from_deepgram_response(ref_payload)
        ref_dur = float((ref_payload.get("metadata") or {}).get("duration") or 0.0)

        wav: Path | None = None
        try:
            wav = normalize_audio_file(src, loudnorm=settings.compliance_ffmpeg_loudnorm)
            live = asyncio.run(transcribe_wav_to_result(wav))
        except Exception as e:
            out["files"].append(
                {
                    "source": str(src),
                    "reference_json": jpath.name,
                    "error": f"{type(e).__name__}: {e}"[:500],
                }
            )
            out["all_match_exact"] = False
            out["all_above_threshold"] = False
            continue
        finally:
            if wav is not None:
                wav.unlink(missing_ok=True)

        actual = str(live.get("transcript_text") or "").strip()
        dg = live.get("deepgram_summary") if isinstance(live.get("deepgram_summary"), dict) else {}
        live_dur = float(dg.get("duration_sec") or 0.0)
        exact = actual == expected
        sim = _similarity(actual, expected)
        dur_delta = round(abs(ref_dur - live_dur), 3)

        row = {
            "source": src.name,
            "reference_json": jpath.name,
            "expected_chars": len(expected),
            "live_chars": len(actual),
            "transcript_exact_match": exact,
            "similarity": sim,
            "ref_duration_sec": ref_dur,
            "live_duration_sec": live_dur,
            "duration_delta_sec": dur_delta,
        }
        if not exact:
            out["all_match_exact"] = False
        if sim < args.min_similarity:
            out["all_above_threshold"] = False
            row["below_threshold"] = True
        # First diff snippet
        if not exact and expected and actual:
            for i, (ca, cb) in enumerate(zip(expected, actual)):
                if ca != cb:
                    row["first_diff_at"] = i
                    row["expected_snip"] = expected[i : i + 80]
                    row["actual_snip"] = actual[i : i + 80]
                    break
            else:
                if len(expected) != len(actual):
                    row["first_diff_at"] = min(len(expected), len(actual))
        out["files"].append(row)

    print(json.dumps(out, indent=2))
    return 0 if out["all_above_threshold"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
