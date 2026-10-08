"""Render subject + HTML body for a :class:`AggregatedClient`.

Deliberately minimal templating — ``str.format_map`` over a curated, pre-escaped
context. No Jinja, no eval, and only documented placeholders:

``{party_name}`` ``{as_of_date}`` ``{total_outstanding_inr}`` ``{total_overdue_inr}``
``{net_pending_total_inr}`` ``{due_this_month_inr}`` ``{unaccounted_revenue_inr}`` ``{row_count}``
``{invoices_table_html}`` ``{aging_summary_html}``

* CHW: ``extra_context`` may pass ``rpo_contacts_block_html`` (trailing ``_html``) —
  optional Mail Master RPO block after the signature; use ``""`` to omit.
* ePharma: ``kam_contacts_block_html`` — optional KAM + contact line block; ``""`` to omit.

* ``net_pending_total_inr`` — sum of ``RendererConfig.net_pending_keys`` across
  the aggregated invoice rows. This is the raw "Sum of Net Amount Pending" the
  PDF SOP mentions before the Unaccounted Revenue adjustment.
* ``unaccounted_revenue_inr`` — looked up via ``LookupSheetConfig`` and stored
  on ``client.facts['unaccounted_revenue']``. ``₹0.00`` if no row matched.
* ``total_outstanding_inr`` — when ``overdue_keys`` is set (ePharma Jun-2026+),
  ``Σ overdue_keys − unaccounted_revenue`` (``Current Total Overdue``). Otherwise
  ``net_pending_total - unaccounted_revenue``. When ``net_pending_keys`` is
  empty (legacy aging-bucket variants) this falls back to the sum of every
  aggregated total, matching the Grand Total in the workbook.
* ``total_overdue_inr``     — sum of ONLY the canonical keys the variant
  declares as overdue (``RendererConfig.overdue_keys``). Use this in copy
  that talks about "overdue" or "past due" so the wording matches the maths.
  Falls back to ``total_outstanding_inr`` if the variant did not declare
  overdue keys.
* ``due_this_month_inr``    — ``net_pending_total − total_overdue_inr`` when
  overdue keys are set (Not Due / Due this Month slice). Otherwise zero.

The invoice + aging tables are rendered from a small, pack-defined column spec so
e-Pharma and CHW variants stay visually consistent without sharing Excel headers.
Extra ``client.facts`` are exposed as ``{<name>_inr}`` (Decimal) or ``{<name>}``
(str) placeholders.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from zoneinfo import ZoneInfo  # std-lib (3.9+); requires `tzdata` on minimal images.

# The business audience reads receivables in IST. We refuse to silently fall
# back to UTC because a half-day shift in the "as_of_date" line on a payment
# reminder is the kind of subtle wrongness that breaks customer trust.
_IST = ZoneInfo("Asia/Kolkata")

# Minimum total outstanding (INR) for outbound payment reminders: row filters
# and post-lookup gates use this so float dust and sub-rupee totals that
# render as ₹0.00 do not produce customer mail.
MIN_PAYMENT_REMINDER_OUTSTANDING_INR: Decimal = Decimal("1")

from .types import AggregatedClient


def _today_ist_str() -> str:
    """Render today's date in IST."""

    return datetime.now(_IST).strftime("%d-%b-%Y")


def _header_safe(value: object) -> str:
    """Strip RFC-5322 header-breaking characters from a subject context value.

    SMTP headers are CRLF-delimited; if a tracker-supplied party name contains
    ``\\r``/``\\n`` we'd silently inject a fake header (Bcc, X-Spam, …). Replace
    them with a single space rather than truncating so the operator still sees
    something recognizable in the audit trail.
    """

    s = "" if value is None else str(value)
    return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")


@dataclass(frozen=True, slots=True)
class TableColumnSpec:
    """One displayed column. ``source_key`` is a canonical key (or display header)."""

    display: str
    source_key: str  # canonical key produced by the reader
    kind: str = "str"  # "str" | "decimal" | "date"
    align: str = "left"


@dataclass(frozen=True, slots=True)
class RendererConfig:
    subject_template: str
    body_template: str
    invoice_columns: tuple[TableColumnSpec, ...]
    aging_columns: tuple[TableColumnSpec, ...] = ()
    # Canonical keys (e.g. ``"0-1 months#0"``) the renderer treats as "overdue"
    # for the ``{total_overdue_inr}`` placeholder. Keep this empty if the
    # variant does not distinguish overdue from outstanding (in which case
    # ``{total_overdue_inr}`` falls back to the full outstanding total).
    overdue_keys: tuple[str, ...] = ()
    # Canonical keys whose ``client.totals`` entries are summed to produce
    # ``net_pending_total``. When set, ``total_outstanding`` is computed as
    # ``net_pending_total - facts['unaccounted_revenue']`` per the SOP.
    net_pending_keys: tuple[str, ...] = ()
    # Output name on ``client.facts`` that holds the per-key Unaccounted
    # Revenue (typically ``"unaccounted_revenue"``). ``Decimal('0')`` if missing.
    unaccounted_revenue_fact: str = "unaccounted_revenue"
    currency_prefix: str = "\u20b9"


def _fmt_inr(value: Decimal | None, prefix: str = "\u20b9") -> str:
    if value is None:
        return ""
    # Indian grouping: 12,34,567.89
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    s = f"{abs(q):.2f}"
    int_part, _, frac = s.partition(".")
    if len(int_part) <= 3:
        grouped = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        pairs: list[str] = []
        while len(rest) > 2:
            pairs.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.insert(0, rest)
        grouped = ",".join(pairs) + "," + last3
    return f"{sign}{prefix}{grouped}.{frac}"


def _fmt_cell(value: object, kind: str, currency_prefix: str) -> str:
    if value is None or value == "":
        return ""
    if kind == "decimal":
        if isinstance(value, Decimal):
            return _fmt_inr(value, currency_prefix)
        try:
            return _fmt_inr(Decimal(str(value)), currency_prefix)
        except Exception:
            return _html.escape(str(value))
    if kind == "date":
        if isinstance(value, date):
            return value.strftime("%d-%b-%Y")
        return _html.escape(str(value))
    return _html.escape(str(value))


def _table_html(
    rows: Sequence[Mapping[str, object]],
    columns: Sequence[TableColumnSpec],
    currency_prefix: str,
) -> str:
    if not columns:
        return ""
    th = "".join(
        f'<th style="padding:6px 10px;background:#f3f4f6;border:1px solid #e5e7eb;'
        f'text-align:{_html.escape(c.align)};font-family:Arial,sans-serif;'
        f'font-size:12px;">{_html.escape(c.display)}</th>'
        for c in columns
    )
    body_rows: list[str] = []
    for data in rows:
        cells = "".join(
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb;'
            f'text-align:{_html.escape(c.align)};font-family:Arial,sans-serif;'
            f'font-size:12px;">{_fmt_cell(data.get(c.source_key), c.kind, currency_prefix)}</td>'
            for c in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<table style="border-collapse:collapse;border:1px solid #e5e7eb;width:100%;">'
        f"<thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def compute_totals(
    client: AggregatedClient, cfg: RendererConfig
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(net_pending, unaccounted, total_outstanding)`` per the SOP.

    Extracted from :func:`render` so the pipeline's post-lookup skip gates
    (``potential_overpayment``, ``insufficient_outstanding`` below
    :data:`MIN_PAYMENT_REMINDER_OUTSTANDING_INR`) and the rendered email body
    compute the same ``Total Outstanding`` from the same inputs — no drift if
    the SOP formula evolves.

    Formula (SOP):
        When ``overdue_keys`` is set:
            ``Current Total Overdue = Σ overdue_keys − facts[unaccounted_revenue_fact]``
        When only ``net_pending_keys`` is set:
            ``Total Outstanding = Σ net_pending_keys − facts[unaccounted_revenue_fact]``

    Legacy fallback when ``net_pending_keys`` is empty (aging-bucket variants):
    ``total_outstanding`` collapses to the sum of *all* Decimal totals so the
    email still shows a figure rather than zero.
    """

    total_aggregated = Decimal("0")
    for v in client.totals.values():
        if isinstance(v, Decimal):
            total_aggregated += v

    if cfg.net_pending_keys:
        net_pending = Decimal("0")
        for k in cfg.net_pending_keys:
            v = client.totals.get(k)
            if isinstance(v, Decimal):
                net_pending += v
    else:
        net_pending = total_aggregated

    unaccounted_raw = client.facts.get(cfg.unaccounted_revenue_fact)
    unaccounted = unaccounted_raw if isinstance(unaccounted_raw, Decimal) else Decimal("0")

    if cfg.overdue_keys:
        overdue_base = Decimal("0")
        for k in cfg.overdue_keys:
            v = client.totals.get(k)
            if isinstance(v, Decimal):
                overdue_base += v
        total_outstanding = overdue_base - unaccounted
    elif cfg.net_pending_keys:
        total_outstanding = net_pending - unaccounted
    else:
        total_outstanding = total_aggregated

    return net_pending, unaccounted, total_outstanding


def render(
    client: AggregatedClient,
    cfg: RendererConfig,
    *,
    extra_context: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """Return ``(subject, body_html)`` for one aggregated client."""

    net_pending, unaccounted, total_outstanding = compute_totals(client, cfg)

    if cfg.overdue_keys:
        overdue = Decimal("0")
        for k in cfg.overdue_keys:
            v = client.totals.get(k)
            if isinstance(v, Decimal):
                overdue += v
    else:
        overdue = total_outstanding

    due_this_month = Decimal("0")
    if cfg.overdue_keys:
        due_this_month = net_pending - overdue
        if due_this_month < 0:
            due_this_month = Decimal("0")

    invoices_table = _table_html(
        [row.values for row in client.rows],
        cfg.invoice_columns,
        cfg.currency_prefix,
    )
    aging_row: dict[str, object] = {c.source_key: client.totals.get(c.source_key) for c in cfg.aging_columns}
    aging_table = _table_html(
        [aging_row] if cfg.aging_columns else [],
        cfg.aging_columns,
        cfg.currency_prefix,
    )

    party_name_raw = client.party_name or client.business_key
    today_str = _today_ist_str()

    # Subject context: NEVER HTML-escape (subjects are plain text on the wire;
    # ``&amp;`` would render as literal characters in inboxes). DO strip CR/LF —
    # otherwise a tracker-injected newline could forge SMTP headers.
    subject_ctx: dict[str, object] = {
        "party_name": _header_safe(party_name_raw),
        "business_key": _header_safe(client.business_key),
        "as_of_date": today_str,
        "total_outstanding_inr": _fmt_inr(total_outstanding, cfg.currency_prefix),
        "total_overdue_inr": _fmt_inr(overdue, cfg.currency_prefix),
        "net_pending_total_inr": _fmt_inr(net_pending, cfg.currency_prefix),
        "due_this_month_inr": _fmt_inr(due_this_month, cfg.currency_prefix),
        "unaccounted_revenue_inr": _fmt_inr(unaccounted, cfg.currency_prefix),
        "row_count": str(len(client.rows)),
    }

    # Body context: HTML-escaped scalars + pre-built table HTML (whitelist by ``_html`` suffix).
    body_ctx: dict[str, object] = {
        "party_name": _html.escape(party_name_raw),
        "business_key": _html.escape(client.business_key),
        "as_of_date": today_str,
        "total_outstanding_inr": _fmt_inr(total_outstanding, cfg.currency_prefix),
        "total_overdue_inr": _fmt_inr(overdue, cfg.currency_prefix),
        "net_pending_total_inr": _fmt_inr(net_pending, cfg.currency_prefix),
        "due_this_month_inr": _fmt_inr(due_this_month, cfg.currency_prefix),
        "unaccounted_revenue_inr": _fmt_inr(unaccounted, cfg.currency_prefix),
        "row_count": str(len(client.rows)),
        "invoices_table_html": invoices_table,
        "aging_summary_html": aging_table,
    }

    # Expose extra ``client.facts`` (Decimal → ``{name_inr}``; other → ``{name}``).
    for k, v in client.facts.items():
        if k == cfg.unaccounted_revenue_fact:
            continue
        if isinstance(v, Decimal):
            subject_ctx[f"{k}_inr"] = _fmt_inr(v, cfg.currency_prefix)
            body_ctx[f"{k}_inr"] = _fmt_inr(v, cfg.currency_prefix)
        elif v is not None:
            subject_ctx[k] = _header_safe(v)
            body_ctx[k] = _html.escape(str(v))

    if extra_context:
        for k, v in extra_context.items():
            if k.endswith("_html"):
                body_ctx[k] = v
                # Strip raw markup for the subject if someone passes ``_html`` keys.
                subject_ctx[k] = ""
            else:
                body_ctx[k] = _html.escape(str(v))
                subject_ctx[k] = _header_safe(v)

    subject = cfg.subject_template.format_map(_SafeFmt(subject_ctx))
    body = cfg.body_template.format_map(_SafeFmt(body_ctx))
    return subject, body


class _SafeFmt(dict):
    """``format_map`` target that leaves unknown keys intact instead of raising."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"
