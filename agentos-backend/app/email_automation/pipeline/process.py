"""Classification + per-variant processing + persistence.

Public entry points:

* :func:`classify_and_process_received` — the graph's ``classify_and_process``
  node: picks up every ``status='received'`` message (inserted by the ingest
  node this tick or left over from a previous tick), classifies each, runs
  every matching variant, and persists one :class:`EmailAutomationSend` per
  (variant × HANA party).
* :func:`scan_and_process` — one-shot compatibility entry used by the HTTP
  ``/scan`` endpoint and the cron job; runs ingest + classify in sequence.

Separation matters: the graph shows two nodes doing two things, and the
classify node can pick up "orphan" ``received`` rows that survived a crash
between ingest commit and classify start.
"""

from __future__ import annotations

import asyncio
import html
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, EmailAutomationSend
from app.email_automation import gmail_sa, sheets_sa
from app.email_automation.engine.excel_reader import SheetData
from app.email_automation.engine import classifier as _classifier
from app.email_automation.engine._decimal import to_decimal as _to_decimal
from app.email_automation.engine.banners import banner as _banner
from app.email_automation.workflow_packs import REGISTRY, WorkflowPack

from . import chw_mail_master as _chw_mm
from . import eph_mail_master as _eph_mm
from ._shared import dedupe_key, jsonable, log, now_utc, period_key
from .collections_intelligence import process_collections_intelligence_batch
from .ingest import IngestedMessage, ingest_inbox, ingest_new_messages
from .workbook_attachments import (
    WorkbookResolveError,
    cleanup_attachment_staging_dir,
    resolve_downloaded_workbook,
)


# Extract the token that follows ``from:`` in a Gmail search query. Handles:
#   from:alice@1mg.com
#   from:(alice@1mg.com OR bob@1mg.com)
#   from:"Alice <alice@1mg.com>"
#   FROM:alice@1mg.com     (Gmail is case-insensitive on operators)
# We then pull RFC-5322 addresses out of whatever matched, so display names
# and parentheses / quotes wash out naturally.
_FROM_CLAUSE_RE = re.compile(r"""\bfrom:\s*(?:"([^"]*)"|\(([^)]*)\)|(\S+))""", re.IGNORECASE)
_ADDR_IN_TOKEN_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class _RequiredSheetMissing(Exception):
    """Raised by :func:`build_variant_plans` when a required Excel sheet
    (invoice or lookup) is missing in the attachment. Caught by
    :func:`_process_one`'s per-variant savepoint so the variant lands in
    ``processed_with_errors`` with a typed reason; other variants on the
    same message are unaffected."""

    __slots__ = ("variant", "sheet", "workbook", "kind")

    def __init__(self, *, variant: str, sheet: str, workbook: str, kind: str) -> None:
        self.variant = variant
        self.sheet = sheet
        self.workbook = workbook
        self.kind = kind
        super().__init__(
            f"required {kind} sheet {sheet!r} missing in {workbook!r} "
            f"(variant={variant})"
        )


class _RequiredColumnsMissing(Exception):
    """Raised by :func:`build_variant_plans` when a sheet is present but lacks
    columns the variant's row filter / projection / ageing sieve depend on.

    Caught by :func:`_process_one`'s per-variant savepoint (same contract as
    :class:`_RequiredSheetMissing`): the variant is abandoned with a typed
    reason and emits **no** plans, while every other variant on the message
    processes normally.

    Fail-closed by design. A missing column is NOT harmless: the DSL reads an
    absent column as ``None``, so a negative predicate (``ne`` / ``not_in`` /
    ``is_empty``) silently flips to **true** and rows that should have been
    filtered out would instead be mailed with wrong data. Refusing to build the
    variant is strictly safer than guessing, and beats the alternative failure
    mode of silently emailing nobody (or the wrong body) after AR renames a
    column upstream.
    """

    __slots__ = ("variant", "sheet", "workbook", "missing")

    def __init__(
        self, *, variant: str, sheet: str, workbook: str, missing: Sequence[str]
    ) -> None:
        self.variant = variant
        self.sheet = sheet
        self.workbook = workbook
        self.missing = list(missing)
        super().__init__(
            f"sheet {sheet!r} in {workbook!r} is missing column(s) required by "
            f"variant={variant}: {', '.join(self.missing)}"
        )


def _dsl_referenced_columns(node: Any) -> set[str]:
    """Canonical keys a DSL predicate reads. ``col`` refs are display names, so
    they go through ``dsl.col_key`` exactly like the evaluator does."""

    from app.email_automation.engine import dsl as _dsl

    out: set[str] = set()
    if not isinstance(node, Mapping):
        return out
    if node.get("op") in ("and", "or", "not"):
        for term in node.get("terms") or ():
            out |= _dsl_referenced_columns(term)
        return out
    if "col" in node:
        out.add(_dsl.col_key(node["col"]))
    for c in node.get("cols") or ():
        out.add(_dsl.col_key(c))
    return out


def _projection_referenced_columns(spec: Any) -> tuple[set[str], list[set[str]]]:
    """Return ``(required, any_of_groups)`` canonical keys for a projection spec.

    Projection specs already carry canonical keys (``"hana code#0"``), unlike
    DSL ``col`` refs. ``coalesce*`` ops are "first non-empty wins", so their
    columns are returned as an *any-of* group — only a group with **none** of
    its columns present is a real problem.
    """

    required: set[str] = set()
    any_of: list[set[str]] = []

    if isinstance(spec, str):
        required.add(spec)
        return required, any_of
    if not isinstance(spec, Mapping):
        return required, any_of

    op = spec.get("op")
    if op in ("coalesce", "coalesce_decimal"):
        cols = {c for c in (spec.get("cols") or ()) if isinstance(c, str)}
        if cols:
            any_of.append(cols)
        return required, any_of
    if op == "decimal_unless_eq_coalesce":
        if isinstance(spec.get("col"), str):
            required.add(spec["col"])
        cols = {c for c in (spec.get("amount_cols") or ()) if isinstance(c, str)}
        if cols:
            any_of.append(cols)
        return required, any_of

    for key in ("col", "a", "b", "due_col", "amount_col", "else_col"):
        val = spec.get(key)
        if isinstance(val, str):
            required.add(val)
        elif isinstance(val, Mapping):  # nested spec (e.g. decimal_unless_eq)
            sub_req, sub_any = _projection_referenced_columns(val)
            required |= sub_req
            any_of += sub_any
    for pair in spec.get("buckets") or ():  # bucket_label: [col, label]
        if isinstance(pair, (list, tuple)) and pair and isinstance(pair[0], str):
            required.add(pair[0])
    return required, any_of


# Columns each Not-Due month-end sieve reads directly (they are not expressed as
# filter/projection specs, so they must be declared here to be preflighted).
_NOT_DUE_MODE_COLUMNS: dict[str, frozenset[str]] = {
    "invoice_ht": frozenset({"ageing type#0", "invoice date#0", "credit days#0"}),
    "invoice_ht_aggregator": frozenset({"ageing type#0", "invoice date#0", "credit days#0"}),
    "dmpl": frozenset({"net due date#0"}),
}


def _validate_sheet_columns(sheet_config, sheet, *, variant_name: str, workbook: str) -> None:
    """Fail-closed preflight: every column the variant needs must exist.

    Raises :class:`_RequiredColumnsMissing` naming the absent columns so ops can
    see exactly what AR renamed, instead of the variant silently emitting zero
    (or wrong) emails.
    """

    available = set(sheet.canonical_keys)
    required: set[str] = set()
    any_of: list[set[str]] = []

    if sheet_config.row_filter:
        required |= _dsl_referenced_columns(sheet_config.row_filter)
    for spec in (sheet_config.projection or {}).values():
        spec_req, spec_any = _projection_referenced_columns(spec)
        required |= spec_req
        any_of += spec_any
    if sheet_config.not_due_month_end_mode:
        required |= _NOT_DUE_MODE_COLUMNS.get(
            sheet_config.not_due_month_end_mode, frozenset()
        )

    missing = sorted(k for k in required if k not in available)
    for group in any_of:
        if not (group & available):
            missing.append(f"one of [{', '.join(sorted(group))}]")

    if missing:
        raise _RequiredColumnsMissing(
            variant=variant_name,
            sheet=sheet.sheet_name,
            workbook=workbook,
            missing=missing,
        )


async def _read_sheet_with_optional_tab_ai_fallback(
    xlsx_path: Path,
    configured_tab_name: str,
    header_hints: tuple[str, ...],
    *,
    variant_name: str,
    workflow_type: str,
    name_aliases: tuple[str, ...] = (),
    sheet_kind: str = "",
) -> SheetData:
    """Try rule-based tab resolution (primary + aliases); then optional OpenAI once."""

    from app.email_automation.engine import excel_reader as _excel_reader
    from app.email_automation.engine.sheet_name_ai_fallback import (
        try_resolve_physical_sheet_name,
    )

    candidates = (configured_tab_name,) + tuple(name_aliases)
    last_keyerror: KeyError | None = None
    for tab in candidates:
        try:
            return await asyncio.to_thread(
                _excel_reader.read_sheet,
                xlsx_path,
                tab,
                header_hints=header_hints,
            )
        except KeyError as err:
            last_keyerror = err
            continue

    if not settings.email_automation_sheet_name_ai_fallback_enabled:
        if last_keyerror is not None:
            raise last_keyerror
        raise KeyError(configured_tab_name)

    picked = await try_resolve_physical_sheet_name(
        xlsx_path,
        configured_tab_name,
        variant_name=variant_name,
        workflow_type=workflow_type,
        attempted_tab_names=candidates,
        sheet_kind=sheet_kind,
        last_error_message=str(last_keyerror) if last_keyerror else None,
    )
    if not picked:
        log.warning(
            "sheet tab AI fallback returned no validated tab for configured=%r "
            "(%s/%s %s) — OpenAI returned null, wrong tab string, API error, or "
            "missing OPENAI_API_KEY; check logs above from sheet_name_ai_fallback",
            configured_tab_name,
            workflow_type,
            variant_name,
            xlsx_path.name,
        )
        if last_keyerror is not None:
            raise last_keyerror
        raise KeyError(configured_tab_name)

    log.warning(
        "sheet tab AI fallback matched configured=%r -> physical=%r (%s/%s/%s)",
        configured_tab_name,
        picked,
        workflow_type,
        variant_name,
        xlsx_path.name,
    )
    return await asyncio.to_thread(
        _excel_reader.read_sheet,
        xlsx_path,
        picked,
        header_hints=header_hints,
    )


# Review-reason codes that always act as a *skip gate* even though they are
# stored without the ``"skip:"`` prefix convention used by resolver checks.
# The post-lookup financial gates (overpayment / materiality floor) predate
# the prefix convention; keeping the bare codes preserves the existing plan
# schema that callers already rely on.
_SKIP_GATE_CODES: frozenset[str] = frozenset(
    {
        "potential_overpayment",
        "insufficient_outstanding",
    }
)


def _is_skip_gate(code: str) -> bool:
    """Return True if *code* should be routed to ``skip_gate`` rather than
    ``review_reasons`` on the outgoing plan dict."""
    return code.startswith("skip:") or code in _SKIP_GATE_CODES


def _eph_has_upcoming_invoice_rows(client) -> bool:
    """True when the aggregated client has at least one Due this Month table row."""

    from app.email_automation.workflow_packs.payment_reminder import (
        DUE_THIS_MONTH_LABEL,
        UNIFIED_STATUS,
    )

    label = DUE_THIS_MONTH_LABEL.casefold()
    return any(
        str(r.values.get(UNIFIED_STATUS) or "").strip().casefold() == label
        for r in client.rows
    )


def _chw_has_upcoming_invoice_rows(client) -> bool:
    """True when the CHW table has at least one row with Ageing = Due this Month."""

    from app.email_automation.workflow_packs.payment_reminder import (
        DUE_THIS_MONTH_LABEL,
        UNIFIED_AGEING,
    )

    label = DUE_THIS_MONTH_LABEL.casefold()
    return any(
        str(r.values.get(UNIFIED_AGEING) or "").strip().casefold() == label
        for r in client.rows
    )


# Review-reason codes that DO NOT cause the row to skip — they're audit /
# housekeeping signals only. The row still ships. Adding a code here is the
# explicit knob: if a future resolver check should be informational rather
# than blocking, drop its code in this set.
_NON_BLOCKING_REVIEW_REASON_CODES: frozenset[str] = frozenset(
    {
        # Resolver merges TO/CC across all duplicate matches — no contact is
        # lost — so the row is safe to send. Ops cleans the sheet on their
        # own cadence; we don't penalise the customer for sheet hygiene.
        "tracker_duplicate_keys",
    }
)


# Body banners injected when the customer's contacts couldn't be resolved
# from the master tracker. Both route the email to the variant's
# ``static_to`` (AR desk) but the suggested action differs, so we render
# two distinct banners. Mutually exclusive: a row can hit at most one of
# the two flags.
_MISSING_RECIPIENT_BANNER = _banner(
    "warn",
    "<b>Action required:</b> client email address not found in master "
    "tracker. This reminder is being sent to the AR desk only \u2014 "
    "please follow up with the customer directly.",
)


def _not_in_tracker_banner(business_key: str) -> str:
    """Banner shown when the customer (``business_key``) has NO tracker
    row at all. Distinct from the missing-recipient banner because the AR
    desk's next action differs: here they need to ADD the customer to the
    tracker (not just fill in their email)."""

    # ``business_key`` is operator-controlled (it comes from the Excel
    # attachment), so escape it before embedding in HTML. ``quote=True``
    # also escapes ``"`` and ``'`` so the value stays safe if this ever
    # gets refactored into an attribute context.
    safe_key = html.escape(business_key, quote=True)
    return _banner(
        "error",
        f"<b>Action required:</b> customer <code>{safe_key}</code> "
        "is not in the master tracker. This reminder is being sent to "
        "the AR desk only \u2014 please add the customer (with TO/CC "
        "email contacts) to the tracker so future reminders route "
        "directly to them.",
    )


def _skip_reason_line(stored_reasons: list[dict[str, str]] | None) -> str:
    """Format the one-line "why this was skipped" banner injected at the
    top of the AR-desk email. Picks the first reason (the resolver/gate
    appends in priority order) and falls back to a generic line."""

    code = "skipped"
    msg = "this row was skipped \u2014 see review_reasons in the platform UI."
    if stored_reasons:
        first = stored_reasons[0]
        code = str(first.get("code") or code)
        msg = str(
            first.get("human_message")
            or first.get("detail")
            or msg
        )
    safe_code = html.escape(code, quote=True)
    safe_msg = html.escape(msg, quote=True)
    return _banner(
        "error",
        f"<b>Skipped \u2014 {safe_code}:</b> {safe_msg} "
        "Customer reminder was <b>not</b> sent; this copy is for AR triage only.",
    )


def _senders_from_inbox_query(query: str) -> tuple[str, ...]:
    """Derive a sender allowlist from the existing ``inbox_query`` setting.

    Why reuse: the Gmail query is already the sender gate at the wire layer
    (``users.messages.list`` only returns emails matching ``from:<addr>``).
    Keeping the classifier's defense-in-depth allowlist in *lockstep* with
    the same string means ops edits one variable instead of two, and drift
    between "what Gmail delivers" and "what classify trusts" is impossible.

    If the query contains no ``from:`` clauses (e.g. someone broadens it to
    ``subject:Receivable newer_than:30d``) we return an empty tuple — the
    caller falls back to the pack's hard-coded default, which is the safe
    thing to do: we don't want a broad Gmail query to also broaden who the
    classifier trusts without a code review.
    """

    seen: dict[str, None] = {}
    for m in _FROM_CLAUSE_RE.finditer(query or ""):
        token = m.group(1) or m.group(2) or m.group(3) or ""
        for addr in _ADDR_IN_TOKEN_RE.findall(token):
            seen.setdefault(addr.lower(), None)
    return tuple(seen)


def _rule_with_overrides(
    pack: WorkflowPack, *, inbox_query: str
) -> "_classifier.ClassifierRule":
    """Apply ``inbox_query`` overrides to a pack's classifier rule.

    Only ``sender_allowlist`` is overridable today, and the override source is
    the *same* query string that Gmail filtered on at the wire — passed in
    explicitly so the wire filter and classifier allowlist cannot drift. HTTP
    ``/scan`` and scheduled ticks both thread the effective query through
    :func:`classify_and_process_received`; tests can pin a query without
    monkey-patching settings.

    Subject / attachment regexes stay in code because they encode the
    provider's email template and are coupled to the downstream Excel schema:
    a mismatch should surface as a reviewed code change, not a silent env edit.
    """

    override = _senders_from_inbox_query(inbox_query)
    if not override:
        return pack.classifier_rule
    return _classifier.ClassifierRule(
        workflow_type=pack.classifier_rule.workflow_type,
        sender_allowlist=override,
        subject_regex=pack.classifier_rule.subject_regex,
        attachment_regex=pack.classifier_rule.attachment_regex,
        min_attachment_size=pack.classifier_rule.min_attachment_size,
    )


# ---------------------------------------------------------------------------
# Projection spec resolver
# ---------------------------------------------------------------------------


_IST = ZoneInfo("Asia/Kolkata")
_projection_reference_date_override: date | None = None


def _set_projection_reference_date_for_tests(d: date | None) -> None:
    """Pin the reference calendar day (IST) for deterministic unit tests."""

    global _projection_reference_date_override
    _projection_reference_date_override = d


def _coerce_projection_date(v: Any) -> date | None:
    """Normalize Excel / engine date cells for due-date ageing projections."""

    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _projection_reference_date() -> date:
    """Calendar 'today' in IST for DMPL ageing and Not Due month sieve."""

    if _projection_reference_date_override is not None:
        return _projection_reference_date_override
    return datetime.now(_IST).date()


def _projection_month_end() -> date:
    today = _projection_reference_date()
    return date(today.year, today.month, monthrange(today.year, today.month)[1])


def _projection_month_start() -> date:
    today = _projection_reference_date()
    return date(today.year, today.month, 1)


def _ht_computed_due_date(typed: Mapping[str, Any]) -> date | None:
    inv = _coerce_projection_date(typed.get("invoice date#0"))
    if inv is None:
        return None
    credit_raw = typed.get("credit days#0")
    try:
        credit = int(credit_raw) if credit_raw is not None else 0
    except (TypeError, ValueError):
        return None
    if credit < 0:
        return None
    return inv + timedelta(days=credit)


def _is_ht_not_due_row(typed: Mapping[str, Any]) -> bool:
    from app.email_automation.engine import dsl as _dsl

    return _dsl._norm_str(typed.get("ageing type#0")) == _dsl._norm_str("Not Due")


def _not_due_due_this_month(due: date, *, mode: str) -> bool:
    """True when *due* is in the SOP 'due this month' window for *mode*.

    ``invoice_ht`` (ePharma / CHW) requires *due* to still be strictly ahead of
    today — a Not Due row whose due date has already passed falls out of the
    window and is dropped (unchanged, long-standing SOP behavior).

    ``invoice_ht_aggregator`` (Diagnostics-Aggregator only, per explicit
    request) is deliberately more lenient: it checks the FULL current
    calendar month (``month_start ≤ due ≤ month_end``), not just the
    remaining part of it. A Not Due row whose computed due date has already
    passed (e.g. due on the 9th when today is the 15th) still falls within
    the current month and is relabeled "Due this Month" rather than silently
    dropped — the row is stale-tagged "Not Due" by AR, not actually
    not-due-yet, and the customer still owes it. Do NOT reuse this mode for
    ePharma/CHW without an explicit ask — they intentionally keep the
    stricter "still upcoming" window.
    """

    today = _projection_reference_date()
    month_end = _projection_month_end()
    if mode == "invoice_ht":
        return today < due <= month_end
    if mode == "invoice_ht_aggregator":
        month_start = _projection_month_start()
        return month_start <= due <= month_end
    if mode == "dmpl":
        return today <= due <= month_end
    raise ValueError(f"unknown not_due_month_end_mode {mode!r}")


def _drop_not_due_outside_month(typed: Mapping[str, Any], mode: str) -> bool:
    """Return True when a Not Due row should be dropped (outside month window)."""

    if mode in ("invoice_ht", "invoice_ht_aggregator"):
        if not _is_ht_not_due_row(typed):
            return False
        due = _ht_computed_due_date(typed)
        if due is None or not _not_due_due_this_month(due, mode=mode):
            return True
        return False
    if mode == "dmpl":
        due = _coerce_projection_date(typed.get("net due date#0"))
        if due is None:
            return False
        today = _projection_reference_date()
        if today > due:
            return False
        return not _not_due_due_this_month(due, mode=mode)
    raise ValueError(f"unknown not_due_month_end_mode {mode!r}")


def _to_decimal_safe(v: Any) -> Decimal:
    """Coerce a cell value to ``Decimal`` for projection arithmetic, never raising.

    Thin wrapper over the canonical :func:`engine._decimal.to_decimal` that
    collapses "unknown / bad" to ``Decimal(0)`` (projections MUST be
    arithmetic-safe; a bad cell would otherwise take down the whole
    variant). Logs non-``None`` inputs that coerce away so ops can see
    which sheet column is mis-declared \u2014 silently zeroing is how you ship
    \u20b90 reminders past the overpayment gate.

    ``None``-valued cells (empty Excel cells) are intentionally NOT logged
    because they're the common case. Non-finite values (NaN / \u00b1\u221e from an
    Excel ``#DIV/0!``) are caught by the canonical layer and arrive here
    as ``None`` \u2014 so the log fires with the original bad value in ``v``.
    """

    if v is None:
        return Decimal(0)
    d = _to_decimal(v)
    if d is None:
        log.error(
            "failed to coerce cell value %r (%s) to Decimal, using 0",
            v, type(v).__name__,
        )
        return Decimal(0)
    return d


def _expand_rows_by_bucket(
    typed: dict[str, Any],
    expander: Mapping[str, Any],
    source_sheet: str,
    raw: dict[str, Any],
) -> list["NormalizedRow"]:
    """Expand one typed+projected row into per-bucket NormalizedRows.

    For each bucket entry in ``expander["buckets"]`` whose ``source_key``
    resolves to a Decimal > 0 (when ``skip_zero`` is True, the default), a
    copy of ``typed`` is produced with:

    * ``amount_target_key`` → bucket Decimal amount
    * ``label_target_key``  → bucket display label (str)

    This lets the diagnostics aggregator variant show one email table row per
    age bucket so the customer sees a clean age-breakdown instead of a single
    worst-bucket label synthesised from the sheet.
    """
    from app.email_automation.engine.types import NormalizedRow as _NR

    buckets = expander.get("buckets") or []
    amount_key: str = expander.get("amount_target_key") or ""
    label_key: str = expander.get("label_target_key") or ""
    skip_zero: bool = bool(expander.get("skip_zero", True))

    result: list[_NR] = []
    for bucket in buckets:
        src_key: str = bucket.get("source_key", "")
        label: str = bucket.get("label", "")
        amount = _to_decimal_safe(typed.get(src_key))
        if skip_zero and amount <= 0:
            continue
        row_copy = dict(typed)
        if amount_key:
            row_copy[amount_key] = amount
        if label_key:
            row_copy[label_key] = label
        result.append(_NR(source_sheet=source_sheet, values=row_copy, raw=raw))

    # Fallback: when skip_zero dropped every bucket but a source-level net
    # amount is available (e.g. ``Net Due`` on the ``Receivable as on`` sheet),
    # emit one row using that total so the party still appears in the email.
    # This handles AR sheets where the overall overdue balance is recorded in a
    # dedicated column but individual age-bucket columns have not been filled in.
    fallback_key: str = expander.get("fallback_amount_source_key") or ""  # type: ignore[assignment]
    if not result and fallback_key:
        fallback_amount = _to_decimal_safe(typed.get(fallback_key))
        if fallback_amount > 0:
            row_copy = dict(typed)
            if amount_key:
                row_copy[amount_key] = fallback_amount
            if label_key:
                row_copy[label_key] = None
            result.append(_NR(source_sheet=source_sheet, values=row_copy, raw=raw))

    return result


# Day-first date anywhere in the receivables attachment filename. AR names the
# wire attachment after its as-on date, with inconsistent separators and
# sometimes a trailing revision marker, e.g.::
#
#     Receivable-08-07.2026.xlsx        -> 08 Jul 2026   (mixed '-' and '.')
#     Receivable-31.05.2026-v3.xlsx     -> 31 May 2026   (date NOT at the end)
#
# so the pattern is searched, not anchored. ``(?<!\d)``/``(?!\d)`` stop a match
# starting or ending mid-number, which is what would otherwise let the 2-digit
# form read ISO ``2026-06-24`` as ``26-06-24`` (→ 26-Jun-2024). ISO is tried
# first so such names parse correctly rather than being mis-read or dropped.
# ``_ISO`` is year-first; the other two are day-first (Indian convention).
_FILENAME_DATE_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?<!\d)(\d{4})[.\-/_](\d{1,2})[.\-/_](\d{1,2})(?!\d)"), True),
    (re.compile(r"(?<!\d)(\d{1,2})[.\-/_](\d{1,2})[.\-/_](\d{4})(?!\d)"), False),
    (re.compile(r"(?<!\d)(\d{1,2})[.\-/_](\d{1,2})[.\-/_](\d{2})(?!\d)"), False),
)


def _receivable_as_on_clause_html(xlsx_path: Path) -> str:
    """Return ``" as on <b>08-Jul-2026</b>"`` for the aggregator intro line.

    The date is taken from the **receivables attachment filename** — the
    workbook the classifier matched on Bhawna's mail is named after its as-on
    date, so no workbook I/O is needed here.

    Deliberately NOT the snapshot/ingest timestamp: that records when we
    processed the file, which can be days after the as-on date (e.g. a workbook
    dated 17-Jun ingested on 24-Jun) and would print a wrong, customer-facing date.

    Returns the FULL clause rather than a bare date so the sentence degrades
    cleanly to "… in our records:" when the filename carries no parseable date —
    instead of a dangling "as on <b></b>".

    Safe to interpolate as raw HTML: the label is rebuilt from parsed integers
    via ``strftime``, so no filename text reaches the body.
    """

    stem = Path(xlsx_path).stem  # drop .xlsx/.zip
    for pattern, year_first in _FILENAME_DATE_PATTERNS:
        for m in pattern.finditer(stem):
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year, month, day = (a, b, c) if year_first else (c, b, a)
            if year < 100:
                year += 2000
            try:
                parsed = date(year, month, day)  # rejects month > 12 / bad days
            except ValueError:
                continue  # e.g. 31.02.2026 or a non-date number run — keep looking
            return f" as on <b>{parsed.strftime('%d-%b-%Y')}</b>"

    log.warning(
        "no parseable as-on date in receivables filename %r — aggregator intro "
        "line will omit it",
        Path(xlsx_path).name,
    )
    return ""


def _lookup_row_key(row: Mapping[str, Any], key_column: str) -> Any:
    """Return the lookup join key, with HANA/Code cross-fallback on partywise sheets."""

    val = row.get(key_column)
    if val is not None and str(val).strip():
        return val
    if key_column == "hana code#0":
        return row.get("code#0")
    if key_column == "code#0":
        return row.get("hana code#0")
    return val


def _resolve_projection(spec: Any, row: Mapping[str, Any]) -> Any:
    """Apply a projection spec to a typed row and return the unified value.

    Specs come in two shapes:

    * ``str`` — source canonical key; copy the typed cell through verbatim
      (preserves the cell's type — Decimal / date / str / int). This is the
      legacy single-column projection used by 95% of the pack.
    * ``Mapping`` — a declarative op. Supported ops:

      - ``{"op": "sub", "a": SRC, "b": SRC}`` → ``a - b`` on Decimal
        coercion. Used by DMPL to express "Net Pending = Grand Total − Not
        Due" so the unified ``UNIFIED_NET_PENDING`` carries only the
        *overdue* portion (PDF intent). Missing/non-numeric values are
        treated as 0 — consistent with how the DSL filter's ``sum_gt``
        already handles them.

      - ``{"op": "bucket_label", "buckets": [[SRC, LABEL], ...]}`` → walk
        the list in order and return the first ``LABEL`` whose ``SRC``
        column holds a Decimal strictly > 0. Used by DMPL (sheets with
        per-bucket amount columns but no text-labeled "Ageing" column) to
        synthesize a bucket label for the customer-facing Ageing field.
        The bucket list MUST be authored oldest-first so the label
        reflects the worst-aged slice on the row — that's the most
        actionable signal for collections and doesn't flip week-to-week
        as partial receipts shuffle amounts between buckets. Returns
        ``None`` if every bucket is zero / missing (defensive — the
        variant's row filter should already have excluded such rows).

    The spec is kept declarative (no callables) so :class:`WorkflowPack`
    remains JSON-snapshottable for ``aggregated_data.pack_snapshot``.
    Unknown ops raise ``ValueError`` so a typo fails loudly during variant
    planning rather than silently emitting ``None`` into the renderer.
    """

    if isinstance(spec, str):
        return row.get(spec)
    if isinstance(spec, Mapping):
        op = spec.get("op")
        if op == "sub":
            return _to_decimal_safe(row.get(spec["a"])) - _to_decimal_safe(
                row.get(spec["b"])
            )
        if op == "bucket_label":
            buckets = spec.get("buckets") or ()
            for pair in buckets:
                # Accept [col, label] or (col, label) — tuples survive
                # in-memory, lists survive JSON round-trips for
                # ``pack_snapshot``.
                try:
                    col, label = pair[0], pair[1]
                except (IndexError, TypeError) as exc:
                    raise ValueError(
                        f"bucket_label: each bucket must be a [col, label] "
                        f"pair; got {pair!r}"
                    ) from exc
                if _to_decimal_safe(row.get(col)) > 0:
                    return label
            return None
        if op == "code_str":
            # Format a party/HANA code as a clean string. SAP exports carry
            # numeric customer codes as floats (``1000017221.0``); a plain
            # ``str()`` would leak the ``.0`` into the business key, subject and
            # tracker join. Integral numerics render without the fractional
            # part; everything else is passed through stripped.
            val = row.get(spec["col"])
            if val is None:
                return None
            if isinstance(val, bool):
                return str(val)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, float):
                return str(int(val)) if val.is_integer() else str(val)
            if isinstance(val, Decimal):
                return str(int(val)) if val == val.to_integral_value() else str(val)
            s = str(val).strip()
            return s or None
        if op == "coalesce":
            for col in spec.get("cols") or ():
                val = row.get(col)
                if val is None:
                    continue
                if isinstance(val, str) and not val.strip():
                    continue
                return val
            return None
        if op == "coalesce_decimal":
            for col in spec.get("cols") or ():
                d = _to_decimal_safe(row.get(col))
                if d > 0:
                    return d
            return Decimal("0")
        if op == "map_eq_str":
            from app.email_automation.engine import dsl as _dsl

            col_val = row.get(spec["col"])
            if _dsl._norm_str(col_val) == _dsl._norm_str(spec.get("value")):
                return spec.get("then")
            else_col = spec.get("else_col")
            if else_col is not None:
                return row.get(else_col)
            return spec.get("else")
        if op == "decimal_unless_eq":
            from app.email_automation.engine import dsl as _dsl

            col_val = row.get(spec["col"])
            if _dsl._norm_str(col_val) == _dsl._norm_str(spec.get("value")):
                return Decimal("0")
            amount_spec = spec.get("amount_col")
            if isinstance(amount_spec, Mapping):
                return _resolve_projection(amount_spec, row)
            return _to_decimal_safe(row.get(amount_spec))
        if op == "decimal_unless_eq_coalesce":
            from app.email_automation.engine import dsl as _dsl

            col_val = row.get(spec["col"])
            if _dsl._norm_str(col_val) == _dsl._norm_str(spec.get("value")):
                return Decimal("0")
            for col in spec.get("amount_cols") or ():
                d = _to_decimal_safe(row.get(col))
                if d > 0:
                    return d
            return Decimal("0")
        if op == "ageing_from_due_date":
            due = _coerce_projection_date(row.get(spec["due_col"]))
            if due is None:
                return spec.get("missing", "Overdue")
            not_due_label = str(spec.get("not_due_label") or "Due this Month")
            return (
                "Overdue"
                if _projection_reference_date() > due
                else not_due_label
            )
        if op == "overdue_amount_if_past_due":
            due = _coerce_projection_date(row.get(spec["due_col"]))
            if due is None or _projection_reference_date() <= due:
                return Decimal("0")
            return _to_decimal_safe(row.get(spec["amount_col"]))
        raise ValueError(f"unknown projection op {op!r} in spec {dict(spec)!r}")
    raise TypeError(
        f"projection spec must be str or Mapping, got {type(spec).__name__}"
    )


# ---------------------------------------------------------------------------
# Variant planning (sheet read → filter → aggregate → lookup → render)
# ---------------------------------------------------------------------------


async def load_tracker_rows(variant) -> list[dict[str, Any]]:
    """Fetch master-tracker rows for the variant (empty list if unconfigured)."""

    if not (variant.resolver and variant.master_sheet_id_setting):
        return []
    sheet_id = getattr(settings, variant.master_sheet_id_setting, "") or ""
    tab = getattr(settings, variant.master_tab_setting, "") or ""
    if not (sheet_id and tab):
        log.warning(
            "variant=%s missing master tracker config (sheet_id=%r tab=%r) — "
            "every send will land in 'skipped' with reason 'no_primary_recipient'",
            variant.name, sheet_id, tab,
        )
        return []
    tbl = await asyncio.to_thread(
        sheets_sa.read_table,
        sheet_id,
        tab,
        header_row_index=variant.master_header_row_index,
    )
    return tbl.dicts()


async def build_variant_plans(
    *,
    pack: WorkflowPack,
    variant,
    xlsx_path: Path,
    tracker_rows: list[dict[str, Any]],
    period: str,
) -> list[dict[str, Any]]:
    """Read the workbook + master tracker and produce per-client send plans.

    Pure (no DB IO, no Gmail writes). Returns one ``plan`` dict per aggregated
    client that the persistence loop turns into an :class:`EmailAutomationSend`
    row. Stages:

      1. read each declared sheet (header auto-detect, NBSP-safe normalize)
      2. apply the per-sheet DSL row filter
      3. aggregate rows by ``variant.group_by_keys``, sum ``variant.sum_cols``
      4. apply lookup sheets (per-variant BU-scoped unaccounted receipts)
      5. evaluate post-lookup decision gates (overpayment ⇒ skip)
      5b. CHW / ePharma: master-tracker ``… Exclusion = Yes`` ⇒
          ``skip:reminder_exclusion``; else optional KAM / RPO contact block in body
      6. resolve recipients from the master tracker
      7. render subject + HTML body
    """

    from app.email_automation.engine import (
        aggregator as _aggregator,
        dsl as _dsl,
        normalizer as _normalizer,
        renderer as _renderer,
        resolver as _resolver,
    )
    from app.email_automation.engine.types import NormalizedRow

    all_rows: list[NormalizedRow] = []
    dropped = 0
    for sc in variant.sheets:
        try:
            sheet = await _read_sheet_with_optional_tab_ai_fallback(
                xlsx_path,
                sc.name,
                sc.header_hints,
                variant_name=variant.name,
                workflow_type=pack.workflow_type,
                name_aliases=sc.name_aliases,
                sheet_kind="invoice",
            )
        except KeyError:
            if sc.required:
                # Fail closed: a required invoice sheet is missing. Raise so
                # the per-variant savepoint in ``_process_one`` rolls back
                # and the message lands in ``processed_with_errors`` for
                # retry / ops triage. Silently emitting a partial plan would
                # undercount receivables (especially for CHW, where three
                # invoice sheets are summed into one plan per HANA).
                raise _RequiredSheetMissing(
                    variant=variant.name,
                    sheet=sc.name,
                    workbook=xlsx_path.name,
                    kind="invoice",
                )
            log.warning(
                "scan[%s:%s]: optional sheet %r missing in %s — skipping",
                pack.workflow_type, variant.name, sc.name, xlsx_path.name,
            )
            continue

        # Fail-closed on schema drift: if AR renames/drops a column this variant
        # filters or projects on, abandon THIS variant only (no emails) rather
        # than silently mailing wrong rows — an absent column reads as None, so
        # a negative predicate would otherwise flip to true. Other variants on
        # the same message are unaffected (per-variant savepoint in _process_one).
        _validate_sheet_columns(
            sc, sheet, variant_name=variant.name, workbook=xlsx_path.name
        )

        for raw in sheet.rows:
            typed: dict[str, Any] = {}
            for key, value in raw.items():
                kind = sc.column_types.get(key, "raw")
                typed[key] = _normalizer.normalize_value(value, kind)  # type: ignore[arg-type]
            if sc.row_filter:
                try:
                    if not _dsl.evaluate(sc.row_filter, typed):
                        dropped += 1
                        continue
                except _dsl.DSLError:
                    log.exception("DSL error on sheet %s", sheet.sheet_name)
                    dropped += 1
                    continue
            if sc.projection:
                for unified_key, spec in sc.projection.items():
                    typed[unified_key] = _resolve_projection(spec, typed)
            if sc.not_due_month_end_mode and _drop_not_due_outside_month(
                typed, sc.not_due_month_end_mode
            ):
                dropped += 1
                continue
            if sc.row_expander:
                # Expand this source row into one NormalizedRow per non-zero
                # age bucket so the email table shows a per-bucket breakdown.
                expanded = _expand_rows_by_bucket(
                    typed, sc.row_expander, sheet.sheet_name, dict(raw)
                )
                if not expanded:
                    # All buckets were zero — no rows emitted; the overall
                    # "not all_rows" guard below will short-circuit if this
                    # is the only sheet.
                    dropped += 1
                all_rows.extend(expanded)
            else:
                all_rows.append(
                    NormalizedRow(source_sheet=sheet.sheet_name, values=typed, raw=dict(raw))
                )

    if not all_rows:
        return []

    clients = _aggregator.aggregate(
        all_rows,
        group_by_keys=variant.group_by_keys,
        party_name_key=variant.party_name_key,
        sum_cols=variant.sum_cols,
        decision_gates=variant.decision_gates,
    )

    if variant.lookup_sheets:
        lookup_maps: list[tuple[Any, dict[str, dict[str, Any]]]] = []
        for ls in variant.lookup_sheets:
            try:
                sheet = await _read_sheet_with_optional_tab_ai_fallback(
                    xlsx_path,
                    ls.name,
                    ls.header_hints,
                    variant_name=variant.name,
                    workflow_type=pack.workflow_type,
                    name_aliases=ls.name_aliases,
                    sheet_kind="lookup",
                )
            except KeyError:
                if ls.required:
                    raise _RequiredSheetMissing(
                        variant=variant.name,
                        sheet=ls.name,
                        workbook=xlsx_path.name,
                        kind="lookup",
                    )
                log.warning(
                    "scan[%s:%s]: optional lookup sheet %r missing \u2014 skipping",
                    pack.workflow_type, variant.name, ls.name,
                )
                continue

            row_map: dict[str, dict[str, Any]] = {}
            for raw_row in sheet.rows:
                typed_lookup_row: dict[str, Any] = dict(raw_row)
                for src_key in set((ls.key_column, *ls.value_columns.values())):
                    if src_key in typed_lookup_row:
                        value_kind = next(
                            (
                                ls.value_types.get(out_name, "raw")
                                for out_name, configured_src_key in ls.value_columns.items()
                                if configured_src_key == src_key
                            ),
                            "str" if src_key == ls.key_column else "raw",
                        )
                        typed_lookup_row[src_key] = _normalizer.normalize_value(
                            typed_lookup_row.get(src_key),  # type: ignore[arg-type]
                            value_kind,
                        )

                key_val = _lookup_row_key(typed_lookup_row, ls.key_column)
                if key_val is None:
                    continue
                norm_key = str(key_val).strip()
                if not norm_key:
                    continue
                if ls.row_filter is not None:
                    try:
                        if not _dsl.evaluate(ls.row_filter, typed_lookup_row):
                            continue
                    except _dsl.DSLError:
                        log.exception(
                            "DSL error on lookup sheet %s — skipping row", ls.name,
                        )
                        continue
                facts: dict[str, Any] = {}
                for out_name, src_key in ls.value_columns.items():
                    kind = ls.value_types.get(out_name, "raw")
                    facts[out_name] = _normalizer.normalize_value(
                        typed_lookup_row.get(src_key), kind  # type: ignore[arg-type]
                    )
                prior = row_map.get(norm_key)
                if prior is None:
                    row_map[norm_key] = facts
                    continue
                # Multiple rows per key after filtering — sum Decimals so an
                # aggregate lookup (e.g. Unaccounted Revenue split across
                # ledgers for the same HANA under one BU) lands on the
                # client once with the correct total. Non-numeric values
                # fall back to last-write-wins.
                merged = dict(prior)
                for out_name, new_val in facts.items():
                    old_val = merged.get(out_name)
                    if isinstance(old_val, Decimal) and isinstance(new_val, Decimal):
                        merged[out_name] = old_val + new_val
                    else:
                        merged[out_name] = new_val
                row_map[norm_key] = merged
                log.debug(
                    "lookup[%s] duplicate key %r — merged (Decimals summed)",
                    ls.name, norm_key,
                )
            lookup_maps.append((ls, row_map))

        for client in clients:
            for ls, row_map in lookup_maps:
                facts = row_map.get(client.business_key)
                if facts:
                    client.facts.update(facts)

    # Post-lookup skip gates. ``unaccounted_revenue`` is only available after the
    # lookup pass, so this runs here — not in the aggregator.
    #
    # ePharma / CHW (Jun-2026+ dual totals): materiality on T; C gates on
    # overdue-only tables — skip when C ≤ 0 (and C < ₹1 dust). Tables with
    # Due this Month rows send when T ≥ ₹1 regardless of C.
    if variant.renderer and variant.renderer.net_pending_keys:
        min_inr = _renderer.MIN_PAYMENT_REMINDER_OUTSTANDING_INR
        cfg = variant.renderer
        epharma_dual_totals = variant.name == "epharma" and bool(cfg.overdue_keys)
        # CHW and both aggregator variants share the same dual-total gate + the
        # UNIFIED_AGEING "Due this Month" upcoming detection (ePharma uses
        # UNIFIED_STATUS instead, hence its own branch above).
        chw_dual_totals = (
            variant.name
            in ("chw", "diagnostics_aggregator", "platform_aggregator")
            and bool(cfg.overdue_keys)
        )
        for client in clients:
            net_pending, unaccounted, total_outstanding = _renderer.compute_totals(
                client, cfg
            )
            overdue_exposure = Decimal("0")
            if epharma_dual_totals or chw_dual_totals:
                for k in cfg.overdue_keys:
                    v = client.totals.get(k)
                    if isinstance(v, Decimal):
                        overdue_exposure += v

            if epharma_dual_totals:
                has_upcoming = _eph_has_upcoming_invoice_rows(client)
                current_overdue = total_outstanding  # C = O − U
                if (
                    pack.workflow_type == "PAYMENT_REMINDER_WEEKLY"
                    and net_pending < min_inr
                ):
                    client.review_reasons.append(
                        {
                            "code": "insufficient_outstanding",
                            "detail": (
                                f"net_pending={net_pending} unaccounted={unaccounted} "
                                f"→ total_amount_pending={net_pending} < {min_inr} INR"
                            ),
                            "human_message": (
                                "Total pending across all listed invoices is below "
                                "the ₹1 materiality floor. A customer reminder is "
                                "not sent."
                            ),
                            "suggested_action": (
                                "Reconcile the workbook; if a real balance remains, "
                                "ensure total pending is at least ₹1 before "
                                "re-triggering."
                            ),
                        }
                    )
                elif not has_upcoming and current_overdue <= 0:
                    client.review_reasons.append(
                        {
                            "code": "potential_overpayment",
                            "detail": (
                                f"overdue_pending={overdue_exposure} "
                                f"unaccounted={unaccounted} "
                                f"→ current_total_overdue={current_overdue} ≤ 0"
                            ),
                            "human_message": (
                                "Customer's overdue amount is at or below the "
                                "unaccounted receipts already recorded against them. "
                                "Sending a reminder now may damage the relationship."
                            ),
                            "suggested_action": (
                                "Reconcile receipts before sending. If the unaccounted "
                                "receipts are a data issue, fix the source sheet and "
                                "re-trigger; otherwise Reject with reason 'overpaid'."
                            ),
                        }
                    )
                elif (
                    not has_upcoming
                    and 0 < current_overdue < min_inr
                ):
                    client.review_reasons.append(
                        {
                            "code": "insufficient_outstanding",
                            "detail": (
                                f"overdue_pending={overdue_exposure} "
                                f"unaccounted={unaccounted} "
                                f"→ current_total_overdue={current_overdue} "
                                f"< {min_inr} INR"
                            ),
                            "human_message": (
                                "Current total overdue after unaccounted receipts "
                                "is below the ₹1 materiality floor. A customer "
                                "reminder is not sent."
                            ),
                            "suggested_action": (
                                "Reconcile the workbook; if a real overdue balance "
                                "remains, ensure current total overdue is at least "
                                "₹1 before re-triggering."
                            ),
                        }
                    )
            elif chw_dual_totals:
                has_upcoming = _chw_has_upcoming_invoice_rows(client)
                current_overdue = total_outstanding  # C = O − U
                if (
                    pack.workflow_type == "PAYMENT_REMINDER_WEEKLY"
                    and net_pending < min_inr
                ):
                    client.review_reasons.append(
                        {
                            "code": "insufficient_outstanding",
                            "detail": (
                                f"net_pending={net_pending} unaccounted={unaccounted} "
                                f"→ total_amount_pending={net_pending} < {min_inr} INR"
                            ),
                            "human_message": (
                                "Total pending across all listed invoices is below "
                                "the ₹1 materiality floor. A customer reminder is "
                                "not sent."
                            ),
                            "suggested_action": (
                                "Reconcile the workbook; if a real balance remains, "
                                "ensure total pending is at least ₹1 before "
                                "re-triggering."
                            ),
                        }
                    )
                elif not has_upcoming and current_overdue <= 0:
                    client.review_reasons.append(
                        {
                            "code": "potential_overpayment",
                            "detail": (
                                f"overdue_pending={overdue_exposure} "
                                f"unaccounted={unaccounted} "
                                f"→ current_total_overdue={current_overdue} ≤ 0"
                            ),
                            "human_message": (
                                "Customer's overdue amount is at or below the "
                                "unaccounted receipts already recorded against them. "
                                "Sending a reminder now may damage the relationship."
                            ),
                            "suggested_action": (
                                "Reconcile receipts before sending. If the unaccounted "
                                "receipts are a data issue, fix the source sheet and "
                                "re-trigger; otherwise Reject with reason 'overpaid'."
                            ),
                        }
                    )
                elif (
                    not has_upcoming
                    and 0 < current_overdue < min_inr
                ):
                    client.review_reasons.append(
                        {
                            "code": "insufficient_outstanding",
                            "detail": (
                                f"overdue_pending={overdue_exposure} "
                                f"unaccounted={unaccounted} "
                                f"→ current_total_overdue={current_overdue} "
                                f"< {min_inr} INR"
                            ),
                            "human_message": (
                                "Current total overdue after unaccounted receipts "
                                "is below the ₹1 materiality floor. A customer "
                                "reminder is not sent."
                            ),
                            "suggested_action": (
                                "Reconcile the workbook; if a real overdue balance "
                                "remains, ensure current total overdue is at least "
                                "₹1 before re-triggering."
                            ),
                        }
                    )
            elif total_outstanding <= 0:
                client.review_reasons.append(
                    {
                        "code": "potential_overpayment",
                        "detail": (
                            f"net_pending={net_pending} "
                            f"unaccounted={unaccounted} "
                            f"→ total_outstanding={total_outstanding} ≤ 0"
                        ),
                        "human_message": (
                            "Customer's net pending amount is at or below the "
                            "unaccounted receipts already recorded against them. "
                            "Sending a reminder now may damage the relationship."
                        ),
                        "suggested_action": (
                            "Reconcile receipts before sending. If the unaccounted "
                            "receipts are a data issue, fix the source sheet and "
                            "re-trigger; otherwise Reject with reason 'overpaid'."
                        ),
                    }
                )
            elif (
                pack.workflow_type == "PAYMENT_REMINDER_WEEKLY"
                and total_outstanding < min_inr
            ):
                client.review_reasons.append(
                    {
                        "code": "insufficient_outstanding",
                        "detail": (
                            f"net_pending={net_pending} unaccounted={unaccounted} "
                            f"→ total_outstanding={total_outstanding} < {min_inr} INR"
                        ),
                        "human_message": (
                            "Total overdue after unaccounted receipts is below the "
                            "₹1 materiality floor (including float rounding noise). "
                            "A customer reminder is not sent."
                        ),
                        "suggested_action": (
                            "Reconcile the workbook; if a real balance remains, "
                            "ensure outstanding is at least ₹1 before re-triggering."
                        ),
                    }
                )

    # Resolved once per variant run (opens the workbook), not per client.
    aggregator_as_on_clause = (
        _receivable_as_on_clause_html(xlsx_path)
        if variant.name in ("diagnostics_aggregator", "platform_aggregator")
        else ""
    )

    plans: list[dict[str, Any]] = []
    for client in clients:
        rpo_block_html = ""
        kam_block_html = ""
        if (
            variant.name in ("chw", "epharma")
            and tracker_rows
            and variant.resolver
        ):
            mm_rows = _resolver.tracker_rows_for_key(
                tracker_rows,
                business_key_parts=client.business_key_parts,
                key_columns=variant.resolver.key_columns,
            )
            if variant.name == "chw" and _chw_mm.chw_exclusion_skips(mm_rows):
                client.review_reasons.append(
                    {
                        "code": "skip:reminder_exclusion",
                        "detail": (
                            f"Auto-Reminder Exclusion=Yes for HANA {client.business_key!r} "
                            "on at least one Mail Master row"
                        ),
                        "human_message": (
                            "This HANA is marked for exclusion from auto-reminders "
                            "in the CHW Mail Master (Auto-Reminder Exclusion = Yes)."
                        ),
                        "suggested_action": (
                            "Set Auto-Reminder Exclusion to No in the Mail Master to "
                            "resume automated reminders for this key."
                        ),
                    }
                )
            elif variant.name == "epharma" and _eph_mm.eph_exclusion_skips(
                mm_rows
            ):
                client.review_reasons.append(
                    {
                        "code": "skip:reminder_exclusion",
                        "detail": (
                            f"Auto Emailer Exclusion=Yes for business key "
                            f"{client.business_key!r} on at least one ePharma master row"
                        ),
                        "human_message": (
                            "This customer is marked for exclusion from auto-reminders "
                            "in the ePharma master tracker (Auto Emailer Exclusion = Yes)."
                        ),
                        "suggested_action": (
                            "Set Auto Emailer Exclusion to No in the ePharma master to "
                            "resume automated reminders for this key."
                        ),
                    }
                )
            else:
                if variant.name == "chw":
                    rpo_block_html = _chw_mm.chw_rpo_contacts_block_html(mm_rows)
                elif variant.name == "epharma":
                    kam_block_html = _eph_mm.eph_kam_contacts_block_html(mm_rows)

        skip_gate = next(
            (r for r in client.review_reasons if _is_skip_gate(str(r.get("code", "")))),
            None,
        )
        review_reasons: list[dict[str, str]] = [
            dict(r) for r in client.review_reasons
            if not _is_skip_gate(str(r.get("code", "")))
        ]

        resolved: _resolver.ResolvedRecipients | None = None
        if not skip_gate and variant.resolver:
            resolved = _resolver.resolve(
                tracker_rows,
                business_key_parts=list(client.business_key_parts),
                config=variant.resolver,
                # Keep business-intended recipients stable regardless of
                # wire-level test redirect. The sender handles test-mode
                # redirection; resolver should still include variant static
                # fallbacks (AR desk) so previews, persisted audit rows, and
                # TEST MODE banners show who the email was actually meant for.
                test_mode=False,
            )
            review_reasons.extend(dict(r) for r in resolved.review_reasons)

        if variant.resolver and (
            not variant.resolver.to_columns and not variant.resolver.cc_columns
        ):
            review_reasons.append(
                {
                    "code": "tracker_has_no_recipient_columns",
                    "detail": (
                        f"variant={variant.name}: master tracker has no TO or "
                        "CC columns declared — recipients must be filled in "
                        "manually before approval"
                    ),
                    "human_message": (
                        "Configuration issue: the recipient columns for this "
                        "workflow have not been set up in the master tracker."
                    ),
                    "suggested_action": (
                        "Contact engineering. The workflow pack needs TO/CC "
                        "column names declared before this can auto-resolve."
                    ),
                }
            )

        subject, body_html = ("", "")
        if variant.renderer:
            render_extras: dict[str, object] | None = None
            if variant.name == "chw":
                has_upcoming = _chw_has_upcoming_invoice_rows(client)
                upcoming_clause = (
                    " <b>Additionally, we draw your attention to the upcoming "
                    "invoices listed above - please factor these for your payment "
                    "cycle in advance to avoid further delays.</b>"
                    if has_upcoming
                    else ""
                )
                render_extras = {
                    "rpo_contacts_block_html": rpo_block_html,
                    "upcoming_invoices_clause_html": upcoming_clause,
                }
            elif variant.name == "epharma":
                has_upcoming = _eph_has_upcoming_invoice_rows(client)
                upcoming_clause = (
                    " <b>Additionally, we draw your attention to the upcoming "
                    "invoices listed above - please factor these for your payment "
                    "cycle in advance to avoid further delays.</b>"
                    if has_upcoming
                    else ""
                )
                render_extras = {
                    "kam_contacts_block_html": kam_block_html,
                    "upcoming_invoices_clause_html": upcoming_clause,
                }
            elif variant.name in ("diagnostics_aggregator", "platform_aggregator"):
                # Aggregator bodies carry no KAM/RPO block and no upcoming-invoice
                # clause; the only placeholder is the "as on <date>" intro clause.
                render_extras = {
                    "receivable_as_on_clause_html": aggregator_as_on_clause,
                }
            subject, body_html = _renderer.render(
                client, variant.renderer, extra_context=render_extras
            )
            # Two mutually-exclusive banner conditions, both routing the
            # email to the variant's ``static_to`` (AR desk):
            #
            # 1. ``client_not_in_tracker`` — no tracker row exists for this
            #    business key. AR needs to ADD the customer.
            # 2. ``client_recipient_missing`` — tracker row exists but has
            #    no TO/CC. AR needs to UPDATE the customer's email columns.
            #
            # Both are intentionally NOT review_reasons (the email ships).
            # The banner is rendered as inline HTML so it survives every
            # mail client; the audit signal lives on aggregated_data so
            # ops can SQL-filter without parsing the body.
            if resolved is not None:
                if resolved.client_not_in_tracker:
                    body_html = (
                        _not_in_tracker_banner(client.business_key) + body_html
                    )
                elif resolved.client_recipient_missing:
                    body_html = _MISSING_RECIPIENT_BANNER + body_html

        plans.append(
            {
                "variant": variant.name,
                "workflow_type": pack.workflow_type,
                "business_key": client.business_key,
                "business_key_parts": list(client.business_key_parts),
                "party_name": client.party_name,
                "row_count": len(client.rows),
                "totals": {k: str(v) for k, v in client.totals.items()},
                "source_sheets": sorted({r.source_sheet for r in client.rows}),
                "period_key": period,
                "skip_gate": skip_gate,
                "review_reasons": review_reasons,
                "resolved_to": list(resolved.to) if resolved else [],
                "resolved_cc": list(resolved.cc) if resolved else [],
                "client_recipient_missing": bool(
                    resolved.client_recipient_missing if resolved else False
                ),
                "client_not_in_tracker": bool(
                    resolved.client_not_in_tracker if resolved else False
                ),
                "rendered_subject": subject,
                "rendered_body_html": body_html,
                "sample_rows": [dict(r.raw) for r in client.rows[:50]],
                "dropped_row_count": dropped,
            }
        )

    return plans


def _shape_recipients_for_persist(
    *,
    plan: dict[str, Any],
    variant,
    status: str,
    stored_reasons: list[dict[str, str]] | None,
) -> tuple[list[str], list[str], str]:
    """Compute (resolved_to_addrs, resolved_cc_addrs, rendered_body_html) for persistence.

    Pure function so the skipped-but-notify contract is unit-testable without
    DB / Postgres. Rules encoded here:

    * Non-skipped status \u2192 use the plan's resolved TO/CC/body verbatim.
    * Skipped + variant has ``static_to`` \u2192 replace TO with the static list,
      clear CC, prepend the "Skipped \u2014 <code>: <msg>" banner. This holds
      in **both** production and test mode: a skipped row's only audience
      IS the AR desk; stripping ``static_to`` in test mode would leave
      ``resolved_to_addrs=[]`` and dispatch's ``cardinality > 0`` filter
      would drop the row \u2014 so QA would never see the notification. The
      sender's wire-level redirect still rewrites TO to the test inbox.
    * Skipped + no ``static_to`` \u2192 leave TO/CC empty; dispatch won't pick
      the row up (intentional; ops sees it in the skipped UI and acts).
    """

    to_addrs = list(plan.get("resolved_to") or [])
    cc_addrs = list(plan.get("resolved_cc") or [])
    body_html = plan.get("rendered_body_html") or ""
    if status == "skipped" and variant.resolver and variant.resolver.static_to:
        to_addrs = list(variant.resolver.static_to)
        cc_addrs = []
        body_html = _skip_reason_line(stored_reasons) + body_html
    return to_addrs, cc_addrs, body_html


async def run_variant(
    db: AsyncSession,
    *,
    pack: WorkflowPack,
    variant,
    xlsx_path: Path,
    source_message: EmailAutomationMessage,
    tick_now: datetime,
) -> list[UUID]:
    """Build send plans for one variant and persist them idempotently."""

    tracker_rows = await load_tracker_rows(variant)
    period = period_key(pack.period_strategy, source_message.received_at, fallback=tick_now)

    plans = await build_variant_plans(
        pack=pack,
        variant=variant,
        xlsx_path=xlsx_path,
        tracker_rows=tracker_rows,
        period=period,
    )

    new_ids: list[UUID] = []
    for plan in plans:
        skip_gate = plan.get("skip_gate") or None
        review_reasons = list(plan.get("review_reasons") or [])

        # Some review reasons are *informational* — the resolver attached them
        # so ops can clean up the master tracker at their leisure, but the row
        # is still safe to ship. Filter them out of the skip-gate decision so
        # the email goes out; they remain in ``stored_reasons`` for audit.
        blocking_reasons = [
            r for r in review_reasons
            if r.get("code") not in _NON_BLOCKING_REVIEW_REASON_CODES
        ]
        if skip_gate or blocking_reasons:
            status = "skipped"
        elif settings.email_automation_require_approval:
            status = "rendered"
        else:
            status = "approved"

        stored_reasons = (
            review_reasons + ([skip_gate] if skip_gate else [])
            if (review_reasons or skip_gate)
            else None
        )

        to_addrs, cc_addrs, body_html = _shape_recipients_for_persist(
            plan=plan, variant=variant, status=status, stored_reasons=stored_reasons,
        )

        dedupe = dedupe_key(
            pack.workflow_type, variant.name, plan["business_key"], period
        )
        aggregated_snapshot = jsonable(
            {
                "variant": variant.name,
                "business_key_parts": plan.get("business_key_parts") or [],
                "row_count": plan.get("row_count") or 0,
                "totals": plan.get("totals") or {},
                "source_sheets": plan.get("source_sheets") or [],
                "period_key": period,
                "sample_rows": plan.get("sample_rows") or [],
                # Audit signals for the two AR-desk fallback paths. Mutually
                # exclusive — ops SQL-filters on whichever they're triaging:
                #   * client_recipient_missing → tracker row exists, no TO/CC
                #   * client_not_in_tracker    → no tracker row at all
                "client_recipient_missing": bool(
                    plan.get("client_recipient_missing")
                ),
                "client_not_in_tracker": bool(
                    plan.get("client_not_in_tracker")
                ),
            }
        )

        stmt = (
            pg_insert(EmailAutomationSend)
            .values(
                source_message_id=source_message.id,
                workflow_type=pack.workflow_type,
                variant=variant.name,
                business_key=plan["business_key"],
                period_key=period,
                dedupe_key=dedupe,
                status=status,
                review_reasons=jsonable(stored_reasons) if stored_reasons else None,
                resolved_to_addrs=to_addrs,
                resolved_cc_addrs=cc_addrs,
                rendered_subject=plan.get("rendered_subject") or "",
                rendered_body_html=body_html,
                aggregated_data=aggregated_snapshot,
                test_mode=bool(settings.email_automation_test_mode),
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(EmailAutomationSend.id)
        )
        result = await db.execute(stmt)
        inserted = result.scalar_one_or_none()
        if inserted is not None:
            new_ids.append(inserted)
    return new_ids


# ---------------------------------------------------------------------------
# Classify + process loop (per-message)
# ---------------------------------------------------------------------------


def _previous_variant_errors(msg: EmailAutomationMessage) -> dict[str, dict[str, Any]]:
    """Decode prior variant retry state from ``msg.error`` (if any).

    Malformed shapes are treated as "no prior state" \u2014 but they're
    *logged* loudly. A corrupt ``error`` column would otherwise cause the
    orphan picker to mark the message ``failed`` (because every variant
    appears cap-exhausted) with zero breadcrumbs for ops; logging here
    gives them the provider id to grep for.
    """

    raw = msg.error or {}
    if not isinstance(raw, dict):
        log.error(
            "msg=%s has non-dict error column (%s); retry state lost, "
            "treating as empty",
            msg.provider_message_id, type(raw).__name__,
        )
        return {}
    prev = raw.get("variant_errors")
    if prev is None:
        return {}
    if not isinstance(prev, dict):
        log.error(
            "msg=%s has non-dict variant_errors payload (%s); retry state "
            "lost, treating as empty",
            msg.provider_message_id, type(prev).__name__,
        )
        return {}
    return prev


async def _process_one(
    db: AsyncSession,
    *,
    msg_row: EmailAutomationMessage,
    fetched: gmail_sa.FetchedMessage,
    classifier: _classifier.InboundClassifier,
    attachment_root: Path,
    tick_now: datetime,
    only_variants: set[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Classify + process a single message. Returns (sends_queued, per_message_row)."""

    classification = classifier.classify(fetched)
    if classification is None:
        msg_row.status = "skipped"
        msg_row.classified_as = "none"
        msg_row.processed_at = now_utc()
        await db.commit()
        return 0, {"msg_id": fetched.id, "classified": None, "sends": 0}

    if msg_row.status != "processed_with_errors":
        msg_row.classified_as = classification.workflow_type
        msg_row.workflow_type = classification.workflow_type
        msg_row.status = "classified"
        await db.commit()

    pack = REGISTRY.get(classification.workflow_type)
    if pack is None:
        # Classifier returned a workflow_type that isn't in the registry — stale
        # DB row after a rename, or a pack was removed. Mark the message failed
        # with a clear code so the batch proceeds and ops can triage via SQL.
        log.error(
            "unknown_workflow_type: msg=%s classified_as=%r — pack not in "
            "REGISTRY; marking message failed so the batch can proceed",
            fetched.id, classification.workflow_type,
        )
        msg_row.status = "failed"
        msg_row.error = jsonable(
            {
                "stage": "registry_lookup",
                "error": f"unknown_workflow_type:{classification.workflow_type}",
                "type": "UnknownWorkflowType",
            }
        )
        msg_row.processed_at = now_utc()
        await db.commit()
        return 0, {
            "msg_id": fetched.id,
            "classified": classification.workflow_type,
            "error": "unknown_workflow_type",
        }
    dest_dir = attachment_root / fetched.id
    try:
        downloaded_path = await asyncio.to_thread(
            gmail_sa.download_attachment,
            fetched.id,
            classification.attachment.gmail_attachment_id,
            classification.attachment.filename,
            dest_dir,
        )
        xlsx_path = await asyncio.to_thread(
            resolve_downloaded_workbook, downloaded_path
        )
    except WorkbookResolveError as e:
        log.warning("resolve_workbook failed for msg %s: %s", fetched.id, e)
        msg_row.status = "failed"
        msg_row.error = jsonable(
            {"stage": "resolve_workbook", "error": str(e), "type": type(e).__name__}
        )
        msg_row.processed_at = now_utc()
        await db.commit()
        cleanup_attachment_staging_dir(dest_dir)
        return 0, {
            "msg_id": fetched.id,
            "classified": classification.workflow_type,
            "error": str(e),
        }
    except Exception as e:
        log.exception("download_attachment failed for msg %s", fetched.id)
        msg_row.status = "failed"
        msg_row.error = jsonable(
            {"stage": "download_attachment", "error": str(e), "type": type(e).__name__}
        )
        msg_row.processed_at = now_utc()
        await db.commit()
        cleanup_attachment_staging_dir(dest_dir)
        return 0, {
            "msg_id": fetched.id,
            "classified": classification.workflow_type,
            "error": str(e),
        }

    prior_errors = _previous_variant_errors(msg_row)
    per_variant_counts: dict[str, int] = {}
    variant_errors: dict[str, dict[str, Any]] = {}
    total_sends = 0
    for variant in pack.iter_variants():
        if only_variants is not None and variant.name not in only_variants:
            continue
        prev = prior_errors.get(variant.name) or {}
        prev_attempts = int(prev.get("attempts") or 0)
        if prev_attempts >= settings.email_automation_variant_max_attempts:
            # F2 cap — leave as-is, surface in result so ops see it.
            log.error(
                "variant retry cap hit: msg=%s variant=%s attempts=%d — no further auto-retry",
                fetched.id, variant.name, prev_attempts,
            )
            variant_errors[variant.name] = {
                "attempts": prev_attempts,
                "error": prev.get("error") or "max_attempts_reached",
                "type": prev.get("type") or "MaxAttempts",
                "last_at": now_utc().isoformat(),
            }
            per_variant_counts[variant.name] = 0
            continue

        sp = await db.begin_nested()
        try:
            inserted_ids = await run_variant(
                db,
                pack=pack,
                variant=variant,
                xlsx_path=xlsx_path,
                source_message=msg_row,
                tick_now=tick_now,
            )
            await sp.commit()
            per_variant_counts[variant.name] = len(inserted_ids)
            total_sends += len(inserted_ids)
        except Exception as e:
            await sp.rollback()
            log.exception(
                "variant processing failed: msg=%s variant=%s attempt=%d",
                fetched.id, variant.name, prev_attempts + 1,
            )
            variant_errors[variant.name] = {
                "attempts": prev_attempts + 1,
                "error": str(e),
                "type": type(e).__name__,
                "last_at": now_utc().isoformat(),
            }
            per_variant_counts[variant.name] = 0

    # After plans exist: rollup uses approved/rendered/sent so the current
    # week's rows are visible before dispatch marks them sent.
    try:
        await db.flush()
        from app.services.receivable_dashboard import try_ingest_receivable_snapshot

        await try_ingest_receivable_snapshot(
            db, xlsx_path, source_message_id=msg_row.id
        )
    except Exception:
        log.exception("receivable dashboard snapshot failed")

    if variant_errors and not any(per_variant_counts.values()):
        # Every variant we attempted failed. If at least one variant has
        # retries left, keep the message retriable; otherwise terminal.
        any_retriable = any(
            int(v.get("attempts") or 0) < settings.email_automation_variant_max_attempts
            for v in variant_errors.values()
        )
        msg_row.status = "processed_with_errors" if any_retriable else "failed"
    elif variant_errors:
        msg_row.status = "processed_with_errors"
    else:
        msg_row.status = "processed"
    msg_row.error = jsonable({"variant_errors": variant_errors}) if variant_errors else None
    msg_row.processed_at = now_utc()
    await db.commit()

    cleanup_attachment_staging_dir(dest_dir)

    return total_sends, {
        "msg_id": fetched.id,
        "classified": classification.workflow_type,
        "variant_sends": per_variant_counts,
        "variant_errors": variant_errors,
    }


async def classify_and_process_received(
    db: AsyncSession,
    *,
    workflow_types: Sequence[str] | None = None,
    ingested_hot: list[IngestedMessage] | None = None,
    inbox_query: str | None = None,
) -> dict[str, Any]:
    """Pick up ``received`` + ``processed_with_errors`` messages and process them.

    ``ingested_hot`` is an optimization: when the ingest node just fetched a
    batch of messages in the same process, we pass the already-fetched payloads
    through so classify avoids a second ``messages.get`` round-trip. Anything
    not in ``ingested_hot`` is reconstructed from the DB row (covers retry +
    crash-recovery paths).

    ``inbox_query`` is the exact Gmail search string used at the wire and
    doubles as the source of the classifier's sender allowlist (see
    :func:`_rule_with_overrides`). Passing it through explicitly keeps the
    two layers in lockstep: an HTTP ``/scan`` caller that narrowed the
    ``from:`` clause won't accidentally process senders allowed by the
    default settings value. Omit (``None``) to fall back to
    ``settings.email_automation_inbox_query``.
    """

    if not settings.email_automation_enabled:
        return {"message_count": 0, "send_count": 0, "per_message": []}

    effective_query = inbox_query or settings.email_automation_inbox_query
    pack_ids = list(workflow_types) if workflow_types else list(REGISTRY.keys())
    packs = [REGISTRY[w] for w in pack_ids if w in REGISTRY]
    classifier = _classifier.InboundClassifier(
        [_rule_with_overrides(p, inbox_query=effective_query) for p in packs]
    )

    tick_now = now_utc()
    attachment_root = Path(settings.email_automation_attachment_dir)
    attachment_root.mkdir(parents=True, exist_ok=True)

    hot_by_id: dict[str, gmail_sa.FetchedMessage] = {}
    if ingested_hot:
        for item in ingested_hot:
            hot_by_id[item.fetched.id] = item.fetched

    # Orphan-state recovery — the picker MUST cover every status that can be
    # left mid-flight by a SIGKILL / OOM / deploy:
    #
    #   * ``received``              — fresh from ingest (this tick or a previous
    #                                 crashed tick that committed ingest but not
    #                                 classify).
    #   * ``classified``            — committed by ``_process_one`` immediately
    #                                 after classification but BEFORE attachment
    #                                 download + variant processing. This is the
    #                                 dangerous 10-30s window (network I/O for
    #                                 attachment + tracker sheet + Excel parse).
    #                                 A crash here would otherwise wedge the row
    #                                 forever: status is "classified", picker
    #                                 ignored it, ops would have to find it by
    #                                 hand.
    #   * ``processed_with_errors`` — F2 per-variant retry path.
    #
    # Replay is safe because every downstream side-effect is idempotent:
    #   - ``classified_as`` / ``workflow_type`` overwrite with the same value.
    #   - Gmail ``download_attachment`` is keyed by stable ``attachmentId``.
    #   - ``EmailAutomationSend`` inserts use ``ON CONFLICT (dedupe_key) DO NOTHING``,
    #     so any plans persisted by the previous attempt are no-ops on replay.
    #   - Email *sending* is gated by ``EmailAutomationSend.status``, which is
    #     a separate phase — re-running classify never causes a duplicate send.
    rows = (
        await db.execute(
            select(EmailAutomationMessage).where(
                EmailAutomationMessage.status.in_(
                    ("received", "classified", "processed_with_errors")
                )
            )
        )
    ).scalars().all()

    total_sends = 0
    per_message: list[dict[str, Any]] = []
    for row in rows:
        fetched = hot_by_id.get(row.provider_message_id)
        if fetched is None:
            # Either a retry of a previously-processed message or an orphan
            # from an earlier crashed tick. Reconstruct the classifier input
            # from the DB row — we already stored headers + attachments on
            # ingest so this is free.
            fetched = gmail_sa.FetchedMessage.from_db_row(row)

        only_variants: set[str] | None = None
        if row.status == "processed_with_errors":
            prev = _previous_variant_errors(row)
            only_variants = {
                name for name, v in prev.items()
                if int(v.get("attempts") or 0) < settings.email_automation_variant_max_attempts
            }
            if not only_variants:
                # Every retriable variant has exhausted its cap. Move the
                # message to the terminal ``failed`` state so the picker
                # stops surfacing it every tick (otherwise the row stays
                # ``processed_with_errors`` forever and operators see a
                # growing queue that never drains).
                log.error(
                    "variant retry caps exhausted: msg=%s — moving to 'failed'",
                    row.provider_message_id,
                )
                row.status = "failed"
                row.processed_at = now_utc()
                await db.commit()
                per_message.append(
                    {
                        "msg_id": row.provider_message_id,
                        "classified": row.classified_as,
                        "error": "variant_retry_caps_exhausted",
                    }
                )
                continue

        try:
            sends, summary = await _process_one(
                db,
                msg_row=row,
                fetched=fetched,
                classifier=classifier,
                attachment_root=attachment_root,
                tick_now=tick_now,
                only_variants=only_variants,
            )
            total_sends += sends
            per_message.append(summary)
        except Exception as e:  # noqa: BLE001 — one poison message must not kill the batch
            # Per-message isolation: a SIGSEGV-class failure (OOM, corrupt
            # Excel, bad sheet header, network flake in the middle of an
            # attachment download) previously would bubble up through
            # ``classify_and_process_received`` and abort every subsequent
            # message in the same tick. Isolate here so the batch completes
            # and only the offending message ends up in ``failed``.
            log.exception(
                "poison message: msg=%s \u2014 isolating and continuing",
                row.provider_message_id,
            )
            try:
                # Session may be in a dirty state if a commit half-ran; roll
                # back before we touch the row.
                await db.rollback()
                # Re-fetch under a clean session state so SA doesn't barf on
                # a detached instance.
                fresh = await db.get(EmailAutomationMessage, row.id)
                if fresh is not None:
                    fresh.status = "failed"
                    fresh.error = jsonable(
                        {"stage": "_process_one", "error": str(e), "type": type(e).__name__}
                    )
                    fresh.processed_at = now_utc()
                    await db.commit()
            except Exception:
                log.exception(
                    "poison message cleanup failed msg=%s \u2014 row may remain in "
                    "its prior status; next tick's crash-recovery picker will retry",
                    row.provider_message_id,
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
            per_message.append(
                {
                    "msg_id": row.provider_message_id,
                    "classified": row.classified_as,
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    return {
        "message_count": len(per_message),
        "send_count": total_sends,
        "per_message": per_message,
    }


# ---------------------------------------------------------------------------
# One-shot compatibility entry (HTTP /scan, cron, tests)
# ---------------------------------------------------------------------------


async def scan_and_process(
    db: AsyncSession,
    *,
    query: str | None = None,
    max_results: int = 25,
    workflow_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run one cron tick: ingest (primary + optional supplemental query), Path C, Path R.

    Aligns with :mod:`app.agents.email_automation.graph` scan branch ordering.
    """

    if not settings.email_automation_enabled:
        return {
            "scanned_ids": [],
            "message_count": 0,
            "send_count": 0,
            "per_message": [],
            "collections_result": {},
        }

    summary = await ingest_new_messages(db, query=query, max_results=max_results)
    coll = await process_collections_intelligence_batch(db)
    effective_query = query or settings.email_automation_inbox_query
    outcome = await classify_and_process_received(
        db,
        workflow_types=workflow_types,
        ingested_hot=None,
        inbox_query=effective_query,
    )
    outcome["scanned_ids"] = list(summary.get("scanned_ids") or [])
    outcome["collections_result"] = coll
    return outcome


__all__ = [
    "load_tracker_rows",
    "build_variant_plans",
    "run_variant",
    "classify_and_process_received",
    "scan_and_process",
]
