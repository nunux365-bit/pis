"""Lazy spaCy model provisioning for Presidio (detect + download on first use)."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_ensured: set[str] = set()


def spacy_model_installed(model_name: str) -> bool:
    name = (model_name or "").strip()
    if not name:
        return False
    try:
        import spacy.util

        return spacy.util.is_package(name)
    except Exception:
        return False


def ensure_spacy_model(model_name: str, *, auto_download: bool = True) -> None:
    """Ensure the spaCy model package exists; download once if missing."""
    name = (model_name or "").strip()
    if not name:
        raise ValueError("spacy model name is required")

    if name in _ensured and spacy_model_installed(name):
        return

    with _lock:
        if name in _ensured and spacy_model_installed(name):
            return

        if spacy_model_installed(name):
            _ensured.add(name)
            return

        if not auto_download:
            raise OSError(
                f"spaCy model '{name}' is not installed "
                f"(install it or set RESPONDER_EVAL_SPACY_AUTO_DOWNLOAD=true)"
            )

        log.info("spaCy model %s not found; downloading (one-time)", name)
        from spacy.cli import download as spacy_download

        spacy_download(name)
        if not spacy_model_installed(name):
            raise OSError(f"spaCy model '{name}' download completed but package is still missing")

        _ensured.add(name)
        log.info("spaCy model %s ready", name)


def reset_spacy_model_cache_for_tests() -> None:
    _ensured.clear()
