"""Internal helpers shared across the pipeline subpackage.

Private by convention — consumers import from ``app.email_automation.pipeline``
which re-exports the public names. Anything here can change without notice.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal

log = logging.getLogger("app.email_automation.pipeline")


def _iso_week_key(dt: datetime) -> str:
    """ISO year + week (e.g. ``2026-W16``) for stable weekly dedupe grouping."""

    iso = dt.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def period_key(strategy: str, received_at: datetime | None, *, fallback: datetime) -> str:
    """Return the dedupe period key for a message.

    ``fallback`` is used when ``received_at`` is missing — callers **must** pick
    a stable reference (e.g. the cron tick start) so retries don't change the
    key. Using ``datetime.now()`` inside this function would break idempotency.
    """

    base = received_at or fallback
    if strategy == "iso_week":
        return _iso_week_key(base)
    raise ValueError(f"Unsupported period strategy: {strategy}")


def dedupe_key(workflow_type: str, variant: str, business_key: str, period: str) -> str:
    raw = "|".join([workflow_type, variant, business_key, period])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decimal_default(v: object) -> object:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.isoformat()
    raise TypeError(f"Unserializable: {type(v)}")


def jsonable(obj: object) -> object:
    """Depth-unlimited JSON round-trip that tolerates Decimal / date-time types."""

    return json.loads(json.dumps(obj, default=_decimal_default))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "log",
    "period_key",
    "dedupe_key",
    "jsonable",
    "now_utc",
]
