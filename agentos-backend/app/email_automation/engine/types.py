"""Shared engine types — kept in one place so unit tests don't cross-import."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# A ColumnRef resolves a header by name, with optional occurrence when the same
# header is duplicated in the sheet (e.g. T(labs) has two "Remarks" columns).
ColumnRef = str | dict  # {"name": "Remarks", "occurrence": 1}


@dataclass(frozen=True, slots=True)
class NormalizedRow:
    """One logical invoice row after header/value normalization."""

    source_sheet: str
    # Typed, normalized values keyed by engine-canonical names.
    values: dict[str, Any]
    # Raw strings keyed by header-after-normalization — useful for template rendering
    # where operators want the original tokens verbatim.
    raw: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(slots=True)
class AggregatedClient:
    """Per-business-key aggregation result (one outbound email candidate).

    ``business_key`` is the pipe-joined display form of ``business_key_parts`` and
    is what's stored on :class:`~app.db.models.EmailAutomationSend.business_key`.
    """

    business_key: str
    business_key_parts: tuple[str, ...]
    party_name: str | None
    rows: list[NormalizedRow] = field(default_factory=list)
    totals: dict[str, Decimal] = field(default_factory=dict)
    # Gates that matched during evaluation.
    review_reasons: list[dict[str, str]] = field(default_factory=list)
    # Per-key facts looked up from secondary sheets (e.g. unaccounted_revenue).
    # Output name → typed value (Decimal / date / str depending on lookup config).
    facts: dict[str, Any] = field(default_factory=dict)
