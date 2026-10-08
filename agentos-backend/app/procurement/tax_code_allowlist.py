"""Procurement tax-code picker allowlist (finance curated; SAP sync keeps full catalogue)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.sql.elements import ColumnElement

log = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "tax_code_allowlist.json"


@lru_cache(maxsize=1)
def tax_code_allowlist_codes() -> frozenset[str]:
    """SAP tax codes permitted in UI picker and create/update validation."""
    path = _DATA_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    codes = raw.get("codes")
    if not isinstance(codes, list) or not codes:
        raise ValueError(f"{path}: expected non-empty 'codes' array")
    out: set[str] = set()
    for c in codes:
        s = str(c or "").strip()
        if s:
            out.add(s)
    if not out:
        raise ValueError(f"{path}: no valid tax codes")
    return frozenset(out)


def tax_code_is_allowed(code: str) -> bool:
    return str(code or "").strip() in tax_code_allowlist_codes()


def tax_code_allowlist_predicate(code_column: Any) -> ColumnElement[bool]:
    """SQLAlchemy ``IN`` filter for reference list/search on ``tax_code`` domain."""
    return code_column.in_(sorted(tax_code_allowlist_codes()))
