"""Tiny predicate DSL used by row filters, decision gates, and recipient rules.

A node is a JSON-compatible ``dict``. Supported ops — by design, this is a closed
set. New operations require a code change and a unit test:

============ =================================================================
 ``eq``       ``{op, col, value}``                     — equality (str-folded)
 ``ne``       ``{op, col, value}``                     — negation of ``eq``
 ``in``       ``{op, col, values: [...]}``             — membership
 ``not_in``   ``{op, col, values: [...]}``             — negation of ``in``
 ``gt`` / ``lt`` / ``gte`` / ``lte``
              ``{op, col, value}``                     — Decimal-safe compares
 ``is_empty`` / ``is_not_empty``
              ``{op, col}``                            — null / '' check
 ``regex``    ``{op, col, pattern, flags?}``           — ``re.search``
 ``sum_gt`` / ``sum_gte`` / ``sum_lt`` / ``sum_lte``
              ``{op, cols: [...], value}``             — Decimal sum across cols
 ``and`` / ``or`` / ``not``
              ``{op, terms: [node, ...]}``             — logical combinators
============ =================================================================

``col`` accepts either a plain header string or ``{"name": str, "occurrence": int}``
to address duplicate-header sheets deterministically. The evaluator never looks
outside the ``row`` / ``context`` dicts it's given — no IO, no LLM, no wall-clock.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from ._decimal import to_decimal as _to_decimal
from .types import ColumnRef


class DSLError(ValueError):
    """Raised for malformed predicate nodes."""


def col_key(col: ColumnRef) -> str:
    """Normalize a :data:`ColumnRef` to the canonical lookup key used by the reader."""

    if isinstance(col, str):
        name = _norm_header(col)
        return f"{name}#0" if name else ""
    if isinstance(col, Mapping):
        name = _norm_header(str(col.get("name") or ""))
        occ = int(col.get("occurrence") or 0)
        return f"{name}#{occ}"
    raise DSLError(f"Unsupported column ref: {col!r}")


def _norm_header(s: str) -> str:
    # Normalize NBSP, strip, collapse whitespace, casefold.
    s = s.replace("\u00a0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def _get(row: Mapping[str, Any], col: ColumnRef) -> Any:
    return row.get(col_key(col))


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return _norm_str(a) == _norm_str(b)
    # numeric-friendly equality
    da, db = _to_decimal(a), _to_decimal(b)
    if da is not None and db is not None:
        return da == db
    return a == b


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    s = s.replace("\u00a0", " ").strip()
    return s.casefold()


def _cmp(op: str, a: Any, b: Any) -> bool:
    da, db = _to_decimal(a), _to_decimal(b)
    if da is None or db is None:
        return False
    if op == "gt":
        return da > db
    if op == "gte":
        return da >= db
    if op == "lt":
        return da < db
    if op == "lte":
        return da <= db
    raise DSLError(f"bad compare op: {op}")


def evaluate(node: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Evaluate a predicate ``node`` against a normalized ``row`` dict.

    ``row`` keys must be canonical (``{norm_header}#{occurrence}``) — the normalizer
    produces them. All comparisons are folded for case / whitespace on strings and
    Decimal for numbers so the DSL is robust to upstream formatting wobble.
    """

    op = node.get("op")
    if op in ("and", "or", "not"):
        terms = node.get("terms")
        if not isinstance(terms, list) or (op == "not" and len(terms) != 1) or (op != "not" and not terms):
            raise DSLError(f"{op} requires non-empty terms")
        if op == "and":
            return all(evaluate(t, row) for t in terms)
        if op == "or":
            return any(evaluate(t, row) for t in terms)
        return not evaluate(terms[0], row)

    if op in ("eq", "ne"):
        val = _get(row, node["col"])
        hit = _eq(val, node.get("value"))
        return hit if op == "eq" else not hit

    if op in ("in", "not_in"):
        val = _get(row, node["col"])
        values = node.get("values") or []
        hit = any(_eq(val, v) for v in values)
        return hit if op == "in" else not hit

    if op in ("gt", "gte", "lt", "lte"):
        return _cmp(op, _get(row, node["col"]), node.get("value"))

    if op == "is_empty":
        v = _get(row, node["col"])
        return v is None or (isinstance(v, str) and v.strip() == "")

    if op == "is_not_empty":
        v = _get(row, node["col"])
        return not (v is None or (isinstance(v, str) and v.strip() == ""))

    if op == "regex":
        v = _get(row, node["col"])
        if v is None:
            return False
        flags = 0
        if "i" in (node.get("flags") or ""):
            flags |= re.IGNORECASE
        return bool(re.search(str(node.get("pattern") or ""), str(v), flags))

    if op in ("sum_gt", "sum_gte", "sum_lt", "sum_lte"):
        cols = node.get("cols") or []
        total = Decimal("0")
        any_val = False
        for c in cols:
            d = _to_decimal(_get(row, c))
            if d is not None:
                total += d
                any_val = True
        if not any_val:
            return False
        target = Decimal(str(node.get("value") or 0))
        short = op.split("_")[1]  # gt/gte/lt/lte
        return _cmp(short, total, target)

    raise DSLError(f"Unknown op: {op!r}")
