"""Value normalizer — sheet cell → typed engine value.

Upstream sheets use Indian grouping (``12,34,567.89``), parentheses-as-negative
(``(1,234)``), date strings in multiple formats, and inconsistent blanks. The
normalizer reads a per-column type hint from the workflow pack and returns typed
values. Unknown / unparseable values become ``None`` — the DSL treats ``None`` as
"missing" rather than zero so aggregations never silently understate totals.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from ._decimal import to_decimal as _to_decimal

ColumnType = Literal["str", "decimal", "int", "date", "bool", "raw"]

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%b-%y",
    "%Y/%m/%d",
    "%d.%m.%Y",
)


def _to_int(v: Any) -> int | None:
    d = _to_decimal(v)
    if d is None:
        return None
    try:
        return int(d)
    except (ValueError, OverflowError):
        return None


def _to_date(v: Any) -> _dt.date | None:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().casefold()
    if s in {"y", "yes", "true", "1", "positive"}:
        return True
    if s in {"n", "no", "false", "0", "negative"}:
        return False
    return None


def _to_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).replace("\u00a0", " ").strip()
    return s or None


def normalize_value(v: Any, kind: ColumnType) -> Any:
    if kind == "str":
        return _to_str(v)
    if kind == "decimal":
        return _to_decimal(v)
    if kind == "int":
        return _to_int(v)
    if kind == "date":
        return _to_date(v)
    if kind == "bool":
        return _to_bool(v)
    return v  # "raw"
