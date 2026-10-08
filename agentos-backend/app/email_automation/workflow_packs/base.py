"""Workflow pack data classes — JSON-serializable when snapshotted for audit.

A :class:`WorkflowPack` is a single workflow type (e.g. ``PAYMENT_REMINDER_WEEKLY``)
with N variants. Each variant is a complete pipeline configuration — sheets to
read, per-column types, row-level filter, aggregation keys + sum columns, decision
gates, recipient resolver, renderer. A variant is entirely declarative so the pack
can be snapshotted on the send row for reproducibility (``aggregated_data.pack_snapshot``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence

NotDueMonthEndMode = Literal["invoice_ht", "invoice_ht_aggregator", "dmpl"]

from app.email_automation.engine.classifier import ClassifierRule
from app.email_automation.engine.renderer import RendererConfig
from app.email_automation.engine.resolver import ResolverConfig


@dataclass(frozen=True, slots=True)
class SheetConfig:
    """One Excel sheet inside the inbound attachment."""

    name: str
    header_hints: tuple[str, ...]
    # Extra physical tab titles to try after ``name`` (finance renames).
    name_aliases: tuple[str, ...] = field(default_factory=tuple)
    # Canonical key (``{norm_header}#{occurrence}``) -> type ("str" | "decimal" | "date" | "int" | "bool" | "raw").
    # Unlisted columns default to ``raw``.
    column_types: Mapping[str, str] = field(default_factory=dict)
    # Optional DSL predicate; when present only rows where it evaluates true survive.
    row_filter: Mapping[str, object] | None = None
    # Optional projection ``{unified_key -> spec}`` applied **after** filtering.
    # Lets a pack normalize multiple source sheets (e.g. CHW reads three invoice
    # sheets with different column names) into one shared schema so the
    # aggregator can group across all of them by ``unified_key``. The unified
    # values are merged into ``NormalizedRow.values`` (existing keys are
    # preserved for downstream debug). Pick collision-free unified keys
    # (e.g. ``"_hana"``).
    #
    # ``spec`` is either:
    #
    # * ``str`` — source canonical key; the cell value is copied through.
    # * ``Mapping`` — a declarative op resolved by
    #   :func:`app.email_automation.pipeline.process._resolve_projection`.
    #   Ops today:
    #     * ``{"op": "sub", "a": SRC, "b": SRC}`` — Decimal subtraction
    #       (used by DMPL to compute *overdue* net pending as
    #       ``Grand Total − Not Due``).
    #     * ``{"op": "bucket_label", "buckets": [[SRC, LABEL], ...]}`` —
    #       oldest-first walk that returns the first ``LABEL`` whose
    #       column is > 0 (used by DMPL to synthesize the customer-facing
    #       Ageing bucket from per-bucket amount columns).
    #   Kept declarative (no callables) so ``WorkflowPack`` stays
    #   JSON-snapshottable on the send row.
    projection: Mapping[str, str | Mapping[str, object]] = field(
        default_factory=dict
    )
    # Optional bucket-expansion config. When set, each filtered + projected
    # source row is expanded into one :class:`NormalizedRow` per non-zero
    # age bucket instead of emitting a single row. This lets party-level
    # receivable sheets (e.g. ``Receivable as on <date>``) produce one email
    # table row per age bucket so customers see a clean age-breakdown.
    #
    # Schema::
    #
    #   {
    #     "buckets": [
    #       {"source_key": "<canonical_key>", "label": "<display_label>"},
    #       ...
    #     ],
    #     "amount_target_key": "<unified_key_for_bucket_amount>",
    #     "label_target_key":  "<unified_key_for_bucket_label>",
    #     "skip_zero": true   # default True — omit zero / negative buckets
    #   }
    #
    # ``amount_target_key`` and ``label_target_key`` are written into each
    # expanded row, overriding any value set by the normal ``projection``.
    # ``sum_cols`` on the variant should include ``amount_target_key`` so
    # the aggregator totals the per-bucket amounts (= total overdue) rather
    # than the gross receivable.
    row_expander: Mapping[str, object] | None = None
    # When set, Not Due rows on this sheet are dropped after projection unless
    # their due date falls in the current calendar month (IST). Overdue rows are
    # never affected. ``invoice_ht``: ``Ageing type = Not Due`` and
    # ``Invoice Date + Credit Days``; ``dmpl``: future ``Net Due Date`` only.
    not_due_month_end_mode: NotDueMonthEndMode | None = None
    # H4 fail-closed: when True (default) a missing sheet in the attachment is a
    # processing failure — the variant is aborted with
    # ``required_sheet_missing`` so the message ends up in
    # ``processed_with_errors`` (retryable) instead of silently emitting an
    # incomplete plan. Flip to False for sheets that are allowed to be absent
    # (e.g. a variant-specific optional sheet that some senders include).
    required: bool = True


@dataclass(frozen=True, slots=True)
class LookupSheetConfig:
    """A secondary Excel sheet read for *per-key facts*, not invoice rows.

    Used today for Unaccounted Revenue (per-HANA balance not yet allocated to
    an invoice). The pipeline reads the sheet, builds an in-memory map keyed
    by ``key_column`` (canonical key) and exposes the looked-up value to the
    renderer / decision gates as ``client.facts[<output_name>]``.
    """

    name: str
    header_hints: tuple[str, ...]
    # Canonical key on this sheet to match against the aggregated client's
    # ``business_key`` (e.g. ``"code#0"`` for ``Party wise Ageing-H&T``).
    key_column: str
    # Extra physical tab titles for this lookup (same logical sheet).
    name_aliases: tuple[str, ...] = field(default_factory=tuple)
    # ``{output_name -> canonical_key_on_this_sheet}``. Output names are
    # exposed as ``client.facts[output_name]`` and as ``{output_name_inr}``
    # placeholders if numeric.
    value_columns: Mapping[str, str] = field(default_factory=dict)
    # Type per output_name — same vocabulary as ``SheetConfig.column_types``.
    value_types: Mapping[str, str] = field(default_factory=dict)
    # Optional DSL predicate applied per lookup row before grouping — lets a
    # variant scope a shared lookup sheet (e.g. ``Party wise Ageing-H&T`` is
    # one physical sheet but holds both ePharma and CHW parties; ePharma
    # looks only at ``Business Unit = e-Pharmacy`` rows, CHW only at
    # ``Business Unit = Corporate Wellness`` rows). Predicates run against
    # raw cell values (stringly-typed) — stick to ``eq`` / ``regex`` on BU
    # columns; don't rely on decimal/date coercion here.
    row_filter: Mapping[str, object] | None = None
    # H4 fail-closed: when True (default) a missing lookup sheet aborts the
    # variant — otherwise a sender who forgets to include ``Unaccounted
    # Revenue`` would silently get reminders computed without the
    # unaccounted-receipts offset (potential overpayment). Flip to False
    # for a purely optional enrichment sheet.
    required: bool = True


@dataclass(frozen=True, slots=True)
class VariantConfig:
    """One business variant of a workflow (e.g. ``epharma``)."""

    name: str
    sheets: tuple[SheetConfig, ...]
    group_by_keys: tuple[str, ...]
    party_name_key: str | None
    sum_cols: tuple[str, ...]
    # Decision gates evaluated post-aggregation — see aggregator._gate_matches for syntax.
    decision_gates: tuple[Mapping[str, object], ...] = ()
    # Settings attribute names (on ``settings``) that hold the master tracker sheet id + tab.
    master_sheet_id_setting: str = ""
    master_tab_setting: str = ""
    # Header index on the tracker sheet; 0 unless the tracker has a title row.
    master_header_row_index: int = 0
    resolver: ResolverConfig | None = None
    renderer: RendererConfig | None = None
    # Optional secondary sheets read once per attachment for per-key fact lookups.
    lookup_sheets: tuple[LookupSheetConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowPack:
    """A top-level workflow (one inbound classifier rule, N business variants)."""

    workflow_type: str
    classifier_rule: ClassifierRule
    variants: tuple[VariantConfig, ...]
    # ``iso_week`` uses the received_at date's ISO year + week number, giving a
    # stable per-week dedupe grouping independent of weekday or retry count.
    period_strategy: str = "iso_week"

    def variant(self, name: str) -> VariantConfig:
        for v in self.variants:
            if v.name == name:
                return v
        raise KeyError(f"variant {name!r} not registered on {self.workflow_type}")

    def iter_variants(self) -> Sequence[VariantConfig]:
        return self.variants
