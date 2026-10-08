"""Public entry points for the email-automation pipeline.

Organised as a package because the pipeline is large enough to warrant
physical separation (ingest vs classify/process vs dispatch vs review queue).
Consumers should import from this module, not from submodules:

    from app.email_automation import pipeline

    await pipeline.scan_and_process(db)
    await pipeline.dispatch_approved(db)
    await pipeline.mark_approved(db, send_id, user_id)

Internal helpers live in :mod:`._shared` and submodules with a leading
underscore on their names.
"""

from __future__ import annotations

from ._shared import dedupe_key, jsonable, now_utc, period_key
from .dispatch import (
    DEFAULT_DISPATCH_SEND_BATCH,
    SENDING_RECLAIM_AFTER_MINUTES,
    dispatch_approved,
    dispatch_claim_and_send,
    reclaim_stuck_sending,
)
from .ingest import IngestedMessage, ingest_inbox, ingest_new_messages
from .metrics import (
    SUPPORTED_WINDOWS,
    Attention,
    Health,
    Metrics,
    TrendPoint,
    Window,
    compute_metrics,
    compute_trend_only,
    message_and_send_status_counts,
)
from .process import (
    build_variant_plans,
    classify_and_process_received,
    load_tracker_rows,
    run_variant,
    scan_and_process,
)
from .review_queue import mark_approved, mark_rejected, mark_retry_failed

__all__ = [
    # Shared helpers
    "period_key",
    "dedupe_key",
    "jsonable",
    "now_utc",
    # Ingest
    "IngestedMessage",
    "ingest_inbox",
    "ingest_new_messages",
    # Classify + process
    "load_tracker_rows",
    "build_variant_plans",
    "run_variant",
    "classify_and_process_received",
    "scan_and_process",
    # Dispatch
    "DEFAULT_DISPATCH_SEND_BATCH",
    "SENDING_RECLAIM_AFTER_MINUTES",
    "reclaim_stuck_sending",
    "dispatch_claim_and_send",
    "dispatch_approved",
    # Review queue
    "mark_approved",
    "mark_rejected",
    "mark_retry_failed",
    # Metrics
    "SUPPORTED_WINDOWS",
    "Attention",
    "Health",
    "Metrics",
    "TrendPoint",
    "Window",
    "compute_metrics",
    "compute_trend_only",
    "message_and_send_status_counts",
]
