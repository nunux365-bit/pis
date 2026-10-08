"""Derive a short doctor identifier from recording filenames (best-effort, not clinical PHI)."""

from __future__ import annotations

import re


def doctor_slug_from_filename(filename: str) -> str:
    """
    Examples:
      ``Copy of Dr Duggirala....m4a`` → ``duggirala``
      ``Dr._Smith_visit.mp3`` → ``smith``
    """
    base = (filename or "").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    s = stem.replace("_", " ").replace("-", " ").strip()
    # Prefer "Dr <Name>" or "Dr. <Name>"
    m = re.search(r"(?i)\bdr\.?\s+([A-Za-z][A-Za-z'\-]+)", s)
    if m:
        return m.group(1).lower()
    # Fallback: first alphabetic token
    for tok in re.split(r"\s+", s):
        t = re.sub(r"[^A-Za-z]", "", tok)
        if len(t) >= 2:
            return t.lower()
    return "unknown"


def doctor_name_from_filename(filename: str) -> str:
    """
    Canonical display name per GLP rubric ``doctor_name_extraction`` (filename-based).

    Examples:
      ``Copy of Dr Duggirala..mpeg`` → ``Dr Duggirala``
      ``Copy of Dr jitendra.mpeg`` → ``Dr jitendra``
    """
    base = (filename or "").rsplit("/", 1)[-1].strip()
    m = re.match(r"(?i)^Copy\s+of\s+(.+)\.(mpeg|mp3|m4a|wav)$", base)
    if m:
        return m.group(1).strip().rstrip(".")
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip() or "unknown"
