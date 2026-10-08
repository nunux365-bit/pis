"""``PAYMENT_REMINDER_WEEKLY`` — ePharmacy, Corporate Wellness (CHW), and Diagnostics variants.

Subject line, email body and invoice table columns are LIFTED VERBATIM from
the AR team's SOP PDFs — the language is already in practice with customers
and must not be paraphrased:

* ``Auto emailer doc.docx.pdf``           — ePharmacy (~200 clients)
* ``Auto-Reminder Document.docx.pdf``     — CHW       (~180 clients)
* ``Payment_Reminder_Process_Flow.pdf``   — CHW       (per-sheet filter detail)

Every variant is handled independently — filter dicts, projections, lookup
configs, recipient resolvers and templates are per-variant. What's shared
(and only where DRY is safe): the unified invoice schema (``UNIFIED_*``
keys) and the physical sheet header hints. If the same HANA Code appears in both variants (multi-BU party) it is
processed twice, producing two independent emails scoped to their BU.

ePharmacy variant
-----------------

* **Source**:  ``Invoice details-H&T`` (Jun-2026+; merges former
  ``Invoice details-H(all)&T(Psp)`` and ``Invoice details-T(labs)``)
* **Filters** (PDF *Auto emailer doc*, Step 2 — all must pass):
    * ``Business Unit = e-Pharmacy`` (strict equality — no ``(Legal)`` etc.)
    * ``Remarks ≠ TDS``
    * ``Consider = Positive``
    * ``Final Amount Pending ≥ 1`` (INR; materiality — avoids sub-rupee / noise rows)
    * ``Ageing type`` may be **Not Due** (upcoming month-end invoices are included
      after a due-date sieve: ``Invoice Date + Credit Days`` must fall in the
      current calendar month, IST).
* **Invoice table columns** (PDF verbatim): ``HANA Code | Name of the party |
  Brand Name | Invoice Date | Invoice No | Final Amount Pending | Status``.
  ``Status`` maps ``Not Due`` → ``Due this Month``; all other ageing → ``Overdue``.
* **Group by**: ``HANA Code`` → one email per HANA Code party.
* **Unaccounted Revenue**: ``Party wise Ageing-H&T`` → ``Unaccounted receipts``
  joined on ``Code`` and **scoped to Business Unit = e-Pharmacy** (lookup
  row filter). Decimals sum if the same HANA appears on >1 BU-matching row.
* **Summary** (customer-facing):
    * ``Amount Pending`` = ``Σ Final Amount Pending`` (Overdue + Due this month)
    * ``Due this month`` = ``Amount Pending − Overdue Amount Pending``
    * ``Overdue Amount Pending`` = ``Σ Final Amount Pending`` where ``Ageing type ≠ Not Due``
    * ``Unaccounted Revenue`` — same lookup as before
    * ``Current Total Overdue`` = ``Overdue Amount Pending − Unaccounted Revenue``
* **Dispatch** (financial gates on **C = Current Total Overdue**):
    * ``insufficient_outstanding`` when ``Total Amount Pending < ₹1``.
    * Table has **any Due this Month row** (Not Due–only or Mixed): send when
      **Amount Pending ≥ ₹1** (``Current Total Overdue`` may be negative).
    * **Overdue-only** table: send when **C > 0**; skip when **C ≤ 0**; also skip
      when ``0 < C < ₹1`` (materiality dust).
* **Subject**: ``Payment Reminder – Overdue Invoices | Tata1mg | <HANA Code>``
  (HANA = grouped ``business_key``).
* **Master tracker** (Google Sheet tab ``Master``; workbook name in ops
  e.g. ``Master for Auto Mailers ePharmacy Monetization.xlsx``):
  TO = ``Email 1..10``, CC = ``KAM Email`` (and optional columns per
  :class:`ResolverConfig`) + the static AR desk address
  (``tata1mg.invoices@1mg.com``). Lookup key: ``BP Code``; KAM / contact block
  and auto-emailer exclusion columns are read in :mod:`eph_mail_master`.

CHW variant
-----------

* **Sources** (merged on a unified schema — *Auto-Reminder Document* Jun-2026+):
    1. ``Invoice details-H&T`` — invoice-level rows
    2. ``Invoice wise DMPL`` — SAP invoice-line export (``Company Code Currency
       Value`` per ``Document Header Text``)
* **Filters**:
    * ``Invoice details-H&T`` → Remarks allowlist + ``Business Unit =
      Corporate Wellness`` + ``Consider = Positive`` + **``Net amount Pending ≥
      1`` OR ``Final Amount Pending ≥ 1``** (INR). ``Ageing type`` may be **Not
      Due**.
    * ``Invoice wise DMPL`` → ``Company Code Currency Value ≥ 1`` (INR) per
      invoice line; ``Customer`` carries the HANA / business key.
* **Party wise Ageing-H&T** is **lookup-only** (Unaccounted Revenue per HANA);
  it is not an email source — parties with no qualifying H&T or DMPL rows are
  not reminded.
* **Subject**: ``Payment Reminder – Overdue Invoices | Tata1mg | <HANA Code>``
  (same pattern as ePharma).
* **Summary** (customer-facing):
    * ``Amount Pending`` = ``Σ Net amount Pending`` (Overdue + Due this month)
    * ``Due this month`` = ``Amount Pending − Overdue Amount Pending``
    * ``Overdue Amount Pending`` = ``Σ Net amount Pending`` where ageing ≠ Not Due
    * ``Unaccounted Revenue`` — partywise lookup
    * ``Current Total Overdue`` = ``Overdue Amount Pending − Unaccounted Revenue``
* **Dispatch** (financial gates on **C = Current Total Overdue**):
    * ``insufficient_outstanding`` when ``Total Amount Pending < ₹1``.
    * Any **Due this Month row** in the table: send when **Amount Pending ≥ ₹1**.
    * **Overdue-only** table: send when **C > 0**; skip when **C ≤ 0**; also skip
      when ``0 < C < ₹1`` (materiality dust).
* **Invoice table columns** (outbound mailer — six): ``HANA Code | Name-HANA |
  Invoice Date | Invoice Number | Net Amount Pending | Ageing``. DMPL rows
  compute ``Ageing`` from ``Net Due Date`` (Overdue vs Due this Month). Future
  DMPL lines are included only when ``Net Due Date`` falls in the current month.
* **Group by**: ``HANA Code`` — one email per party across H&T + DMPL.
* **Unaccounted Revenue**: ``Party wise Ageing-H&T``, scoped to **Corporate
  Wellness**.
* **Master tracker** (Google Sheet ``Mail Master`` tab): TO =
  ``TO 1 (Fill all details basis Col J & K)`` + ``TO 2``…``TO 6``, CC =
  ``CC 1``, ``CC 2``, ``CC3``, ``CC4``. Static TO ``cw_payments@1mg.com``.
  Exclusion: ``Auto-Reminder Exclusion``; RPO block in body.

Diagnostics aggregator variant
------------------------------

* **Source**: ``Receivable as on <date>`` (dated tab title varies per wire;
  pack configures the stem ``Receivable as on``).
* **Filters**:
    * ``Business Unit ∈ {e-Diagnostic-Aggregator,
      e-Pharmacy (Platform Aggregator)}``
    * ``Name of Business owner`` = ``Ashyin Thakral/Prateek verma``
      (case- and whitespace-insensitive)
    * ``Net Due ≥ 1`` (INR)
* **Invoice table columns** (five): ``HANA Code | Name-HANA |
  Business Unit | Net Amount Pending | Ageing``.
  Party-level receivable rows synthesize ``Ageing`` from bucket columns.
  Invoice Date and Invoice Number are not shown (not present on the source
  sheet); Business Unit distinguishes aggregator sub-categories per row.
  The email body states the pending amounts as of the tab's date (extracted
  from the ``Receivable as on <date>`` tab title).
* **Group by**: ``Code`` (HANA).
* **Unaccounted Revenue**: ``Party wise Ageing-H&T`` joined on ``Code``,
  scoped to the two aggregator Business Units (Decimals sum per HANA).
* **Total Outstanding** = ``Sum(Net Due) − Unaccounted Revenue``.
* **Master tracker** (Google Sheet ``Lab`` tab, e.g. *Master Sheet For Recon*):
  lookup key ``Code``; TO = ``Mail ID 1`` (primary client contact) + static TO
  ``himanshu.singh@1mg.com`` (diagnostics desk); CC = ``Mail ID 2``…``Mail ID 11``
  (secondary client contacts). Any CC address already present in TO is deduped
  to avoid duplication.

Skip rules (all variants)
--------------------------

No HITL in this system: rows that can't be rendered cleanly are auto-skipped
and never sent. The pipeline emits ``status = "skipped"`` with a reason code
for any of:

* No invoices for a HANA after filtering → no plan emitted.
* **ePharma** (Jun-2026+ dual totals; gates use **C = Current Total Overdue**):
    * ``insufficient_outstanding`` when ``Total Amount Pending < ₹1``.
    * Any **Due this Month row** in the table (Not Due–only or Mixed): send when
      **Amount Pending ≥ ₹1**.
    * **Overdue-only** table: ``potential_overpayment`` when **C ≤ 0**;
      ``insufficient_outstanding`` when ``0 < C < ₹1``.
* **CHW** (Jun-2026+ dual totals; gates use **C = Current Total Overdue**):
    * ``insufficient_outstanding`` when ``Total Amount Pending < ₹1``.
    * Any **Due this Month row** in the table (Not Due–only or Mixed): send when
      **Amount Pending ≥ ₹1**.
    * **Overdue-only** table: ``potential_overpayment`` when **C ≤ 0**;
      ``insufficient_outstanding`` when ``0 < C < ₹1``.
* **Diagnostics** (legacy single total):
    * ``potential_overpayment`` when ``Total Outstanding ≤ 0``.
    * ``insufficient_outstanding`` when ``0 < Total Outstanding < ₹1``.
* Tracker lookup failed (``tracker_not_found`` / ``tracker_duplicate_keys`` /
  ``tracker_disabled`` / ``no_primary_recipient``).
* Master-tracker auto-reminder exclusion: CHW
  (``Auto-Reminder Exclusion = Yes``) or ePharma
  (``Auto Emailer Exclusion = Yes``) → ``skip:reminder_exclusion`` (shared
  skip code; variant-specific detail text in the pipeline).
"""

from __future__ import annotations

from typing import Mapping

from app.email_automation.engine.classifier import ClassifierRule
from app.email_automation.engine.renderer import RendererConfig, TableColumnSpec
from app.email_automation.engine.resolver import ResolverConfig
from app.email_automation.pipeline.workbook_attachments import RECEIVABLE_ATTACHMENT_REGEX

from .base import LookupSheetConfig, SheetConfig, VariantConfig, WorkflowPack


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-variant static TO addresses — appended to every rendered email's TO
# list in production (see ``ResolverConfig.static_to``). Action-owner for
# the AR / collections desk that has to follow up. Per the requirement:
# different desks per variant. Test mode omits these from the wire.
#
# When the customer's tracker row has no TO and no CC, the static address
# becomes the sole TO and the body picks up a "client email not found" banner
# so the desk knows to follow up manually instead of assuming the customer
# was reached.
_EPHARMA_STATIC_TO = "tata1mg.invoices@1mg.com"
# CHW desk: the Auto-Reminder PDF doesn't pin a specific address. Using the
# same desk as ePharma until AR confirms a CHW-specific mailbox — flip this
# constant when they do; it's a one-line change.
_CHW_STATIC_TO = "cw_payments@1mg.com"
# Diagnostics desk: same AR invoicing mailbox until collections confirms a
# lab-specific address (flip this constant when they do).
_DIAGNOSTICS_STATIC_TO = "himanshu.singh@1mg.com"

_DIAGNOSTICS_AGGREGATOR_BUSINESS_UNITS: tuple[str, ...] = (
    "e-Diagnostic-Aggregator",
    "e-Pharmacy (Platform Aggregator)",
)

_DIAGNOSTICS_AGGREGATOR_BUSINESS_OWNER_PATTERN = (
    r"(?i)^\s*ashyin\s+thakral\s*/\s*prateek\s+verma\s*$"
)

# Jun-2026+ receivables workbook tab titles (legacy aliases kept on SheetConfig).
INVOICE_DETAILS_H_T = "Invoice details-H&T"
INVOICE_DETAILS_LEGACY_ALIASES: tuple[str, ...] = (
    "Invoice details-H(all)&T(Psp)",
    "Invoice detailsH(all)&T(PSP)",
    "Invoice details-T(labs)",
)
PARTY_WISE_AGEING_H_T = "Party wise Ageing-H&T"
PARTY_WISE_AGEING_ALIASES: tuple[str, ...] = (
    "Partywise Ageing H & T",
    "Party wise Ageing-h",
)
INVOICE_WISE_DMPL = "Invoice wise DMPL"
INVOICE_WISE_DMPL_ALIASES: tuple[str, ...] = (
    "Invoice wise DMPL.",
)
# Platform-aggregator (e-Pharmacy (Platform Aggregator)) invoice-line export.
INVOICE_WISE_AGG = "Invoice wise Agg."
INVOICE_WISE_AGG_ALIASES: tuple[str, ...] = (
    "Invoice wise Agg",
    "Invoice wise Aggregator",
)


# ---------------------------------------------------------------------------
# Header hints — help the reader lock onto the correct header row even when
# title rows precede it.
# ---------------------------------------------------------------------------

_INVOICE_H_PSP_HINTS: tuple[str, ...] = (
    "co", "code", "hana code", "name of the party", "brand name",
    "business owner", "kam", "kam 2", "rpt", "invoice date", "invoice no:-",
    "credit days", "original amount from aug'24", "net amount pending",
    "final amount pending", "ageing", "ageing type", "ageing type 2",
    "remarks", "consider", "business unit", "segment", "retail",
)

_INVOICE_T_LABS_HINTS: tuple[str, ...] = (
    "customer code", "hana code", "name-hana", "customer name", "kam", "rpt",
    "business owner", "business unit", "segment", "credit days", "type",
    "doc. no.", "posting date", "invoice number", "original amount",
    "pending amount", "receipts", "tds mapped", "net amount pending",
    "remarks", "knock off status", "due days", "ageing",
)

_INVOICE_DMPL_HINTS: tuple[str, ...] = (
    "company code", "customer", "company code currency value",
    "document header text", "document date", "net due date",
    "customer account", "document number", "posting date",
    # Legacy party-level export (header detection fallback only).
    "entity name", "code", "grand total", "business unit", "bill no.",
)

_INVOICE_AGG_HINTS: tuple[str, ...] = (
    "company code", "fiscal year", "posting period", "customer",
    "company code currency key", "company code currency value",
    "document number", "document header text", "document date",
    "posting date", "reference", "net due date", "search term",
    "customer account: name 1", "month bucket", "ageing date", "day",
    "grac period", "final overdue day", "projected ageing of next month", "bu",
)

_PARTY_WISE_HINTS: tuple[str, ...] = (
    "co", "code", "hana code", "name of the party", "kam 2", "rpt",
    "business unit", "segment", "business owner", "credit days", "not due",
    "0-1 months", "1-3 months", "3-6 months", "6-9 months", "9-12 months",
    "+1yrs", "grand total", "unaccounted receipts", "amount in sap", "remarks",
)

_RECEIVABLE_AS_ON_HINTS: tuple[str, ...] = (
    "entity name", "type", "code", "name of the party", "kam", "rpt",
    "business unit", "segment", "name of business owner", "credit period",
    "check", "receivables", "not due", "0-1 months", "1-3 months",
    "3-6 months", "6-9 months", "9-12 months", "+1yrs", "subsquent receipt",
    "due tds", "net due", "not due tds",
)

_RECEIVABLE_AS_ON_HINTS: tuple[str, ...] = (
    "entity name", "type", "code", "name of the party", "kam", "rpt",
    "business unit", "segment", "name of business owner", "credit period",
    "check", "receivables", "not due", "0-1 months", "1-3 months",
    "3-6 months", "6-9 months", "9-12 months", "+1yrs", "subsquent receipt",
    "due tds", "net due", "not due tds",
)


# ---------------------------------------------------------------------------
# Per-sheet column-type maps (canonical key → declared engine type).
# Unlisted columns default to ``raw`` (kept verbatim for templates / debug).
# ---------------------------------------------------------------------------

_TYPES_INVOICE_H_PSP: dict[str, str] = {
    "co#0": "str", "code#0": "str", "hana code#0": "str",
    "name of the party#0": "str", "brand name#0": "str",
    "business owner#0": "str", "kam 2#0": "str", "rpt#0": "str", "kam#0": "str",
    "invoice date#0": "date", "invoice no:-#0": "str", "credit days#0": "int",
    "original amount from aug'24#0": "decimal", "net amount pending#0": "decimal",
    "final amount pending#0": "decimal", "ageing#0": "str",
    "ageing type#0": "str", "ageing type 2#0": "str",
    "remarks#0": "str", "consider#0": "str",
    "business unit#0": "str", "segment#0": "str",
}

_TYPES_INVOICE_T_LABS: dict[str, str] = {
    "customer code#0": "str", "hana code#0": "str", "name-hana#0": "str",
    "customer name#0": "str", "kam#0": "str", "rpt#0": "str",
    "business owner#0": "str", "business unit#0": "str", "segment#0": "str",
    "credit days#0": "int", "type#0": "str", "doc. no.#0": "str",
    "posting date#0": "date", "invoice number#0": "str",
    "original amount#0": "decimal", "pending amount#0": "decimal",
    "receipts#0": "decimal", "tds mapped#0": "decimal",
    "net amount pending#0": "decimal", "remarks#0": "str",
    # Duplicate "Remarks" — keep both addressable for diagnostics.
    "remarks#1": "str",
    "consider#0": "str",
    "knock off status#0": "str", "due days#0": "int", "ageing#0": "str",
}

_TYPES_INVOICE_DMPL: dict[str, str] = {
    "company code#0": "str",
    "fiscal year#0": "str",
    "posting period#0": "str",
    "customer#0": "str",
    "a#0": "str",
    "company code currency key#0": "str",
    "company code currency value#0": "decimal",
    "document currency key#0": "str",
    "document currency value#0": "decimal",
    "document number#0": "str",
    "document header text#0": "str",
    "document type#0": "str",
    "document date#0": "date",
    "posting date#0": "date",
    "net due date#0": "date",
    "customer account: name 1#0": "str",
    "search term#0": "str",
}

# Platform-aggregator SAP export (``Invoice wise Agg.``). ``Customer`` stays
# ``raw`` so the ``code_str`` projection can strip the ``.0`` that SAP emits on
# numeric HANA codes. ``Month Bucket`` (AT) is the displayed ageing bucket;
# ``Projected Ageing of Next Month`` (AZ, formerly ``Remarks``) is the overdue
# flag AR uses to rescue "Not Due" rows; ``BU`` (BA) scopes the variant.
_TYPES_INVOICE_AGG: dict[str, str] = {
    "company code#0": "str",
    "fiscal year#0": "str",
    "posting period#0": "str",
    "customer#0": "raw",
    "company code currency key#0": "str",
    "company code currency value#0": "decimal",
    "document number#0": "str",
    "document header text#0": "str",
    "document date#0": "date",
    "posting date#0": "date",
    "reference#0": "str",
    "net due date#0": "date",
    "search term#0": "str",
    "customer account: name 1#0": "str",
    "month bucket#0": "str",  # AT — displayed ageing bucket
    "projected ageing of next month#0": "str",  # AZ — overdue flag ("OverDue")
    "bu#0": "str",  # BA — variant scope ("LGP")
}

_TYPES_PARTY_WISE: dict[str, str] = {
    "co#0": "str", "code#0": "str", "hana code#0": "str",
    "name of the party#0": "str",
    "business unit#0": "str", "segment#0": "str",
    "not due#0": "decimal", "0-1 months#0": "decimal", "1-3 months#0": "decimal",
    "3-6 months#0": "decimal", "6-9 months#0": "decimal",
    "9-12 months#0": "decimal", "+1yrs#0": "decimal", "grand total#0": "decimal",
    "unaccounted receipts#0": "decimal",
}

_TYPES_RECEIVABLE_AS_ON: dict[str, str] = {
    "entity name#0": "str", "type#0": "str", "code#0": "str",
    "name of the party#0": "str", "kam#0": "str", "rpt#0": "str",
    "business unit#0": "str", "segment#0": "str",
    "name of business owner#0": "str", "credit period#0": "int",
    "check#0": "int", "receivables#0": "decimal", "not due#0": "decimal",
    "0-1 months#0": "decimal", "1-3 months#0": "decimal",
    "3-6 months#0": "decimal", "6-9 months#0": "decimal",
    "9-12 months#0": "decimal", "+1yrs#0": "decimal",
    "subsquent receipt#0": "decimal", "due tds#0": "decimal",
    "net due#0": "decimal", "not due tds#0": "decimal",
}


# ---------------------------------------------------------------------------
# Unified per-invoice schema. Both ePharma and CHW project to these keys so
# the aggregator can group across sheets and the renderer can show one
# consistent invoice table regardless of the source. Pick names unlikely to
# collide with any source canonical key.
# ---------------------------------------------------------------------------

UNIFIED_HANA = "_hana_code"
UNIFIED_PARTY = "_party_name"
UNIFIED_BRAND = "_brand_name"  # ePharma only — None elsewhere
UNIFIED_BU = "_business_unit"  # Diagnostics aggregator — Business Unit column
UNIFIED_INV_DATE = "_invoice_date"
UNIFIED_INV_NO = "_invoice_no"
UNIFIED_INV_AMOUNT = "_invoice_amount"
UNIFIED_NET_PENDING = "_net_pending"
UNIFIED_OVERDUE_PENDING = "_overdue_pending"
UNIFIED_STATUS = "_status"
UNIFIED_AGEING = "_ageing"
UNIFIED_REMARKS = "_remarks"  # e-Diagnostic-Aggregator — Remarks column on the mailer

# Customer-facing label when source ageing is **Not Due** (ePharma Status / CHW Ageing).
DUE_THIS_MONTH_LABEL = "Due this Month"


# ---------------------------------------------------------------------------
# Row filters (DSL) — predicate uses display names; the DSL appends ``#0``.
# ---------------------------------------------------------------------------

# DSL ``in`` does casefold + NBSP/whitespace-normalised equality (see
# ``dsl._norm_str``), so these literals are matched case-insensitively and
# after ``.strip()``. We enumerate every real-world phrasing observed on the
# AR workbooks because the DSL does NOT do substring matching — a missing
# variant here = silent row drop = customer misses their reminder.
#
# CHW first-``Remarks`` column (invoice status). ``in`` is casefold + NBSP-safe
# exact match — extend when AR adds new literals on the sheets.
# Includes common misspellings seen in trackers.
_REMARKS_INVOICE_OR_PARTIAL: dict[str, object] = {
    "op": "in",
    "col": "Remarks",
    "values": [
        "Invoice",
        "Partial Invoice Pending",
        "Partial Invoice is pending",
        "Partial Amount is pending",
        "Partial amount is pending",
        "Partial invoice is pending",
        "Partial invoice",
        "Partial CN",
        "Partial Invocie",
    ],
}

# ePharma: per Auto emailer doc — five gates (Not Due rows included).
# Business Unit uses strict ``eq`` (case/whitespace-folded by the DSL) so
# adjacent categories like ``e-Pharmacy(Legal)`` or ``e-Pharmacies`` do not
# leak in. PDF §Step 2 names only ``e-Pharmacy``.
_EPHARMA_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "HANA Code"},
        {"op": "eq", "col": "Business Unit", "value": "e-Pharmacy"},
        {"op": "ne", "col": "Remarks", "value": "TDS"},
        {"op": "eq", "col": "Consider", "value": "Positive"},
        {"op": "gte", "col": "Final Amount Pending", "value": 1},
    ],
}

# CHW invoice-level rows on the merged ``Invoice details-H&T`` tab.
_CHW_INVOICE_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "HANA Code"},
        _REMARKS_INVOICE_OR_PARTIAL,
        {
            "op": "regex",
            "col": "Business Unit",
            "pattern": r"(?i)^\s*corporate\s+wellness\s*$",
        },
        {"op": "regex", "col": "Consider", "pattern": r"(?i)^\s*positive\s*$"},
        {
            "op": "or",
            "terms": [
                {"op": "gte", "col": "Net amount Pending", "value": 1},
                {"op": "gte", "col": "Final Amount Pending", "value": 1},
            ],
        },
    ],
}

# Legacy aliases — tests and old docs may still reference these names.
_CHW_T_LABS_FILTER = _CHW_INVOICE_FILTER
_CHW_H_PSP_FILTER = _CHW_INVOICE_FILTER
# Display labels are intentional — the DSL's ``col_key()`` is the single
# source of truth for header normalization (NBSP, casing, whitespace) and
# both column types and predicates pass through it. Passing pre-canonical
# keys here would double-suffix to ``"0-1 months#0#0"`` and silently match
# nothing.
_AGED_BUCKETS_DISPLAY: list[str] = [
    "0-1 Months", "1-3 Months", "3-6 Months",
    "6-9 Months", "9-12 Months", "+1yrs",
]
_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "Code"},
        {
            "op": "in",
            "col": "Business Unit",
            "values": list(_DIAGNOSTICS_AGGREGATOR_BUSINESS_UNITS),
        },
        {
            "op": "regex",
            "col": "Name of Business owner",
            "pattern": _DIAGNOSTICS_AGGREGATOR_BUSINESS_OWNER_PATTERN,
        },
        {"op": "gte", "col": "Net Due", "value": 1},
    ],
}

# e-Diagnostic-Aggregator invoice-level rows on the merged ``Invoice details-H&T``
# tab (Jul-2026+ requirement). Filter: strict Business Unit, a Remarks allowlist
# scoping to still-collectible invoices (Invoice / TDS / Partial), and a ₹1
# materiality floor so fully-settled ₹0 lines never render in the mailer.
# ``Ageing type`` may be **Not Due**; the ``invoice_ht`` month-end sieve keeps
# only those due by month end (Invoice Date + Credit Days ≡ receivable-date + Due
# Days per the SOP, so the two encodings agree on the due date).
_DIAGNOSTICS_AGGREGATOR_REMARKS_ALLOWLIST: list[str] = [
    "Invoice",
    "TDS",
    "Partial Invoice is pending",
    "Partial invoice is pending",
    "Partial Invoice Pending",
    "Partial Invoice",
    "Partial Invocie",
]
_DIAGNOSTICS_AGGREGATOR_H_T_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "HANA Code"},
        {"op": "eq", "col": "Business Unit", "value": "e-Diagnostic-Aggregator"},
        {
            "op": "in",
            "col": "Remarks",
            "values": _DIAGNOSTICS_AGGREGATOR_REMARKS_ALLOWLIST,
        },
        {"op": "gte", "col": "Final Amount Pending", "value": 1},
    ],
}

# e-Pharmacy (Platform Aggregator) invoice-line rows on ``Invoice wise Agg.``.
# Scoped by the ``BU`` column (added Jul-2026) = ``LGP`` — this replaced the
# provisional ``Search term ~ TRUWORTH`` gate once AR added the BU marker.
# A row is kept when it carries a real amount and is collectible: any non-"Not
# Due" ``Month Bucket`` (AT), or a "Not Due" AT whose ``Projected Ageing of Next
# Month`` column (AZ, formerly ``Remarks``) reads "OverDue" — those become
# "Due this Month" in the projection below.
_PLATFORM_AGGREGATOR_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "Customer"},
        {"op": "eq", "col": "BU", "value": "LGP"},
        {"op": "gte", "col": "Company Code Currency Value", "value": 1},
        {
            "op": "or",
            "terms": [
                {"op": "ne", "col": "Month Bucket", "value": "Not Due"},
                {
                    "op": "eq",
                    "col": "Projected Ageing of Next Month",
                    "value": "OverDue",
                },
            ],
        },
    ],
}

# CHW: DMPL — SAP invoice-line export (Jun-2026+).
_CHW_DMPL_FILTER: dict[str, object] = {
    "op": "and",
    "terms": [
        {"op": "is_not_empty", "col": "Customer"},
        {"op": "gte", "col": "Company Code Currency Value", "value": 1},
    ],
}


# ---------------------------------------------------------------------------
# Per-sheet projections — map each source schema onto the unified invoice
# schema so multi-sheet variants (CHW) can aggregate uniformly.
# ---------------------------------------------------------------------------

_PROJ_INVOICE_H_PSP_EPHARMA: dict[str, str | Mapping[str, object]] = {
    UNIFIED_HANA: "hana code#0",
    UNIFIED_PARTY: "name of the party#0",
    UNIFIED_BRAND: "brand name#0",
    UNIFIED_INV_DATE: "invoice date#0",
    UNIFIED_INV_NO: "invoice no:-#0",
    # Per Auto emailer doc PDF: ePharma invoice table shows ONLY
    # "Final Amount Pending" as the amount column — no original/net split.
    UNIFIED_INV_AMOUNT: "final amount pending#0",
    UNIFIED_NET_PENDING: "final amount pending#0",
    UNIFIED_AGEING: "ageing type#0",
    UNIFIED_STATUS: {
        "op": "map_eq_str",
        "col": "ageing type#0",
        "value": "Not Due",
        "then": DUE_THIS_MONTH_LABEL,
        "else": "Overdue",
    },
    UNIFIED_OVERDUE_PENDING: {
        "op": "decimal_unless_eq",
        "col": "ageing type#0",
        "value": "Not Due",
        "amount_col": "final amount pending#0",
    },
}

_PROJ_INVOICE_H_T_CHW: dict[str, str | dict[str, object]] = {
    UNIFIED_HANA: "hana code#0",
    UNIFIED_PARTY: "name of the party#0",
    UNIFIED_BRAND: "brand name#0",
    UNIFIED_INV_DATE: "invoice date#0",
    UNIFIED_INV_NO: "invoice no:-#0",
    UNIFIED_INV_AMOUNT: "original amount from aug'24#0",
    UNIFIED_NET_PENDING: {
        "op": "coalesce_decimal",
        "cols": ["net amount pending#0", "final amount pending#0"],
    },
    UNIFIED_AGEING: {
        "op": "map_eq_str",
        "col": "ageing type#0",
        "value": "Not Due",
        "then": DUE_THIS_MONTH_LABEL,
        "else_col": "ageing type#0",
    },
    UNIFIED_OVERDUE_PENDING: {
        "op": "decimal_unless_eq_coalesce",
        "col": "ageing type#0",
        "value": "Not Due",
        "amount_cols": ["net amount pending#0", "final amount pending#0"],
    },
}

# Legacy projection aliases (tests / docs).
_PROJ_INVOICE_H_PSP_CHW = _PROJ_INVOICE_H_T_CHW

_PROJ_INVOICE_DMPL_CHW: dict[str, str | dict[str, object]] = {
    # SAP invoice-line export — ``Customer`` is the HANA / business key.
    UNIFIED_HANA: "customer#0",
    UNIFIED_PARTY: "customer account: name 1#0",
    UNIFIED_INV_DATE: "document date#0",
    UNIFIED_INV_NO: "document header text#0",
    UNIFIED_INV_AMOUNT: "company code currency value#0",
    UNIFIED_NET_PENDING: "company code currency value#0",
    UNIFIED_AGEING: {
        "op": "ageing_from_due_date",
        "due_col": "net due date#0",
        "not_due_label": DUE_THIS_MONTH_LABEL,
    },
    UNIFIED_OVERDUE_PENDING: {
        "op": "overdue_amount_if_past_due",
        "due_col": "net due date#0",
        "amount_col": "company code currency value#0",
    },
}

# e-Diagnostic-Aggregator projection (``Invoice details-H&T``). Mirrors the CHW
# H&T projection but the amount column is ``Final Amount Pending`` only (no
# net/final coalesce) and it carries a Remarks column for the mailer. Surviving
# Not Due rows (post month-end sieve) relabel to "Due this Month"; overdue net
# pending excludes Not Due rows so the dual-total summary splits correctly.
_PROJ_INVOICE_H_T_DIAG: dict[str, str | dict[str, object]] = {
    UNIFIED_HANA: "hana code#0",
    UNIFIED_PARTY: "name of the party#0",
    UNIFIED_INV_DATE: "invoice date#0",
    UNIFIED_INV_NO: "invoice no:-#0",
    UNIFIED_NET_PENDING: "final amount pending#0",
    UNIFIED_REMARKS: "remarks#0",
    UNIFIED_AGEING: {
        "op": "map_eq_str",
        "col": "ageing type#0",
        "value": "Not Due",
        "then": DUE_THIS_MONTH_LABEL,
        "else_col": "ageing type#0",
    },
    UNIFIED_OVERDUE_PENDING: {
        "op": "decimal_unless_eq",
        "col": "ageing type#0",
        "value": "Not Due",
        "amount_col": "final amount pending#0",
    },
}

# e-Pharmacy (Platform Aggregator) projection (``Invoice wise Agg.``).
#
# Ageing takes the ``Month Bucket`` (AT) value verbatim, EXCEPT when AT is
# "Not Due": the row filter only lets a Not Due row through when ``Projected
# Ageing of Next Month`` (AZ) reads "OverDue", and those rows are labelled
# "Due this Month" per the Jul-2026 requirement.
# Because they are due-this-month rather than overdue, their amount is excluded
# from UNIFIED_OVERDUE_PENDING so the dual-total summary splits correctly:
# Amount Pending = all rows, Overdue = non-Not-Due rows, Due this month = the
# difference (the Not Due + OverDue rows).
_PROJ_INVOICE_AGG: dict[str, str | dict[str, object]] = {
    UNIFIED_HANA: {"op": "code_str", "col": "customer#0"},
    UNIFIED_PARTY: "customer account: name 1#0",
    UNIFIED_INV_DATE: "posting date#0",
    UNIFIED_INV_NO: "reference#0",
    UNIFIED_NET_PENDING: "company code currency value#0",
    UNIFIED_OVERDUE_PENDING: {
        "op": "decimal_unless_eq",
        "col": "month bucket#0",
        "value": "Not Due",
        "amount_col": "company code currency value#0",
    },
    UNIFIED_AGEING: {
        "op": "map_eq_str",
        "col": "month bucket#0",
        "value": "Not Due",
        "then": DUE_THIS_MONTH_LABEL,
        "else_col": "month bucket#0",
    },
}


# ---------------------------------------------------------------------------
# Lookup sheet — Unaccounted Revenue per HANA Code.
#
# ``Party wise Ageing-H&T`` is one physical sheet that carries rows for both
# ePharma and CHW parties. Variants MUST be isolated: a party that appears
# under both Business Units (rare but real) gets a variant-specific
# Unaccounted Revenue, not the sum across BUs. Each variant gets its own
# ``LookupSheetConfig`` with a ``row_filter`` scoped to its Business Unit.
# If the same (HANA, BU) appears on multiple rows after filtering the
# pipeline sums their Decimal values (see ``pipeline.build_variant_plans``).
# ---------------------------------------------------------------------------

_UNACCOUNTED_LOOKUP_EPHARMA = LookupSheetConfig(
    name=PARTY_WISE_AGEING_H_T,
    header_hints=_PARTY_WISE_HINTS,
    key_column="hana code#0",
    name_aliases=PARTY_WISE_AGEING_ALIASES,
    value_columns={"unaccounted_revenue": "unaccounted receipts#0"},
    value_types={"unaccounted_revenue": "decimal"},
    # Mirror the invoice-sheet gate: strict ``e-Pharmacy`` only.
    row_filter={"op": "eq", "col": "Business Unit", "value": "e-Pharmacy"},
)

_UNACCOUNTED_LOOKUP_CHW = LookupSheetConfig(
    name=PARTY_WISE_AGEING_H_T,
    header_hints=_PARTY_WISE_HINTS,
    key_column="hana code#0",
    name_aliases=PARTY_WISE_AGEING_ALIASES,
    value_columns={"unaccounted_revenue": "unaccounted receipts#0"},
    value_types={"unaccounted_revenue": "decimal"},
    # CHW reads the same sheet but scoped to Corporate Wellness rows only.
    row_filter={
        "op": "regex",
        "col": "Business Unit",
        "pattern": r"(?i)^\s*corporate\s+wellness\s*$",
    },
)

_UNACCOUNTED_LOOKUP_DIAGNOSTICS_AGGREGATOR = LookupSheetConfig(
    name=PARTY_WISE_AGEING_H_T,
    header_hints=_PARTY_WISE_HINTS,
    key_column="hana code#0",
    name_aliases=PARTY_WISE_AGEING_ALIASES,
    value_columns={"unaccounted_revenue": "unaccounted receipts#0"},
    value_types={"unaccounted_revenue": "decimal"},
    # Scoped to e-Diagnostic-Aggregator rows only (its own variant now).
    row_filter={
        "op": "eq",
        "col": "Business Unit",
        "value": "e-Diagnostic-Aggregator",
    },
)

_UNACCOUNTED_LOOKUP_PLATFORM_AGGREGATOR = LookupSheetConfig(
    name=PARTY_WISE_AGEING_H_T,
    header_hints=_PARTY_WISE_HINTS,
    key_column="hana code#0",
    name_aliases=PARTY_WISE_AGEING_ALIASES,
    value_columns={"unaccounted_revenue": "unaccounted receipts#0"},
    value_types={"unaccounted_revenue": "decimal"},
    row_filter={
        "op": "eq",
        "col": "Business Unit",
        "value": "e-Pharmacy (Platform Aggregator)",
    },
)


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

# Shared subject — HANA suffix on both variants (grouped ``business_key``).
_SUBJECT_TMPL_WITH_HANA = (
    "Payment Reminder \u2013 Overdue Invoices | Tata1mg | {business_key}"
)

# Aggregator variants also carry the party name (``Name-HANA``) per the Jul-2026
# requirement. ``party_name`` falls back to the business key inside the renderer
# when the sheet has no name cell.
#
# PLACEMENT IS LOAD-BEARING \u2014 do not move the name after the HANA code. Reply
# Tracker tooling recovers the ``business_key`` from the subject's TRAILING
# token and requires the ``\u2026 | Tata1mg | <HANA>`` suffix to stay last:
#   * ``scripts/reconstruct_send_anchors_local.py`` \u2014 regex
#     ``Tata1mg\s*\|\s*([A-Za-z0-9._\-/]+)\s*$`` (no match \u21d2 the thread gets no
#     ``sent`` anchor at all \u21d2 the thread is silently absent from Reply Tracker).
#   * ``scripts/temp_sent_replies_bounces.py::_parse_hana_code`` \u2014 returns the
#     last ``|`` segment (a trailing name would be mistaken for the HANA code).
# Putting the name before ``Tata1mg`` keeps both parsers working untouched, and
# is safe even if the name itself contains ``|`` or the word "Tata1mg".
_SUBJECT_TMPL_WITH_HANA_AND_NAME = (
    "Payment Reminder \u2013 Overdue Invoices | {party_name} | Tata1mg | {business_key}"
)


# Invoice table columns — per-variant, PDF-verbatim display names.
#
# ePharma (PDF *Auto emailer doc*): seven columns including Status.
_EPHARMA_INVOICE_COLUMNS: tuple[TableColumnSpec, ...] = (
    TableColumnSpec(display="HANA Code", source_key=UNIFIED_HANA, kind="str"),
    TableColumnSpec(display="Name of the party", source_key=UNIFIED_PARTY, kind="str"),
    TableColumnSpec(display="Brand Name", source_key=UNIFIED_BRAND, kind="str"),
    TableColumnSpec(display="Invoice Date", source_key=UNIFIED_INV_DATE, kind="date"),
    TableColumnSpec(display="Invoice No", source_key=UNIFIED_INV_NO, kind="str"),
    TableColumnSpec(
        display="Final Amount Pending", source_key=UNIFIED_NET_PENDING,
        kind="decimal", align="right",
    ),
    TableColumnSpec(display="Status", source_key=UNIFIED_STATUS, kind="str"),
)

# CHW (chw_email.pdf): six columns — no separate Invoice Amount in the mailer.
_CHW_INVOICE_COLUMNS: tuple[TableColumnSpec, ...] = (
    TableColumnSpec(display="HANA Code", source_key=UNIFIED_HANA, kind="str"),
    TableColumnSpec(display="Name-HANA", source_key=UNIFIED_PARTY, kind="str"),
    TableColumnSpec(display="Invoice Date", source_key=UNIFIED_INV_DATE, kind="date"),
    TableColumnSpec(display="Invoice Number", source_key=UNIFIED_INV_NO, kind="str"),
    TableColumnSpec(
        display="Net Amount Pending", source_key=UNIFIED_NET_PENDING,
        kind="decimal", align="right",
    ),
    TableColumnSpec(display="Ageing", source_key=UNIFIED_AGEING, kind="str"),
)

# e-Diagnostic-Aggregator table — invoice-level rows from ``Invoice details-H&T``
# with a Remarks column, no Ageing column.
_DIAGNOSTICS_AGGREGATOR_INVOICE_COLUMNS: tuple[TableColumnSpec, ...] = (
    TableColumnSpec(display="HANA Code", source_key=UNIFIED_HANA, kind="str"),
    TableColumnSpec(display="Name-HANA", source_key=UNIFIED_PARTY, kind="str"),
    TableColumnSpec(display="Invoice Date", source_key=UNIFIED_INV_DATE, kind="date"),
    TableColumnSpec(display="Invoice Number", source_key=UNIFIED_INV_NO, kind="str"),
    TableColumnSpec(
        display="Net Amount Pending", source_key=UNIFIED_NET_PENDING,
        kind="decimal", align="right",
    ),
    # Ageing column dropped per AR Jul-2026; UNIFIED_AGEING is still projected
    # per row (drives the overpayment gate + upcoming-invoice clause), just not
    # displayed here.
    TableColumnSpec(display="Remarks", source_key=UNIFIED_REMARKS, kind="str"),
)

# e-Pharmacy (Platform Aggregator) table — invoice-level rows from ``Invoice
# wise Agg.``; no Remarks column on this source sheet, no Ageing column.
_PLATFORM_AGGREGATOR_INVOICE_COLUMNS: tuple[TableColumnSpec, ...] = (
    TableColumnSpec(display="HANA Code", source_key=UNIFIED_HANA, kind="str"),
    TableColumnSpec(display="Name-HANA", source_key=UNIFIED_PARTY, kind="str"),
    TableColumnSpec(display="Invoice Date", source_key=UNIFIED_INV_DATE, kind="date"),
    TableColumnSpec(display="Invoice Number", source_key=UNIFIED_INV_NO, kind="str"),
    TableColumnSpec(
        display="Net Amount Pending", source_key=UNIFIED_NET_PENDING,
        kind="decimal", align="right",
    ),
    # Ageing column dropped per AR Jul-2026 (see diagnostics spec above).
)


# Summary blocks — wording and layout aligned to the AR template PDFs
# (epharmacy_email.pdf / chw_email.pdf).
_SUMMARY_FOOTNOTE = 'style="margin:0;color:#6b7280;font-size:12px;"'

_EPHARMA_SUMMARY_HTML = (
    '<p style="margin-top:14px;"><b>Summary</b></p>'
    '<p style="margin:0;">Amount Pending: {net_pending_total_inr} '
    '<span style="color:#6b7280;font-size:12px;">(Overdue + Due this month)</span></p>'
    '<p style="margin:0;">Due this month: {due_this_month_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "Due this month Amount pending refers to sum of final amount pending of "
    "to be due invoices by monthend.</p>"
    '<p style="margin:0;">Overdue Amount Pending: {total_overdue_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "Overdue Amount pending refers to sum of final amount pending of "
    "Overdue invoices.</p>"
    '<p style="margin:0;">Unaccounted Revenue: {unaccounted_revenue_inr}</p>'
    '<p style="margin:8px 0 0 0;">Current Total Overdue: {total_outstanding_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "(Total Overdue = Sum of Overdue Amount Pending \u2013 Unaccounted Revenue)</p>"
)

_CHW_SUMMARY_HTML = (
    '<p style="margin-top:14px;"><b>Summary</b></p>'
    '<p style="margin:0;">Amount Pending: {net_pending_total_inr} '
    '<span style="color:#6b7280;font-size:12px;">(Overdue + Due this month)</span></p>'
    '<p style="margin:0;">Due this month: {due_this_month_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "Due this month Amount pending refers to sum of Net amount pending of "
    "to be due invoices by monthend.</p>"
    '<p style="margin:0;">Overdue Amount Pending: {total_overdue_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "Overdue Amount pending refers to sum of Net amount pending of "
    "Overdue invoices.</p>"
    '<p style="margin:0;">Unaccounted Revenue: {unaccounted_revenue_inr}</p>'
    '<p style="margin:8px 0 0 0;">Current Total Overdue: {total_outstanding_inr}</p>'
    f'<p {_SUMMARY_FOOTNOTE}>'
    "(Total Overdue = Sum of Overdue Amount Pending \u2013 Unaccounted Revenue)</p>"
)

# Aggregator summary (shared by both aggregator variants). Per AR Jul-2026 this
# shows only a single "Amount to be Paid" total (= Σ Net Amount Pending across
# the listed invoices). The "Due this month" and "Overdue Amount Pending"
# breakdown lines were removed; the overdue split is still computed internally
# for the overpayment skip gate, it is just no longer shown to the customer.
_AGGREGATOR_SUMMARY_HTML = (
    '<p style="margin-top:14px;"><b>Summary</b></p>'
    '<p style="margin:0;">Amount to be Paid: {net_pending_total_inr}</p>'
)


# Body templates — copy aligned to epharmacy_email.pdf / chw_email.pdf.
#
# ePharma: table → Summary → compliance → UTR → brand POC + tata1mginvoice → thanks → sign-off
# + optional KAM block ({kam_contacts_block_html} from master tracker; empty = omit).
# {upcoming_invoices_clause_html} — bold upcoming-invoice sentence when Not Due rows exist.
_BODY_TMPL_EPHARMA = """<!doctype html>
<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#111827;line-height:1.5;">
<p>Dear Partner,</p>
<p>I hope you are doing well.</p>
<p>I am reaching out regarding the payment for online marketing services provided by Tata 1mg. The invoices listed below require your prompt attention.</p>

{invoices_table_html}
""" + _EPHARMA_SUMMARY_HTML + """

<p style="margin-top:14px;">We request you to kindly prioritize the payment of all overdue invoices within the next 7 days, as this is being treated as a compliance concern at our end. Please share the payment advice/UTR details upon processing. If invoices are already paid, request you to share the confirmation at the earliest as it is not yet reflected in our records.{upcoming_invoices_clause_html}</p>
<p style="margin-top:14px;">Additionally, we request you to update the payment advice/UTR with your respective brand POC and tata1mginvoice@1mg.com. This will help ensure timely reconciliation and invoice knock-off.</p>
<p style="margin-top:14px;">Thank you for your continued support.</p>
<p>Warm Regards,<br/>Invoicing &amp; Collections Team<br/>Tata 1mg</p>
{kam_contacts_block_html}
</body></html>
"""

# Aggregator body — matches the Jul-2026 reference mailer: greeting → intro →
# invoice table → summary → compliance → UTR → thanks → sign-off. No KAM/RPO
# contact block, and (per AR request) no upcoming-invoice clause on either
# aggregator variant.
#
# The intro names the Business Unit the invoices belong to, so a party billed
# under both aggregator BUs can tell the two reminders apart. The label is baked
# into the template per variant (rather than passed through ``extra_context``)
# because it is a fixed property of the variant, not of the client. It is
# interpolated BEFORE ``str.format_map`` runs, so a label must never contain
# ``{``/``}`` — both current labels are plain text.
#
# ``{receivable_as_on_clause_html}`` is the workbook's "as on <date>" (read off
# the ``Receivable as on <date>`` tab title in the pipeline). It is the FULL
# clause, so the sentence still reads correctly when the date is unavailable.
def _aggregator_body_template(business_unit_label: str) -> str:
    return (
        """<!doctype html>
<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#111827;line-height:1.5;">
<p>Dear Team,</p>
<p>Greetings from Tata 1mg!</p>
<p>We'd like to draw your attention to the following """
        + business_unit_label
        + """ invoices that remain overdue in our records{receivable_as_on_clause_html}:</p>

{invoices_table_html}
"""
        + _AGGREGATOR_SUMMARY_HTML
        + """

<p style="margin-top:14px;">We request you to prioritize and process the pending overdue payments within the next week and share the payment advice, as this is being treated as a compliance concern.</p>
<p style="margin-top:14px;">If any of the invoices are already paid, please share the UTR or payment advice.</p>
<p style="margin-top:14px;">Thanks for your continued support.</p>
<p>Warm regards,<br/>Invoicing &amp; Collections Team<br/>Tata 1mg</p>
</body></html>
"""
    )


_BODY_TMPL_DIAGNOSTICS_AGGREGATOR = _aggregator_body_template(
    "e-Diagnostic-Aggregator"
)
_BODY_TMPL_PLATFORM_AGGREGATOR = _aggregator_body_template(
    "e-Pharmacy (Platform Aggregator)"
)

_BODY_TMPL_CHW = """<!doctype html>
<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#111827;line-height:1.5;">
<p>Dear Team,</p>
<p>Greetings from Tata 1mg!</p>
<p>We'd like to draw your attention to the following invoices that remain overdue in our records:</p>

{invoices_table_html}
""" + _CHW_SUMMARY_HTML + """

<p style="margin-top:14px;">We request you to prioritize and process the pending overdue payments within the next week and share the payment advice, as this is being treated as a compliance concern.{upcoming_invoices_clause_html}</p>
<p style="margin-top:14px;">If any of the invoices are already paid, please share the UTR or payment advice.</p>
<p style="margin-top:14px;">Additionally, as the central team is now managing collections, we request you to kindly update the payment advice communication email ID to be cw_payments@1mg.com. This will help ensure timely reconciliation and invoice knock-off.</p>
<p style="margin-top:14px;">Thanks for your continued support.</p>
<p>Warm regards,<br/>Invoicing &amp; Collections Team<br/>Tata 1mg</p>
{rpo_contacts_block_html}
</body></html>
"""


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

EPHARMA_VARIANT = VariantConfig(
    name="epharma",
    sheets=(
        SheetConfig(
            name=INVOICE_DETAILS_H_T,
            name_aliases=INVOICE_DETAILS_LEGACY_ALIASES,
            header_hints=_INVOICE_H_PSP_HINTS,
            column_types=_TYPES_INVOICE_H_PSP,
            row_filter=_EPHARMA_FILTER,
            projection=_PROJ_INVOICE_H_PSP_EPHARMA,
            not_due_month_end_mode="invoice_ht",
        ),
    ),
    group_by_keys=(UNIFIED_HANA,),
    party_name_key=UNIFIED_PARTY,
    sum_cols=(UNIFIED_NET_PENDING, UNIFIED_OVERDUE_PENDING),
    decision_gates=(),
    master_sheet_id_setting="email_automation_epharma_master_sheet_id",
    master_tab_setting="email_automation_epharma_master_tab",
    # Production tab ``Master``: preamble row (CC/To) then data headers
    # (KAM, BP Code, KAM Contact Number, Auto Emailer Exclusion, …).
    master_header_row_index=1,
    resolver=ResolverConfig(
        key_columns=("BP Code",),
        to_columns=(
            "Email 1", "Email 2", "Email 3", "Email 4", "Email 5",
            "Email 6", "Email 7", "Email 8", "Email 9", "Email 10",
        ),
        # ``Master`` tab: KAM, BP Code, KAM Contact Number, Auto Emailer Exclusion,
        # Email 1..10, etc. (preamble row then header row; see ``master_header_row_index``).
        # ``Internal Team ID`` often carries an internal invoicing mailbox;
        # ``_extract_emails`` ignores non-address cell text.
        cc_columns=("KAM Email", "Internal Team ID"),
        # AR desk goes in TO (action-owner), not CC. Always present in
        # production; test mode omits it so test runs don't surface in
        # the AR desk's mailbox-side audit. Per-variant by design — the
        # CHW variant uses a different desk address (see CHW_VARIANT).
        static_to=(_EPHARMA_STATIC_TO,),
        static_cc=(),
        enabled_column=None,
    ),
    renderer=RendererConfig(
        subject_template=_SUBJECT_TMPL_WITH_HANA,
        body_template=_BODY_TMPL_EPHARMA,
        invoice_columns=_EPHARMA_INVOICE_COLUMNS,
        aging_columns=(),
        overdue_keys=(UNIFIED_OVERDUE_PENDING,),
        net_pending_keys=(UNIFIED_NET_PENDING,),
        unaccounted_revenue_fact="unaccounted_revenue",
        currency_prefix="\u20b9",
    ),
    lookup_sheets=(_UNACCOUNTED_LOOKUP_EPHARMA,),
)


CHW_VARIANT = VariantConfig(
    name="chw",
    sheets=(
        SheetConfig(
            name=INVOICE_DETAILS_H_T,
            name_aliases=INVOICE_DETAILS_LEGACY_ALIASES,
            header_hints=_INVOICE_H_PSP_HINTS,
            column_types=_TYPES_INVOICE_H_PSP,
            row_filter=_CHW_INVOICE_FILTER,
            projection=_PROJ_INVOICE_H_T_CHW,
            not_due_month_end_mode="invoice_ht",
        ),
        SheetConfig(
            name=INVOICE_WISE_DMPL,
            name_aliases=INVOICE_WISE_DMPL_ALIASES,
            header_hints=_INVOICE_DMPL_HINTS,
            column_types=_TYPES_INVOICE_DMPL,
            row_filter=_CHW_DMPL_FILTER,
            projection=_PROJ_INVOICE_DMPL_CHW,
            not_due_month_end_mode="dmpl",
        ),
    ),
    group_by_keys=(UNIFIED_HANA,),
    party_name_key=UNIFIED_PARTY,
    sum_cols=(UNIFIED_NET_PENDING, UNIFIED_OVERDUE_PENDING),
    decision_gates=(),
    master_sheet_id_setting="email_automation_chw_master_sheet_id",
    master_tab_setting="email_automation_chw_master_tab",
    master_header_row_index=0,
    # CHW Mail Master — verified column titles from production ``Mail Master``:
    # ``HANA Code``, ``TO 1 (Fill all details basis Col J & K)``, ``TO 2``…``TO 6``,
    # ``CC 1``, ``CC 2``, ``CC3``, ``CC4``. CHW desk address goes in TO (action-owner).
    resolver=ResolverConfig(
        key_columns=("HANA Code",),
        to_columns=(
            "TO 1 (Fill all details basis Col J & K)",
            "TO 2",
            "TO 3",
            "TO 4",
            "TO 5",
            "TO 6",
        ),
        cc_columns=("CC 1", "CC 2", "CC3", "CC4"),
        static_to=(_CHW_STATIC_TO,),
        static_cc=(),
        enabled_column=None,
    ),
    renderer=RendererConfig(
        subject_template=_SUBJECT_TMPL_WITH_HANA,
        body_template=_BODY_TMPL_CHW,
        invoice_columns=_CHW_INVOICE_COLUMNS,
        aging_columns=(),
        overdue_keys=(UNIFIED_OVERDUE_PENDING,),
        net_pending_keys=(UNIFIED_NET_PENDING,),
        unaccounted_revenue_fact="unaccounted_revenue",
    ),
    lookup_sheets=(_UNACCOUNTED_LOOKUP_CHW,),
)


# Recipient resolver shared by both aggregator variants — the ``Lab`` master
# tab (Master Sheet For Recon) keyed by ``Code``: Mail ID 1 → TO, Mail ID 2–11
# → CC, plus the static diagnostics desk in TO.
_AGGREGATOR_RESOLVER = ResolverConfig(
    key_columns=("Code",),
    # Primary recipient: only Mail ID 1 goes in TO.
    to_columns=("Mail ID 1",),
    # Secondary recipients: Mail ID 2–11 go in CC.
    cc_columns=tuple(f"Mail ID {i}" for i in range(2, 12)),
    static_to=(_DIAGNOSTICS_STATIC_TO,),
    static_cc=(),
    enabled_column=None,
)


DIAGNOSTICS_AGGREGATOR_VARIANT = VariantConfig(
    name="diagnostics_aggregator",
    sheets=(
        SheetConfig(
            name=INVOICE_DETAILS_H_T,
            name_aliases=INVOICE_DETAILS_LEGACY_ALIASES,
            header_hints=_INVOICE_H_PSP_HINTS,
            column_types=_TYPES_INVOICE_H_PSP,
            row_filter=_DIAGNOSTICS_AGGREGATOR_H_T_FILTER,
            projection=_PROJ_INVOICE_H_T_DIAG,
            # Not Due invoices are kept when due anywhere within the current
            # calendar month (Invoice Date + Credit Days), even if that due
            # date has already passed relative to today — deliberately more
            # lenient than ePharma/CHW's "invoice_ht" mode (which requires the
            # due date to still be strictly upcoming). A row still tagged Not
            # Due by AR whose due date already fell this month is stale, not
            # actually not-due-yet, and should still be flagged to the client.
            not_due_month_end_mode="invoice_ht_aggregator",
        ),
    ),
    group_by_keys=(UNIFIED_HANA,),
    party_name_key=UNIFIED_PARTY,
    sum_cols=(UNIFIED_NET_PENDING, UNIFIED_OVERDUE_PENDING),
    decision_gates=(),
    master_sheet_id_setting="email_automation_diagnostics_master_sheet_id",
    master_tab_setting="email_automation_diagnostics_master_tab",
    # ``Lab`` tab: preamble row (TO/CC) then ``BU``, ``Code``, ``Mail ID 1``…
    master_header_row_index=1,
    resolver=_AGGREGATOR_RESOLVER,
    renderer=RendererConfig(
        subject_template=_SUBJECT_TMPL_WITH_HANA_AND_NAME,
        body_template=_BODY_TMPL_DIAGNOSTICS_AGGREGATOR,
        invoice_columns=_DIAGNOSTICS_AGGREGATOR_INVOICE_COLUMNS,
        aging_columns=(),
        overdue_keys=(UNIFIED_OVERDUE_PENDING,),
        net_pending_keys=(UNIFIED_NET_PENDING,),
        unaccounted_revenue_fact="unaccounted_revenue",
    ),
    lookup_sheets=(_UNACCOUNTED_LOOKUP_DIAGNOSTICS_AGGREGATOR,),
)


PLATFORM_AGGREGATOR_VARIANT = VariantConfig(
    name="platform_aggregator",
    sheets=(
        SheetConfig(
            name=INVOICE_WISE_AGG,
            name_aliases=INVOICE_WISE_AGG_ALIASES,
            header_hints=_INVOICE_AGG_HINTS,
            column_types=_TYPES_INVOICE_AGG,
            row_filter=_PLATFORM_AGGREGATOR_FILTER,
            projection=_PROJ_INVOICE_AGG,
        ),
    ),
    group_by_keys=(UNIFIED_HANA,),
    party_name_key=UNIFIED_PARTY,
    sum_cols=(UNIFIED_NET_PENDING, UNIFIED_OVERDUE_PENDING),
    decision_gates=(),
    master_sheet_id_setting="email_automation_diagnostics_master_sheet_id",
    master_tab_setting="email_automation_diagnostics_master_tab",
    master_header_row_index=1,
    resolver=_AGGREGATOR_RESOLVER,
    renderer=RendererConfig(
        subject_template=_SUBJECT_TMPL_WITH_HANA_AND_NAME,
        body_template=_BODY_TMPL_PLATFORM_AGGREGATOR,
        invoice_columns=_PLATFORM_AGGREGATOR_INVOICE_COLUMNS,
        aging_columns=(),
        overdue_keys=(UNIFIED_OVERDUE_PENDING,),
        net_pending_keys=(UNIFIED_NET_PENDING,),
        unaccounted_revenue_fact="unaccounted_revenue",
    ),
    lookup_sheets=(_UNACCOUNTED_LOOKUP_PLATFORM_AGGREGATOR,),
)


PAYMENT_REMINDER_WEEKLY = WorkflowPack(
    workflow_type="PAYMENT_REMINDER_WEEKLY",
    classifier_rule=ClassifierRule(
        workflow_type="PAYMENT_REMINDER_WEEKLY",
        sender_allowlist=("bhawna.gandhi@1mg.com",),
        # Wire subjects: full AR line "Receivable(s) / Overdue(s) as on <date>", the shortened
        # "Receivable(s) as on <date>", or a bare "Receivable(s)" token. The standalone token
        # uses ``(?<![\w-])`` so ``non-receivable`` does not match. ``InboundClassifier`` uses IGNORECASE.
        subject_regex=(
            r"(?:\breceivables?(?:\s*/\s*overdues?\s+|\s+)as\s+on\b|(?<![\w-])receivables?\b)"
        ),
        # Workbook names usually contain receivable(s); optional ``i`` after ``receiv`` covers
        # the common wire typo ``Recevable``. ``.zip`` is unpacked in
        # :func:`pipeline.workbook_attachments.resolve_downloaded_workbook`.
        attachment_regex=RECEIVABLE_ATTACHMENT_REGEX,
        min_attachment_size=1024,
    ),
    variants=(
        EPHARMA_VARIANT,
        CHW_VARIANT,
        DIAGNOSTICS_AGGREGATOR_VARIANT,
        PLATFORM_AGGREGATOR_VARIANT,
    ),
)
