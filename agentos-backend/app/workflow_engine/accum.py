"""Pipeline context: dotted paths + {{ template }} substitution."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


def lookup_path(accum: dict[str, Any], path: str) -> Any:
    """Resolve `input.x` or `step_id.field` against accum {input, step outputs by id}."""
    path = (path or "").strip()
    if not path:
        raise KeyError("empty path")
    parts = path.split(".")
    cur: Any = accum
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            raise KeyError(path)
    return cur


def resolve_templates(s: str, accum: dict[str, Any]) -> str:
    """Replace {{ dotted.path }} with stringified values."""

    def repl(m: re.Match[str]) -> str:
        key = m.group(1).strip()
        try:
            v = lookup_path(accum, key)
            if v is None:
                return ""
            return str(v)
        except KeyError:
            return m.group(0)

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, s)


def to_decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if v is None:
        raise ValueError("missing number")
    s = str(v).strip().replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"not a number: {v!r}") from e
