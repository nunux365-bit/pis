"""Pydantic schemas for the email automation HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MessageRead(BaseModel):
    id: UUID
    provider_message_id: str
    thread_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    status: str
    workflow_type: str | None = None
    classified_as: str | None = None
    processed_at: datetime | None = None
    attachments: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime


class SendRead(BaseModel):
    id: UUID
    source_message_id: UUID | None = None
    workflow_type: str
    variant: str
    business_key: str
    period_key: str
    dedupe_key: str
    status: str
    review_reasons: list[dict[str, str]] | None = None
    resolved_to_addrs: list[str] | None = None
    resolved_cc_addrs: list[str] | None = None
    to_addrs: list[str] | None = None
    cc_addrs: list[str] | None = None
    rendered_subject: str | None = None
    rendered_body_html: str | None = None
    aggregated_data: dict[str, Any] | None = None
    test_mode: bool
    provider_message_id: str | None = None
    provider_rfc_message_id: str | None = None
    gmail_thread_id: str | None = None
    sent_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    error: dict[str, Any] | None = None


class RetryFailedRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=400)


class ApproveRequest(BaseModel):
    note: str | None = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)


class SimpleResponse(BaseModel):
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Dashboard metrics \u2014 read-only, aggregations only, no row-level PII.
# Shape mirrors :class:`app.email_automation.pipeline.metrics.Metrics`; kept
# dict-of-counts where possible so adding a new FSM status later can't break
# dashboards (JSON keys add, never rename).
# ---------------------------------------------------------------------------


class MetricsHealth(BaseModel):
    enabled: bool
    test_mode: bool
    last_message_received_at: datetime | None = None
    last_send_sent_at: datetime | None = None


class MetricsAttention(BaseModel):
    """Counters sized to link 1:1 with runbook \u00a75 / \u00a77 triage paragraphs."""

    sends_at_attempt_cap: int
    messages_processed_with_errors_open: int
    sends_stuck_sending: int


class MetricsTrendPoint(BaseModel):
    bucket: datetime
    sent: int
    failed: int
    ingested: int


class MetricsResponse(BaseModel):
    window: str
    generated_at: datetime
    bucket_granularity: str
    health: MetricsHealth
    messages_by_status: dict[str, int]
    sends_by_status: dict[str, int]
    sends_by_variant: dict[str, dict[str, int]]
    sends_skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    sends_failed_by_reason: dict[str, int] = Field(default_factory=dict)
    trend: list[MetricsTrendPoint]
    attention: MetricsAttention


class IntelligenceTimelinePoint(BaseModel):
    """DEPRECATED: One time bucket for reply-intelligence classifications."""
    bucket: datetime
    count: int


class IntelligencePriorPeriodRead(BaseModel):
    """DEPRECATED: Counts for the same-length interval immediately before the active window (UTC)."""

    total_threads: int
    by_category: dict[str, int] = Field(default_factory=dict)
    by_confidence: dict[str, int] = Field(default_factory=dict)
    low_confidence_count: int


class IntelligenceMetricsResponse(BaseModel):
    """DEPRECATED: Response for intelligence metrics."""
    window: str
    generated_at: datetime
    kind: str
    bucket_granularity: Literal["hour", "day"]
    total_threads: int
    by_category: dict[str, int]
    by_confidence: dict[str, int] = Field(default_factory=dict)
    low_confidence_count: int
    timeline: list[IntelligenceTimelinePoint] = Field(default_factory=list)
    prior_period: IntelligencePriorPeriodRead | None = None


class IntelligenceThreadRow(BaseModel):
    """DEPRECATED: Part of the deprecated Replies tab."""
    id: str
    gmail_thread_id: str
    kind: str
    category: str
    confidence: str
    business_key: str | None
    workflow_type: str | None
    variant: str | None
    classified_at: datetime
    trigger_message_id: str | None
    anchor_send_id: str | None


class IntelligenceThreadsPage(BaseModel):
    """Keyset-paginated list for admin intelligence UI."""

    items: list[IntelligenceThreadRow]
    next_cursor: str | None = None
    has_more: bool = False


class IntelligenceReceivableRow(BaseModel):
    """Merged row showing party details + latest intelligence classification."""

    hana_code: str
    party_name: str
    business_unit: str
    total_overdue_lakh: float
    email_sent_at: datetime | None = None
    reply_received_at: datetime | None = None
    reply_category: str | None = None
    gmail_thread_id: str | None = None
    email_subject: str | None = None


class IntelligenceReceivablesResponse(BaseModel):
    """Paginated dashboard table response."""

    items: list[IntelligenceReceivableRow]
    business_units: list[str]
    total: int
    limit: int
    offset: int
    has_more: bool


class CategoryBreakdownRow(BaseModel):
    """Aggregated counts and overdue amount for one reply category."""

    category: str
    reply_count: int
    pct_of_replies: float   # 0–100, one decimal place
    amount_due_lakh: float


class IntelligenceSummaryResponse(BaseModel):
    """Aggregated Collections Outreach executive summary for one BU scope (or All)."""

    business_unit: str | None = None  # None / "All" = whole book

    # KPI cards
    total_overdue_book_lakh: float
    total_overdue_in_campaign_lakh: float
    total_overdue_replies_lakh: float

    # Engagement metrics
    emails_delivered: int
    replies_received: int
    reply_rate: float   # 0–100, one decimal place

    # Reply breakdown table (sorted by reply_count desc)
    category_breakdown: list[CategoryBreakdownRow]

    # Active send-anchored view window in days (7 / 14 / 30)
    window_days: int = 7

    # Meta
    snapshot_created_at: datetime | None = None
    generated_at: datetime


class KamDashboardRow(BaseModel):
    """One KAM's reply-tracking metrics over the dashboard window."""

    kam_name: str
    total_overdue_lakh: float     # total overdue ₹ (lakh, TDS-adjusted) the KAM manages
    reply_rate: float             # 0–100, one decimal — KAM/Central/Finance/other 1mg team
                                   # client-facing follow-ups / eligible (client-replied) threads
    accuracy_rate: float          # 0–100, one decimal — avg LLM 0/1 accuracy score over
                                   # eligible (client-replied) threads only
    avg_reply_seconds: float | None  # mean client→KAM reply gap; null when no replies
    threads_scored: int           # total threads scored for this KAM in the window
    client_replied_threads: int   # eligible threads: client genuinely replied (rate base)
    no_client_reply_threads: int  # threads excluded from both rates: no client reply at all


class KamDashboardResponse(BaseModel):
    """Per-KAM dashboard rows for one rolling window."""

    window: str                   # "24h" | "7d" | "30d"
    generated_at: datetime
    rows: list[KamDashboardRow]


class EmailThreadMessage(BaseModel):
    """One message inside a Gmail thread."""

    message_id: str
    from_addr: str
    to_addr: str          # raw "To:" header string (may be comma-sep list of addresses)
    cc_addr: str = ""     # raw "Cc:" header string; empty when not present
    subject: str
    date_ms: int          # internalDate in milliseconds (UTC epoch)
    body: str = ""   # plain text body of the message


class EmailThreadResponse(BaseModel):
    """All messages in a Gmail thread, ordered oldest-first."""

    thread_id: str
    messages: list[EmailThreadMessage]
