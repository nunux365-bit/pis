"""FFmpeg normalization for STT — 16 kHz mono PCM WAV (batch v1)."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def normalize_audio_file(
    input_path: Path,
    *,
    loudnorm: bool = False,
    streaming_mode: bool = False,
) -> Path:
    """
    Returns path to a temp ``.wav`` file. Caller must unlink.

    ``streaming_mode`` reserved for chunk-friendly policy (v1 same as batch).
    """
    _ = streaming_mode  # future: shorter blocks / different loudnorm
    fd, out = tempfile.mkstemp(prefix="cc_norm_", suffix=".wav")
    import os

    os.close(fd)
    out_path = Path(out)
    af = []
    if loudnorm:
        af = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        *af,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        out_path.unlink(missing_ok=True)
        err = (e.stderr or e.stdout or "")[:2000]
        raise RuntimeError(f"ffmpeg failed: {err}") from e
    return out_path
