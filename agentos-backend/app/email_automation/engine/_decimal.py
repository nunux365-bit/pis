"""Canonical Decimal coercion for the email-automation engine.

One function: :func:`to_decimal`. Used by :mod:`normalizer`, :mod:`dsl`, and
:mod:`pipeline.process` so all three agree on:

* **Parentheses-as-negative** \u2014 ``(1,234.50)`` \u2192 ``-1234.50`` (the Indian
  accounting convention our source workbooks use for credits).
* **Indian-grouping + currency-symbol scrubbing** \u2014 commas, NBSP (``\u00a0``),
  ``\u20b9``, ``INR`` (case-insensitive) stripped.
* **``bool`` is NOT a number** \u2014 ``True`` \u2192 ``None`` (not ``1``). We don't
  want a stray boolean cell to silently count as \u20b91 in a receivables sum.
* **Non-finite values** (``NaN`` / ``\u00b1Infinity`` \u2014 e.g. propagated Excel
  ``#DIV/0!``) \u2192 ``None``. Leaving them would poison every sum
  (``NaN + x == NaN``) and every comparison (``NaN > 0`` is ``False``),
  which has in the past routed $0-total reminders past the overpayment gate.

Callers that want a hard fallback (e.g. ``Decimal(0)``) should wrap this
function themselves \u2014 we deliberately don't bake a default in so the
``None`` signal isn't lost for consumers that need to distinguish
"missing / bad" from "zero".
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Indian grouping commas + NBSP + plain whitespace + rupee symbol + literal ``INR``.
# Keep this regex in one place: both dsl and normalizer previously carried
# near-identical (but subtly different) copies.
_NUM_CLEAN_RE = re.compile(r"[,\u00a0\s\u20b9]|(?:inr)", re.IGNORECASE)


def to_decimal(v: Any) -> Decimal | None:
    """Return ``Decimal(v)`` or ``None`` if ``v`` can't be represented safely.

    Returns ``None`` for: ``None``, ``bool``, blank strings, un-parseable
    strings, and non-finite ``Decimal`` / ``float`` values.
    """

    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    if isinstance(v, (int, float)):
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None

    s = str(v).strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    s = _NUM_CLEAN_RE.sub("", s)
    if not s or s in {"-", "."}:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return -d if neg else d


__all__ = ["to_decimal"]
