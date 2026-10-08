"""
Validate step of ``product_flow.CONTRACT_INGEST_STAGES``: server-side JSON Schema check
after LLM output and file-truth normalization (see ``pipeline.process_one_contract_pdf``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema
from jsonschema import Draft202012Validator

_PKG = Path(__file__).resolve().parent


@lru_cache
def _schema() -> dict:
    p = _PKG / "schemas" / "contract_ingestion_schema.json"
    return json.loads(p.read_text(encoding="utf-8"))


def validate_contract_payload(payload: dict, *, max_errors: int = 60) -> list[str]:
    """Return list of human-readable errors (empty if valid)."""
    validator = Draft202012Validator(_schema())
    errs: list[str] = []
    for i, e in enumerate(
        sorted(validator.iter_errors(payload), key=lambda x: list(x.absolute_path))
    ):
        if i >= max_errors:
            errs.append(f"... and more (stopped at {max_errors} messages)")
            break
        loc = "/".join(str(x) for x in e.absolute_path) if e.absolute_path else "(root)"
        errs.append(f"{loc}: {e.message}")
    return errs
