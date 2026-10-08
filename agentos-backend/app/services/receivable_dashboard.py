"""Parse weekly receivables .xlsx and build dashboard JSON (lakh, grids, unbilled).

* **File format** — The workbook is **Microsoft Excel** (``.xlsx``), including files stored on
  **Google Drive** and opened in the browser as the “receivable sheet.” This module uses
  **openpyxl** on the binary workbook. It does **not** call the **Google Sheets** (spreadsheet) API
  for data — only Drive **media** download is used when the file lives on Drive.

* **Receivable tab** — first sheet whose normalized name contains ``receivable``;
  not the Partywise Unbilled tab.
* **KPIs (All)** — **sum of party detail rows (INR) ÷ 100_000** for each header in the
  :func:`_build_kpi_column_allowlist` (control-row figures **or** amount/ageing headers, so a blank
  subtotal cell does not drop a column), **excluding** :func:`is_excluded_payable_ledger_name` rows.
  The control row is not the sole source of allowlist; it is also merged in and duplicate labels
  sum across physical columns. Published ``all`` is not the raw Excel subtotal row alone.
* **KPIs (BU)** — sum party-level detail rows with :func:`normalize_bu_key` (same rules as
  :func:`normalize_bu_unbilled` and Partywise Unbilled: hyphens/space, case). One rollup per
  logical BU. **API** replicates that same dict under **every** raw ``Business Unit`` string
  that normalizes the same, so the dropdown can list all spellings and **Summary / Top 10 /
  collection grid** all use the same row set (not exact-string–only, which would split a BU
  when the sheet has variants).
* **Partywise Unbilled** — tab with ``partywise`` + ``unbilled``; match Segment to each
  party **Business Unit** from the receivable sheet using the same normalization (case/space,
  strip ``-``). A row’s amount is credited to **every** BU in the workbook whose normalized
  name equals the segment (not only the first match), so alternate spellings of the same BU
  all receive the slice. The sheet **total** still adds each unbilled row **once**.
* **Reminder matrix** — Distinct-week counts from ``email_automation_sends`` (same filter as
  before); column labels ``"0-1"``, ``"2"``…``"5"``, ``"6+"`` (``0``/``1`` week → first column).

* **Weighted age (months)** — Top-party / cell line labels use the same weighted average on
  **age-column INR** and the fixed midpoint vector above; if all bucket amounts are zero but
  ``Net Due`` is positive, fall back to the **dominant** bucket midpoint (display only).

* **Collection matrix (Excel-aligned)** — Party rows are **aggregated by (party name,
  business unit)** so multi-row HANA lines match finance ``SUMIF`` / ``SUMPRODUCT``.
  **SAP *Payable* ledger names** (party name ending in ``-Payable`` / ``- payables`` style) are
  **excluded** from all party-based rollups—KPI by BU, Top 10, collection grid, and
  :func:`_sum_kpi_by_bu`—so they do not act as receivable; ``meta`` reports how many were dropped.
  Cell amount = **sum of ageing-column INR** (same as workbook ``O:T`` / receivable age
  columns), **not** ``Net Due``. **Age row** = band from **weighted-average months**
  using fixed midpoints ``0.5, 2, 4.5, 7.5, 10.5, 18`` on those buckets, then
  ``≤1→`` row 0, ``≤3→`` 1, ``≤6→`` 2, ``≤12→`` 3, else row 4 (five rows, labels like
  the executive dashboard). **Reminder column headers** are ``"0-1"``, ``"2"``…``"5"``, ``"6+"`` (six
  slots; last label is display-only vs workbook ``6``) mapping
  ``COUNT(DISTINCT period_key)`` with **0 and 1** sends both in the first column (workbook
  parity). Parties with **zero** bucket sum are omitted from the matrix. **Party lines per cell** —
  all parties that land in a cell, **ordered by that cell’s bucket INR (highest first)**; duplicate
  line strings: if the same display line would appear twice, the second and later rows add
  ``[HANA …]`` so distinct parties are not hidden.
* **Top parties** — Same aggregation; sort by **bucket sum** then ``Net Due``; **₹ Lakh**
  in the API is **overdue ageing buckets only** (same basis as Summary “Total due” and the
  collection grid), **not** full ``Net Due`` when all balance sits in “Not due.”
* **Snapshot meta** — ``reminder_grid_format`` = ``excel_5x6_v1`` (5 ageing × 6 reminder bands).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.db.models import EmailAutomationSend, ReceivableDashboardSnapshot
from app.email_automation.engine._decimal import to_decimal
from app.email_automation.pipeline._shared import jsonable

log = logging.getLogger(__name__)

INR_PER_LAKH = Decimal(100_000)
WORKFLOW_REMINDER = "PAYMENT_REMINDER_WEEKLY"
PARSER_VERSION = "3.14"
# Exposed in snapshot ``meta`` (5 ageing rows × 6 reminder columns ``"0-1"``, ``"2"``…``"5"``, ``"6+"``).
REMINDER_GRID_FORMAT = "excel_5x6_v1"

# Executive-dashboard style row labels (weighted-age bands; not raw receivable headers).
EXCEL_GRID_AGE_LABELS = (
    "0-1 Months",
    "1-3 Months",
    "3-6 Months",
    "6-12 Months",
    "1Y+",
)

# Midpoints for O…T-style buckets (matches ``Dashboard test.xlsx`` ``_Helper`` col I).
EXCEL_BUCKET_MIDPOINTS_MONTHS: tuple[float, ...] = (
    0.5,
    2.0,
    4.5,
    7.5,
    10.5,
    18.0,
)


def normalize_tab_name(name: str) -> str:
    """Lowercase, collapse spaces, strip common separators so ``Party-wise`` ≈ ``Partywise``."""
    s = (name or "").strip().lower()
    for ch in "\u2013\u2014\u2212":
        s = s.replace(ch, "-")
    s = s.replace("-", "")
    return re.sub(r"\s+", "", s)


def normalize_bu_unbilled(name: str) -> str:
    """Match Segment to receivable BU: casefold, strip ``-``, collapse spaces."""
    s = (name or "").strip().lower()
    for ch in "\u2013\u2014":
        s = s.replace(ch, "-")
    s = s.replace("-", "")
    s = re.sub(r"\s+", "", s)
    return s


def normalize_bu_key(name: str) -> str:
    """Logical BU for grouping: **same** rules as :func:`normalize_bu_unbilled` and Partywise Unbilled.

    Hyphens and spaces are removed so *Retail–West* and *Retail West* are one key; keeps
    ``&`` and other alphanumerics (single source of truth with unbilled).
    """
    return normalize_bu_unbilled(name)


# Contra/liability lines from SAP, not customer receivable (see ``* - Payable`` in extract).
_SAP_PAYABLE_NAME_TAIL = re.compile(
    r"(?i)[-–—]\s*payables?\s*$"
)


def is_excluded_payable_ledger_name(name: str) -> bool:
    """True if the party *Name* is a SAP *Payable* sub-ledger / contra line, not a customer row."""
    s = (name or "").strip()
    if not s:
        return False
    if _SAP_PAYABLE_NAME_TAIL.search(s):
        return True
    return False


def _norm_header(s: str) -> str:
    """Single canonical form for header string equality (case, spacing, common unicode)."""
    t = (s or "").replace("\u00a0", " ")
    t = t.strip()
    t = t.strip(" :*\t")
    t = t.lower()
    for ch in "\u2013\u2014\u2212":
        t = t.replace(ch, "-")
    t = re.sub(r"[\s]+", " ", t)
    t = re.sub(
        r"\s+[\(（](in\s*r|inr|₹|rs\.?|amount)[^)]*[\)）]\s*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return t


def is_receivable_tab(sheet_name: str) -> bool:
    n = normalize_tab_name(sheet_name)
    if "receivable" not in n:
        return False
    if "partywise" in n and "unbilled" in n:
        return False
    return True


def is_unbilled_tab(sheet_name: str) -> bool:
    n = normalize_tab_name(sheet_name)
    return "partywise" in n and "unbilled" in n


def is_tds_aging_tab(sheet_name: str) -> bool:
    n = normalize_tab_name(sheet_name)
    return "tds" in n and ("ageing" in n or "aging" in n)


def _cell_decimal(v: Any) -> Decimal | None:
    d = to_decimal(v)
    if d is None:
        return None
    return d


def _f(d: Decimal | None) -> float:
    if d is None:
        return 0.0
    return float(d)


def _find_receivable_sheet(sheetnames: Sequence[str]) -> str | None:
    for s in sheetnames:
        if is_receivable_tab(s):
            return s
    return None


def _find_unbilled_sheet(sheetnames: Sequence[str]) -> str | None:
    for s in sheetnames:
        if is_unbilled_tab(s):
            return s
    return None


def _find_tds_aging_sheet(sheetnames: Sequence[str]) -> str | None:
    for s in sheetnames:
        if is_tds_aging_tab(s):
            return s
    return None


# Receivable sheet — logical columns (many synonyms; order of columns is not assumed).
COL_CODE: tuple[str, ...] = (
    "Code",
    "HANA Code",
    "HANA  Code",
    "Party code",
    "Customer code",
    "SAP code",
    "Corp Code",
    "Co. Code",
    "Acct Code",
    "Account Code",
    "Ledger code",
    "MIS Code",
)
COL_BUSINESS_UNIT: tuple[str, ...] = (
    "Business Unit",
    "BU",
    "B.U.",
    "B U",
    "Bus. Unit",
    "Business  Unit",
    "Segment (BU)",
    "Division",
)
COL_NET_DUE: tuple[str, ...] = (
    "Net Due",
    "Net  Due",
    "Net Dues",
    "Net due",
    "Net  Dues",
    "Total Due",
    "Total due",
    "Outstanding",
    "Due amount",
    "Amount due",
    "O/s Amount",
    "OS Amt",
    "Bal due",
    "Open due",
    "OD Amount",
)
COL_PARTY_NAME: tuple[str, ...] = (
    "Name of the party",
    "Name of the Party",
    "Name-HANA",
    "Customer Name",
    "Party Name",
    "Party name",
    "Name",
    "Customer",
    "M/s",
    "Party / Customer",
    "Party Name (HANA)",
)


def _header_match_keys(candidates: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """Normalized and de-spaced forms for :func:`_match_col`."""
    exact: set[str] = set()
    no_space: set[str] = set()
    for c in candidates:
        n = _norm_header(c)
        if n:
            exact.add(n)
            no_space.add(n.replace(" ", ""))
    return exact, no_space


def _header_matches_any(label: str, candidates: tuple[str, ...]) -> bool:
    want, want_ns = _header_match_keys(candidates)
    if not (label or "").strip():
        return False
    hn = _norm_header(str(label))
    if hn in want or hn.replace(" ", "") in want_ns:
        return True
    return False


def _match_col(
    header_row: list[Any], candidates: tuple[str, ...]
) -> int | None:
    """First column index whose header text matches one of the synonym strings."""
    want, want_ns = _header_match_keys(candidates)
    if not want:
        return None
    for c_idx, h in enumerate(header_row):
        if h is None or str(h).strip() == "":
            continue
        hn = _norm_header(str(h))
        if hn in want:
            return c_idx
        hns = hn.replace(" ", "")
        if hns in want_ns:
            return c_idx
    # Suffixes like ``Name of the party (INR)`` or ``*`` already stripped in ``_norm_header``;
    # long unambiguous substrings: header contains full synonym phrase.
    for c_idx, h in enumerate(header_row):
        if h is None or str(h).strip() == "":
            continue
        hn = _norm_header(str(h))
        for cand in want:
            if len(cand) >= 12 and cand in hn:
                return c_idx
    return None


def _is_dimension_header_for_kpi(lab: str) -> bool:
    """Code / Business Unit / party name — never summed as a KPI amount column."""
    t = (lab or "").strip()
    if not t:
        return True
    return _header_matches_any(t, COL_CODE + COL_BUSINESS_UNIT + COL_PARTY_NAME)


def _header_included_in_kpi_allowlist(
    lab: str,
    *,
    has_control_value: bool,
) -> bool:
    """Whether party-row sums should include this column (control row and/or header semantics)."""
    if not (lab and str(lab).strip()):
        return False
    if _is_dimension_header_for_kpi(lab):
        return False
    if has_control_value:
        return True
    if _is_ageing_header(lab):
        return True
    n = _norm_header(str(lab))
    if n in _KPI_NON_AGE_BALANCE_NORMS:
        return True
    if n.startswith("not due "):
        return True
    if n.startswith("o/s") or n.startswith("os "):
        return True
    return False


# Normalized headers for gross / control-line columns that are not caught by
# :func:`_is_ageing_header` (e.g. **Receivables** is explicitly non-ageing there).
_KPI_NON_AGE_BALANCE_NORMS: frozenset[str] = frozenset(
    {
        "receivables",
        "not due",
        "net due",
        "total due",
        "check",
        "due tds",
        "not due tds",
        "outstanding",
        "o/s",
        "amount due",
        "due amount",
    }
)


def _excel_col_letter_0based(c0: int) -> str:
    n = c0 + 1
    letters = ""
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


def _log_duplicate_receivable_headers(
    row4: list[Any], c_max: int, *, workbook: str
) -> None:
    by_strip: dict[str, list[int]] = defaultdict(list)
    for c in range(min(c_max, len(row4))):
        h = row4[c]
        if h is None or not str(h).strip():
            continue
        by_strip[str(h).strip()].append(c)
    for label, indices in by_strip.items():
        if len(indices) < 2:
            continue
        loc = ", ".join(_excel_col_letter_0based(c) for c in indices)
        log.warning(
            "receivable_dashboard: duplicate header %r in %s columns %s — "
            "KPI values sum across columns; ageing uses the first match per label order.",
            label,
            workbook,
            loc,
        )


def _build_kpi_column_allowlist(
    row4: list[Any],
    c_max: int,
    kpi_from_control: dict[str, float],
) -> set[str]:
    allow: set[str] = set()
    for c in range(min(c_max, len(row4)) if row4 else 0):
        h = row4[c] if c < len(row4) else None
        if h is None or not str(h).strip():
            continue
        key = str(h).strip()
        in_ctrl = key in kpi_from_control
        if _header_included_in_kpi_allowlist(
            key, has_control_value=in_ctrl
        ):
            allow.add(key)
    return allow


_AGE_PATTERNS = re.compile(
    r"(not\s+due|month|yrs|year|>\s*1|1\s*\+|\+1)",
    re.IGNORECASE,
)


def _exclude_age_bucket_from_collection_grid(label: str) -> bool:
    """Excel collection matrix omits the **Not due** row (per finance layout)."""
    t = (label or "").strip().lower()
    if t == "not due":
        return True
    if t.startswith("not due "):
        return True
    return False


def kpi_key_is_due_ageing(name: str) -> bool:
    """Public interface: True when ``name`` is a due-ageing bucket key.

    Use this to sum only overdue columns from ``kpi_lakh`` — excludes Net Due,
    Not Due, Receivables, Check, and TDS columns.
    """
    return _kpi_key_is_due_ageing_for_summary(name)


def _kpi_key_is_due_ageing_for_summary(name: str) -> bool:
    """Same inclusion rules as the dashboard ``isDueAgeingBucketKey`` (Total due line).

    The KPI control row may have values for **Receivables**, **Not Due**, **Net Due**, etc.
    Those keys must not feed party ``age_amounts`` or Top 10 / grid, or ``tb`` exceeds the
    Summary "Total due" (sum of only overdue / ageing columns).
    """
    t = (name or "").strip().lower()
    if not t:
        return False
    if t in ("receivables", "net due", "check"):
        return False
    if t == "not due" or t.startswith("not due "):
        return False
    if re.match(r"^due tds$|^not due tds$", t, re.IGNORECASE):
        return False
    if "month" in t or "yrs" in t or "year" in t:
        return True
    if re.search(r"^\d+\s*[-–]\s*\d+", t):
        return True
    if re.search(r"^\+1|^1y\+|^>\s*1", t, re.IGNORECASE):
        return True
    return False


def _is_ageing_header(label: str) -> bool:
    t = (label or "").strip()
    if not t:
        return False
    if t.lower() in (
        "code",
        "name of the party",
        "business unit",
        "receivables",
        "name",
        "segment",
    ):
        return False
    _dim = COL_CODE + COL_NET_DUE + COL_BUSINESS_UNIT + COL_PARTY_NAME
    if _header_matches_any(t, _dim):
        return False
    return bool(_AGE_PATTERNS.search(t))


def _ageing_bucket_sort_key(
    label: str, col_index: int
) -> tuple[int, int]:
    """Order buckets in **calendar** order (0–1 → 1Y+), not left-to-right on the sheet.

    Midpoints in :data:`EXCEL_BUCKET_MIDPOINTS_MONTHS` line up with the **first** O…T
    position only when the physical sheet order matches; sorting by label decouples that.
    """
    t0 = (label or "").strip()
    t = t0.replace("\u2013", "-").replace("\u2014", "-").lower()
    t = re.sub(r"\s+", " ", t)
    if t == "not due" or t.startswith("not due "):
        return (5, col_index)
    if re.search(r"0[\s-]*1|0\s*to\s*1", t) and "not" not in t[:8]:
        rank = 10
    elif re.search(r"1[\s-]*3|1\s*to\s*3", t):
        rank = 20
    elif re.search(r"3[\s-]*6|3\s*to\s*6", t):
        rank = 30
    elif re.search(r"6[\s-]*9|6\s*to\s*9", t) and "12" not in t:
        rank = 40
    elif re.search(r"6[\s-]*12|6\s*to\s*12", t):
        rank = 45
    elif re.search(r"9[\s-]*12|9\s*to\s*12", t):
        rank = 50
    elif re.search(
        r"1y|1\s*\+|yrs|year|>[\s]*1|over[\s-]*1|1\s*year|\+1|1\+",
        t,
    ):
        rank = 60
    else:
        rank = 80
    return (rank, col_index)


def _age_column_indices(
    header_row: list[Any],
    max_cols: int,
    *,
    kpi_column_allowlist: set[str] | None = None,
) -> list[int]:
    """Ageing data columns, ordered by **label semantics** (not left-to-right sheet order).

    When ``kpi_column_allowlist`` is set, only include headers that appear in the
    receivable **control (KPI) row** (same allowlist as :func:`_sum_kpi_by_bu`). This keeps
    party bucket sums, collection grid, and Top 10 on the same basis as the Summary.

    The workbook can have extra ageing-style headers (or detail without a control figure);
    those are omitted. When no allowlist is given (no KPI row), we keep the legacy rule:
    at most **six** buckets (O:T-style cap).
    """
    collected: list[tuple[tuple[int, int], int]] = []
    for c in range(max_cols):
        h = header_row[c] if c < len(header_row) else None
        if h is None or str(h).strip() == "":
            continue
        lab = str(h).strip()
        if not _is_ageing_header(lab):
            continue
        if _exclude_age_bucket_from_collection_grid(lab):
            continue
        if kpi_column_allowlist is not None and lab not in kpi_column_allowlist:
            continue
        sk = _ageing_bucket_sort_key(lab, c)
        collected.append((sk, c))
    collected.sort()
    cols = [c for _sk, c in collected]
    if kpi_column_allowlist is None:
        return cols[:6]
    return cols


HEADER_SCAN_MAX_ROW = 40


def _kpi_labeled_value_count(
    kpi_values: list[Any], header_values: list[Any], c_max: int
) -> int:
    n = 0
    for c in range(c_max):
        h = header_values[c] if c < len(header_values) else None
        if h is None or str(h).strip() == "":
            continue
        d = _cell_decimal(
            kpi_values[c] if c < len(kpi_values) else None
        )
        if d is not None:
            n += 1
    return n


def _find_kpi_excel_row(
    ws: Any,
    header_excel_row: int,
    header_values: list[Any],
    c_max: int,
) -> int | None:
    """Control-totals row is usually the row above the header; else scan further up."""
    for min_labeled in (2, 1):
        for delta in (1, 2, 3, 4):
            r = header_excel_row - delta
            if r < 1:
                break
            rowv = _read_row(ws, r, c_max)
            if (
                _kpi_labeled_value_count(rowv, header_values, c_max)
                >= min_labeled
            ):
                return r
    return None


def _discover_receivable_header(
    ws: Any, c_max: int
) -> tuple[int, list[Any]] | None:
    """Locate the header row by required columns; on equal score, prefer a later row (closer to data)."""
    best_r: int | None = None
    best_score = -1
    last_row = min(
        HEADER_SCAN_MAX_ROW,
        _sheet_max_row(ws, start_row=1, cap=HEADER_SCAN_MAX_ROW + 1),
    )
    for r in range(1, last_row + 1):
        rowv = _read_row(ws, r, c_max)
        if not any(
            x is not None and str(x).strip() for x in rowv
        ):
            continue
        c_code = _match_col(list(rowv), COL_CODE)
        c_bu = _match_col(list(rowv), COL_BUSINESS_UNIT)
        c_net = _match_col(list(rowv), COL_NET_DUE)
        if c_code is None or c_bu is None or c_net is None:
            continue
        score = 3
        if _match_col(list(rowv), COL_PARTY_NAME) is not None:
            score += 1
        age_n = len(_age_column_indices(list(rowv), c_max))
        if age_n >= 2:
            score += 1
        if score > best_score:
            best_score = score
            best_r = r
        elif (
            score == best_score
            and best_r is not None
            and r > best_r
        ):
            best_r = r
    if best_r is None:
        return None
    return (best_r, _read_row(ws, best_r, c_max))


@dataclass
class _Party:
    excel_row: int
    code: str
    name: str
    bu: str
    net_due: Decimal
    age_amounts: list[Decimal] = field(default_factory=list)
    #: Set on name-aggregated rows: ``max`` distinct reminder weeks across merged codes.
    reminder_distinct_weeks: int | None = None


def _read_row(ws: Any, r: int, c_max: int) -> list[Any]:
    return [ws.cell(row=r, column=c + 1).value for c in range(c_max)]


def _sheet_max_col(ws: Any, *, cap: int = 200) -> int:
    """read_only sheets may report ``max_column``/``max_row`` as None until scanned."""
    m = getattr(ws, "max_column", None)
    if m is None:
        return cap
    return min(int(m), cap)


def _sheet_max_row(ws: Any, *, start_row: int, cap: int = 100_000) -> int:
    m = getattr(ws, "max_row", None)
    if m is None:
        return min(start_row + cap - 1, start_row + 25_000)
    return min(int(m), start_row + cap - 1)


def _iter_party_rows(
    ws: Any,
    *,
    start_row: int,
    c_max: int,
    c_code: int,
    c_bu: int,
    c_net: int,
    c_name: int | None,
    age_idx: list[int],
) -> list[_Party]:
    parties: list[_Party] = []
    empty_run = 0
    r_end = _sheet_max_row(ws, start_row=start_row, cap=100_000) + 1
    for r in range(start_row, r_end):
        code_raw = _read_row(ws, r, c_max)
        if c_code >= len(code_raw):
            break
        code_v = code_raw[c_code]
        if code_v is None or str(code_v).strip() == "":
            empty_run += 1
            if empty_run >= 5:
                break
            continue
        empty_run = 0
        bu_v = code_raw[c_bu] if c_bu < len(code_raw) else None
        net_v = _cell_decimal(
            code_raw[c_net] if c_net < len(code_raw) else None
        )
        name = ""
        if (
            c_name is not None
            and c_name < len(code_raw)
            and code_raw[c_name] is not None
        ):
            name = str(code_raw[c_name]).strip()
        bu_s = str(bu_v or "").strip()
        ag: list[Decimal] = []
        for ci in age_idx:
            d = _cell_decimal(
                code_raw[ci] if ci < len(code_raw) else None
            ) or Decimal(0)
            ag.append(d)
        if net_v is None:
            net_v = Decimal(0)
        code_s = str(code_v).strip()
        parties.append(
            _Party(
                excel_row=r,
                code=code_s,
                name=name,
                bu=bu_s,
                net_due=net_v,
                age_amounts=ag,
            )
        )
    return parties


def _party_total_bucket_inr(p: _Party) -> Decimal:
    """Sum of ageing-column amounts (workbook ``O:T`` / receivable age slice)."""
    t = Decimal(0)
    for a in p.age_amounts or []:
        if a is None:
            continue
        t += a if isinstance(a, Decimal) else Decimal(str(a))
    return t


def _excel_weighted_avg_months(amounts: Sequence[Decimal]) -> float:
    """Same construction as ``_Helper`` column ``D`` in ``Dashboard test.xlsx``."""
    acc = Decimal(0)
    denom = Decimal(0)
    for i, a in enumerate(amounts[: len(EXCEL_BUCKET_MIDPOINTS_MONTHS)]):
        ai = a if isinstance(a, Decimal) else Decimal(str(a))
        if ai == 0:
            continue
        m = Decimal(str(EXCEL_BUCKET_MIDPOINTS_MONTHS[i]))
        acc += ai * m
        denom += ai
    if denom <= 0:
        return 0.0
    return float((acc / denom).quantize(Decimal("0.000001")))


def _excel_matrix_row_from_wtd(wtd_months: float) -> int:
    if wtd_months <= 0:
        return 0
    if wtd_months <= 1:
        return 0
    if wtd_months <= 3:
        return 1
    if wtd_months <= 6:
        return 2
    if wtd_months <= 12:
        return 3
    return 4


def _reminder_col_idx_excel(distinct_week_count: int) -> int:
    """Map DB week count to column index 0…5 (headers ``"0-1"``…``"5"``, ``"6+"``).

    Zero sends share the first column with one send (workbook illustrative default).
    """
    n = int(distinct_week_count)
    if n < 0:
        n = 0
    band = max(1, min(n, 6))
    return band - 1


def _aggregate_parties_for_excel(
    parties: list[_Party],
    send_by_code: Mapping[str, int],
) -> list[_Party]:
    """One row per (party name, BU) with summed ageing and max reminder weeks."""
    groups: dict[tuple[str, str], list[_Party]] = defaultdict(list)
    for p in parties:
        if not p.name or not str(p.name).strip() or not p.bu:
            continue
        key = (str(p.name).strip().lower(), normalize_bu_key(p.bu))
        groups[key].append(p)
    merged: list[_Party] = []
    for (_, _), plist in groups.items():
        bu_disp = plist[0].bu
        name_disp = str(plist[0].name).strip()
        n_age = max((len(x.age_amounts) for x in plist), default=0)
        ag = [Decimal(0)] * n_age
        net_tot = Decimal(0)
        max_send = 0
        for x in plist:
            net_tot += x.net_due if x.net_due is not None else Decimal(0)
            for i, a in enumerate(x.age_amounts):
                if i < n_age:
                    ai = a if isinstance(a, Decimal) else Decimal(str(a or 0))
                    ag[i] += ai
            ks = str(x.code or "").strip()
            max_send = max(max_send, int(send_by_code.get(ks, 0)))
        primary = max(plist, key=lambda z: _party_total_bucket_inr(z))
        merged.append(
            _Party(
                excel_row=-1,
                code=primary.code,
                name=name_disp,
                bu=bu_disp,
                net_due=net_tot,
                age_amounts=ag,
                reminder_distinct_weeks=max_send,
            )
        )
    return merged


def _age_label_midpoint_months(lab: str) -> float:
    """Heuristic center of bucket for weighted months (aligns with Excel-style party list)."""
    t = (lab or "").strip().lower()
    if "0-1" in t or "0–1" in t:
        return 0.5
    if "1-3" in t or "1–3" in t:
        return 2.0
    if "3-6" in t or "3–6" in t:
        return 4.5
    if "6-9" in t or "6–9" in t:
        return 7.5
    if "6-12" in t or "6–12" in t or "6 to 12" in t:
        return 9.0
    if "9-12" in t or "9–12" in t:
        return 10.5
    if "yrs" in t or "year" in t or "1y" in t or "+1" in t or t.startswith(">") or t.endswith("y+"):
        return 18.0
    return 6.0


def _party_wtd_age_months(p: _Party, age_labels: list[str]) -> float:
    """Weighted months: Σ(age INR × bucket midpoint) / Σ(age INR) when the sum is positive.

    Uses **sum of ageing columns** as the denominator (not ``Net Due``) so the
    average matches Excel when bucket totals do not reconcile to net. If all
    bucket amounts are zero but ``net_due > 0``, falls back to the **dominant
    bucket** midpoint (same tie-break as the collection matrix row).
    """
    if not age_labels:
        return 0.0
    net = p.net_due
    if net is None or net <= 0:
        return 0.0
    amounts = p.age_amounts or []
    n = min(len(amounts), len(age_labels))
    if n == 0:
        return 0.0
    labels = age_labels[:n]
    acc = Decimal(0)
    bucket_sum = Decimal(0)
    for i in range(n):
        a = amounts[i] if isinstance(amounts[i], Decimal) else Decimal(amounts[i])
        m = _age_label_midpoint_months(labels[i])
        acc += a * Decimal(str(m))
        bucket_sum += a
    if bucket_sum > 0:
        return float((acc / bucket_sum).quantize(Decimal("0.01")))
    di = min(_age_row_index(p), len(age_labels) - 1)
    return float(
        Decimal(str(_age_label_midpoint_months(age_labels[di]))).quantize(
            Decimal("0.01")
        )
    )


def _party_line_for_collection_cell(
    p: _Party,
    age_labels: list[str],
    *,
    net_pending_override: Decimal | None = None,
    is_stale: bool = False,
) -> str:
    nm = (p.name or "").strip() or p.code
    tb = _party_total_bucket_inr(p)
    # Use email-engine _net_pending when available — it is aggregated by HANA
    # code and uses Net Amount Pending (not raw ageing-bucket sums).
    amount = net_pending_override if net_pending_override is not None else tb
    if amount > 0:
        w = _excel_weighted_avg_months(p.age_amounts) if tb > 0 else _party_wtd_age_months(p, age_labels)
        lakh = _f(amount / INR_PER_LAKH)
    else:
        w = _party_wtd_age_months(p, age_labels)
        lakh = _f(p.net_due / INR_PER_LAKH)
    if is_stale and tb > 0:
        # Bucket sum is stale (significantly exceeds Net Due). Primary format
        # stays standard so the frontend ₹ regex still parses the display amount;
        # net due is appended as a visible annotation.
        net_due_lakh = _f(p.net_due / INR_PER_LAKH)
        return f"{nm} (₹{lakh:.2f} L, {w:.2f}m) [net due ₹{net_due_lakh:.2f} L]"
    return f"{nm} (₹{lakh:.2f} L, {w:.2f}m)"


def _age_row_index(p: _Party) -> int:
    """Which age *row* (0..5) gets this party’s full net: argmax of age-column INR.

    Ties: if two age columns are equal and largest, the **earlier** column (lower
    index) wins, because we use strict ``>`` not ``>=``.
    """
    if not p.age_amounts:
        return 0
    best = 0
    best_amt = p.age_amounts[0]
    for i, a in enumerate(p.age_amounts[1:], start=1):
        if a > best_amt:
            best = i
            best_amt = a
    return min(best, 5)


def _parse_unbilled(
    path: str, sheet: str, bu_set: set[str]
) -> tuple[Decimal, dict[str, Decimal]]:
    wb = load_workbook(path, data_only=True, read_only=False)
    try:
        ws = wb[sheet]
        scan_cols = _sheet_max_col(ws, cap=40)
        mr = getattr(ws, "max_row", None)
        header_last = min(mr, 30) if mr is not None else 30
        seg_col: int | None = None
        amt_col: int | None = None
        data_start = 1
        seg_candidates: tuple[str, ...] = (
            "Segment",
            "Seg.",
            "Seg",
            "SBU",
            "Strategic Business Unit",
            "Business Segment",
            "Business Unit",
            "BU",
        )
        for r in range(1, header_last + 1):
            row = _read_row(ws, r, scan_cols)
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                raw = str(cell)
                t = _norm_header(raw)
                if seg_col is None and _header_matches_any(
                    raw, seg_candidates
                ):
                    seg_col = c
                if amt_col is None and (
                    "unbill" in t
                    or _header_matches_any(
                        raw,
                        (
                            "Unbilled Amt",
                            "Unbilled Amount",
                            "Open (Unbilled)",
                            "Unbilled",
                        ),
                    )
                ):
                    amt_col = c
            if seg_col is not None and amt_col is not None:
                data_start = r + 1
                break
        if seg_col is None or amt_col is None:
            return Decimal(0), {}

        total = Decimal(0)
        by_bu: dict[str, Decimal] = defaultdict(Decimal)
        ub_last = ws.max_row
        if ub_last is None:
            ub_last = data_start + 50_000
        for r in range(data_start, min(ub_last, data_start + 50_000) + 1):
            row = _read_row(ws, r, scan_cols)
            seg = row[seg_col] if seg_col < len(row) else None
            amt = _cell_decimal(row[amt_col] if amt_col < len(row) else None)
            if seg is None or str(seg).strip() == "" or amt is None:
                continue
            total += amt
            nk = normalize_bu_unbilled(str(seg))
            for bu in bu_set:
                if normalize_bu_unbilled(bu) == nk:
                    by_bu[bu] += amt
        return total, {k: v for k, v in by_bu.items()}
    finally:
        wb.close()


def _discover_tds_header(ws: Any, c_max: int) -> tuple[int, list[Any]] | None:
    """Find header row in TDS Ageing sheet — requires Code + BU only (no Net Due column)."""
    last = min(HEADER_SCAN_MAX_ROW, _sheet_max_row(ws, start_row=1, cap=HEADER_SCAN_MAX_ROW + 1))
    for r in range(1, last + 1):
        rowv = _read_row(ws, r, c_max)
        if not any(x is not None and str(x).strip() for x in rowv):
            continue
        if (
            _match_col(list(rowv), COL_CODE) is not None
            and _match_col(list(rowv), COL_BUSINESS_UNIT) is not None
        ):
            return (r, list(rowv))
    return None


def _parse_tds_aging(
    wb: Any,
    sheet_name: str,
    age_labels: list[str],
) -> dict[tuple[str, str], list[Decimal]]:
    """Parse TDS Ageing sheet → per-(code, bu_norm) TDS amounts aligned to receivable age_labels.

    Both sheets use the same bucket header names, but column order may differ.
    Multiple rows with the same (code, BU) are summed — matching the receivable
    sheet's multi-row behaviour for the same party.

    Returns ``{(hana_code, bu_norm): [tds_bucket_0, ..., tds_bucket_n]}``.
    Missing (code, BU) combinations mean zero TDS for that party — treated as such downstream.
    """
    ws = wb[sheet_name]
    c_max = _sheet_max_col(ws, cap=200)
    discovered = _discover_tds_header(ws, c_max)
    if discovered is None:
        log.warning("tds_aging: no header row (Code + BU) found in sheet %r", sheet_name)
        return {}
    header_r, header_row = discovered
    c_code = _match_col(header_row, COL_CODE)
    c_bu = _match_col(header_row, COL_BUSINESS_UNIT)
    if c_code is None or c_bu is None:
        return {}

    # Map normalized receivable age_labels to TDS sheet column indices.
    # Columns are matched by label semantics, not position, so sheet-order differences
    # between the two tabs are handled correctly.
    norm_to_label_idx: dict[str, int] = {_norm_header(lab): i for i, lab in enumerate(age_labels)}
    tds_col_map: list[tuple[int, int]] = []  # (sheet_col_idx, age_labels_idx)
    for c in range(c_max):
        h = header_row[c] if c < len(header_row) else None
        if h is None or not str(h).strip():
            continue
        label_idx = norm_to_label_idx.get(_norm_header(str(h)))
        if label_idx is not None:
            tds_col_map.append((c, label_idx))

    if not tds_col_map:
        log.warning(
            "tds_aging: no age bucket columns matched receivable age_labels in sheet %r",
            sheet_name,
        )
        return {}

    result: dict[tuple[str, str], list[Decimal]] = defaultdict(
        lambda: [Decimal(0)] * len(age_labels)
    )
    data_start = header_r + 1
    r_end = _sheet_max_row(ws, start_row=data_start, cap=100_000) + 1
    empty_run = 0
    for r in range(data_start, r_end):
        row_vals = _read_row(ws, r, c_max)
        code_v = row_vals[c_code] if c_code < len(row_vals) else None
        if code_v is None or str(code_v).strip() == "":
            empty_run += 1
            if empty_run >= 5:
                break
            continue
        empty_run = 0
        code_s = str(code_v).strip()
        bu_v = row_vals[c_bu] if c_bu < len(row_vals) else None
        bu_norm = normalize_bu_key(str(bu_v or "").strip())
        key = (code_s, bu_norm)
        for col_idx, label_idx in tds_col_map:
            v = _cell_decimal(row_vals[col_idx] if col_idx < len(row_vals) else None) or Decimal(0)
            result[key][label_idx] += v

    return dict(result)


def _aggregate_parties_by_code_bu(parties: list[_Party], age_n: int) -> list[_Party]:
    """Collapse receivable rows to one ``_Party`` per (code, normalize_bu_key(bu)).

    Multiple raw rows for the same HANA code + BU (common in SAP extracts when a
    party spans several invoice buckets) are merged by summing ``age_amounts`` and
    ``net_due``.  The representative name / BU string come from the first row for
    that group (highest-bucket row in original parse order).

    ``excel_row`` is set to ``-1`` on merged rows — callers that need per-row Excel
    access (i.e. ``_sum_kpi_by_bu``) should continue to use the unmerged list.
    """
    groups: dict[tuple[str, str], list[_Party]] = defaultdict(list)
    for p in parties:
        key = (str(p.code or "").strip(), normalize_bu_key(str(p.bu or "")))
        groups[key].append(p)
    merged: list[_Party] = []
    for plist in groups.values():
        ag: list[Decimal] = [Decimal(0)] * age_n
        net_tot = Decimal(0)
        for x in plist:
            net_tot += x.net_due if x.net_due is not None else Decimal(0)
            for i, a in enumerate(x.age_amounts[:age_n]):
                ag[i] += a if isinstance(a, Decimal) else Decimal(str(a or 0))
        rep = plist[0]
        merged.append(
            _Party(
                excel_row=-1,
                code=rep.code,
                name=str(rep.name).strip(),
                bu=rep.bu,
                net_due=net_tot,
                age_amounts=ag,
            )
        )
    return merged


def _build_grids(
    parties: list[_Party],
    send_by_code: Mapping[str, int],
    age_labels: list[str],
    net_pending_by_code: Mapping[str, Decimal] | None = None,
    stale_codes: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels_rem = ["0-1", "2", "3", "4", "5", "6+"]
    age_labels_f = list(EXCEL_GRID_AGE_LABELS)

    def one_scope(rows: list[_Party]) -> dict[str, Any]:
        vals: list[list[float]] = [[0.0] * 6 for _ in range(5)]
        merged = _aggregate_parties_for_excel(rows, send_by_code)
        cell_items: dict[
            tuple[int, int], list[tuple[Decimal, str, str]]
        ] = defaultdict(list)
        # Track which HANA codes have already consumed their _net_pending so the
        # same code is not counted twice when it appears under multiple name/BU groups.
        seen_np_codes: set[str] = set()
        for p in merged:
            tb = _party_total_bucket_inr(p)
            if tb <= 0:
                continue
            wtd = _excel_weighted_avg_months(p.age_amounts)
            ai = _excel_matrix_row_from_wtd(wtd)
            cnt = (
                int(p.reminder_distinct_weeks)
                if p.reminder_distinct_weeks is not None
                else int(send_by_code.get(str(p.code or "").strip(), 0))
            )
            ri = _reminder_col_idx_excel(cnt)
            hana = str(p.code or "").strip()
            np_override: Decimal | None = None
            if net_pending_by_code is not None and hana and hana not in seen_np_codes:
                raw_np = net_pending_by_code.get(hana)
                if raw_np is not None:
                    np_override = raw_np
                    seen_np_codes.add(hana)
            amount = np_override if np_override is not None else tb
            cell_party = _Party(
                p.excel_row,
                p.code,
                p.name,
                p.bu,
                p.net_due,
                p.age_amounts,
                p.reminder_distinct_weeks,
            )
            vals[ai][ri] += _f(amount / INR_PER_LAKH)
            line = _party_line_for_collection_cell(
                cell_party,
                age_labels,
                net_pending_override=np_override,
                is_stale=bool(stale_codes and hana in stale_codes),
            )
            if not line or not str(line).strip():
                continue
            cell_items[(ai, ri)].append((amount, str(line).strip(), hana))
        nms: list[list[list[str]]] = [[[] for _ in range(6)] for _ in range(5)]
        for (ai, ri), items in cell_items.items():
            items.sort(key=lambda t: t[0], reverse=True)
            used: set[str] = set()
            for _tb, line, hana in items:
                disp = line
                if disp in used:
                    disp = f"{line} [HANA {hana}]" if hana else f"{line} (dup)"
                i = 2
                while disp in used:
                    disp = (
                        f"{line} [HANA {hana} ·{i}]" if hana else f"{line} ·{i}"
                    )
                    i += 1
                used.add(disp)
                nms[ai][ri].append(disp)
        return {
            "reminder_bands": labels_rem,
            "ageing_buckets": age_labels_f,
            "values_lakh": vals,
            "client_names": nms,
        }

    g_all = one_scope(parties)
    g_bu: dict[str, Any] = {}
    by_norm: dict[str, list[_Party]] = defaultdict(list)
    for p in parties:
        if p.bu:
            by_norm[normalize_bu_key(p.bu)].append(p)
    # One entry per *dropdown* raw string, each sharing the same norm-wide grid (re-use one build).
    raw_bus = sorted({p.bu for p in parties if p.bu}, key=str)
    grid_for_norm: dict[str, Any] = {}
    for u in raw_bus:
        bnk = normalize_bu_key(u)
        if bnk not in grid_for_norm:
            grid_for_norm[bnk] = one_scope(by_norm[bnk])
        g_bu[u] = grid_for_norm[bnk]
    return g_all, g_bu


def _sum_kpi_by_bu(
    parties: list[_Party],
    bu_norm: str,
    row4: list[Any],
    c_max: int,
    ws: Any,
    *,
    kpi_column_allowlist: set[str],
) -> dict[str, float]:
    """Sum **detail** party rows whose :func:`normalize_bu_key`\\ ``(Business Unit)`` is ``bu_norm``."""
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for pr in parties:
        if not pr.bu or normalize_bu_key(pr.bu) != bu_norm:
            continue
        rowvals = _read_row(ws, pr.excel_row, c_max)
        for c in range(c_max):
            h = row4[c] if c < len(row4) else None
            if h is None or str(h).strip() == "":
                continue
            key = str(h).strip()
            if key not in kpi_column_allowlist:
                continue
            d = _cell_decimal(rowvals[c] if c < len(rowvals) else None)
            if d is None:
                d = Decimal(0)
            sums[key] += d
    return {k: _f(v / INR_PER_LAKH) for k, v in sums.items()}


def _normalize_hana_key_for_send_lookup(code: str) -> str:
    return (code or "").strip()


def _merge_send_count_keys(send_counts: Mapping[str, int]) -> dict[str, int]:
    """Collapse duplicate keys that differ only by outer whitespace; take max if duplicated."""
    out: dict[str, int] = {}
    for k, v in send_counts.items():
        ks = _normalize_hana_key_for_send_lookup(str(k))
        if not ks:
            continue
        n = int(v)
        out[ks] = max(out.get(ks, 0), n)
    return out


def _top_parties(
    parties: list[_Party],
    age_labels: list[str],
    send_by_code: Mapping[str, int],
    n: int | None = 100,
) -> list[dict[str, Any]]:
    merged = _aggregate_parties_for_excel(parties, send_by_code)
    merged.sort(
        key=lambda x: (_party_total_bucket_inr(x), x.net_due),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for p in merged[:n]:
        tb = _party_total_bucket_inr(p)
        # Align with Summary “Total due” and collection grid: only ageing-bucket INR.
        # Do not substitute full Net Due when tb==0 (e.g. all in “Not due”), or top
        # rows can exceed portfolio overdue totals.
        amt_lakh = _f(tb / INR_PER_LAKH)
        if tb > 0:
            wtd = float(
                Decimal(str(_excel_weighted_avg_months(p.age_amounts))).quantize(
                    Decimal("0.01")
                )
            )
        else:
            wtd = _party_wtd_age_months(p, age_labels)
        out.append(
            {
                "code": p.code,
                "name": p.name,
                "business_unit": p.bu,
                "net_due_lakh": amt_lakh,
                "wtd_age_months": wtd,
            }
        )
    return out


def build_receivable_payload(
    path: str | Path,
    send_counts: Mapping[str, int],
    *,
    source_message_id: str | None = None,
    reminder_sends_meta: Mapping[str, Any] | None = None,
    net_pending_by_code: Mapping[str, Decimal] | None = None,
) -> dict[str, Any] | None:
    p = str(path)
    # Not read_only: large receivables files need real dimensions; read_only often
    # leaves max_row/max_column None and forces pathological full-sheet scans.
    wb = load_workbook(p, data_only=True, read_only=False)
    try:
        rname = _find_receivable_sheet(wb.sheetnames)
        if rname is None:
            return None
        ubn = _find_unbilled_sheet(wb.sheetnames)
        ws = wb[rname]
        c_max = _sheet_max_col(ws, cap=200)
        discovered = _discover_receivable_header(ws, c_max)
        if discovered is None:
            log.warning(
                "receivable sheet: no header row (Code + BU + Net Due) in %r", p
            )
            return None
        header_excel_row, row4 = discovered
        kpi_from_control: dict[str, float] = {}
        kpi_excel_row = _find_kpi_excel_row(
            ws, header_excel_row, list(row4), c_max
        )
        if kpi_excel_row is not None:
            kpi_vals = _read_row(ws, kpi_excel_row, c_max)
            for c in range(c_max):
                h = row4[c] if c < len(row4) else None
                if h is None or str(h).strip() == "":
                    continue
                d = _cell_decimal(
                    kpi_vals[c] if c < len(kpi_vals) else None
                )
                if d is None:
                    continue
                key = str(h).strip()
                v_lakh = _f(d / INR_PER_LAKH)
                kpi_from_control[key] = (
                    kpi_from_control.get(key, 0.0) + v_lakh
                )
        c_code = _match_col(list(row4), COL_CODE)
        c_bu = _match_col(list(row4), COL_BUSINESS_UNIT)
        c_net = _match_col(list(row4), COL_NET_DUE)
        c_name = _match_col(list(row4), COL_PARTY_NAME)
        if c_code is None or c_bu is None or c_net is None:
            log.warning("receivable sheet missing Code/BU/Net column in %r", p)
            return None
        _log_duplicate_receivable_headers(
            list(row4), c_max, workbook=Path(p).name
        )
        kpi_column_allowlist = _build_kpi_column_allowlist(
            list(row4), c_max, kpi_from_control
        )
        if not kpi_column_allowlist:
            log.warning(
                "receivable_dashboard: no KPI columns in %r — using empty allowlist; "
                "downstream may be empty.",
                Path(p).name,
            )
        kpi_due_ageing_keys = {
            k
            for k in kpi_column_allowlist
            if _kpi_key_is_due_ageing_for_summary(k)
        }
        kpi_for_age: set[str] | None
        if kpi_due_ageing_keys:
            kpi_for_age = kpi_due_ageing_keys
        else:
            kpi_for_age = None
        age_idx = _age_column_indices(
            list(row4), c_max, kpi_column_allowlist=kpi_for_age
        )
        if not age_idx:
            age_idx = [c_net]
        age_pairs = [(i, str(row4[i]).strip()) for i in age_idx]
        filtered_age = [
            (i, lab)
            for i, lab in age_pairs
            if not _exclude_age_bucket_from_collection_grid(lab)
        ]
        if filtered_age:
            age_idx = [i for i, _ in filtered_age]
            age_labels = [lab for _, lab in filtered_age]
        else:
            age_labels = [lab for _, lab in age_pairs]
        data_start_excel_row = header_excel_row + 1
        raw_parties = _iter_party_rows(
            ws,
            start_row=data_start_excel_row,
            c_max=c_max,
            c_code=c_code,
            c_bu=c_bu,
            c_net=c_net,
            c_name=c_name,
            age_idx=age_idx,
        )
        n_excl_pay = sum(
            1
            for p in raw_parties
            if is_excluded_payable_ledger_name(p.name)
        )
        parties = [
            p
            for p in raw_parties
            if not is_excluded_payable_ledger_name(p.name)
        ]
        bu_set_pre: set[str] = {x.bu for x in parties if x.bu}
        norms_seen: set[str] = {
            normalize_bu_key(u) for u in bu_set_pre
        }
        # KPI computation uses raw party rows (with excel_row) so _sum_kpi_by_bu can
        # re-read the sheet for non-age columns (Net Due, Not Due, etc.).
        kpi_by_norm: dict[str, dict[str, float]] = {}
        for bnk in sorted(norms_seen):
            kpi_by_norm[bnk] = _sum_kpi_by_bu(
                parties,
                bnk,
                row4,
                c_max,
                ws,
                kpi_column_allowlist=kpi_column_allowlist,
            )
        kpi_bu: dict[str, dict[str, float]] = {
            u: kpi_by_norm[normalize_bu_key(u)] for u in sorted(bu_set_pre)
        }
        kpi_lakh_all: dict[str, float] = {}
        for km in kpi_by_norm.values():
            for key, v in km.items():
                kpi_lakh_all[key] = kpi_lakh_all.get(key, 0.0) + v
        send_by_code = _merge_send_count_keys(send_counts)

        # ── TDS subtraction ──────────────────────────────────────────────────
        # Collapse raw rows to one _Party per (code, BU norm) so TDS is applied
        # at the correct aggregate level — both sheets may have multiple rows per
        # HANA code and the net amount is: sum(rec_buckets) − sum(tds_buckets).
        agg_parties = _aggregate_parties_by_code_bu(parties, len(age_idx))

        tds_sheet_name = _find_tds_aging_sheet(wb.sheetnames)
        tds_by_code_bu: dict[tuple[str, str], list[Decimal]] = {}
        tds_clip_parties: list[dict[str, Any]] = []

        if tds_sheet_name:
            tds_by_code_bu = _parse_tds_aging(wb, tds_sheet_name, age_labels)
            for _ap in agg_parties:
                _key = (str(_ap.code or "").strip(), normalize_bu_key(str(_ap.bu or "")))
                _tds = tds_by_code_bu.get(_key)
                if not _tds:
                    continue
                for _i in range(len(_ap.age_amounts)):
                    _tds_i = _tds[_i] if _i < len(_tds) else Decimal(0)
                    if _tds_i == 0:
                        continue
                    _rec_i = _ap.age_amounts[_i]
                    if _tds_i > 0 and _rec_i > 0 and _tds_i > _rec_i:
                        tds_clip_parties.append({
                            "code": str(_ap.code or "").strip(),
                            "name": _ap.name,
                            "bu": _ap.bu,
                            "bucket": age_labels[_i] if _i < len(age_labels) else f"bucket_{_i}",
                            "rec_lakh": _f(_rec_i / INR_PER_LAKH),
                            "tds_lakh": _f(_tds_i / INR_PER_LAKH),
                        })
                    _ap.age_amounts[_i] -= _tds_i
                    if _ap.age_amounts[_i] < 0:
                        _ap.age_amounts[_i] = Decimal(0)
            # Apply the same TDS correction to the KPI bucket columns.
            # _sum_kpi_by_bu read raw Excel cells (TDS-inclusive); subtract the
            # aggregate TDS per BU norm so the KPI cards stay consistent with the grid.
            tds_corr_by_norm: dict[str, list[Decimal]] = defaultdict(
                lambda: [Decimal(0)] * len(age_labels)
            )
            for (_c, _bn), _tds_amts in tds_by_code_bu.items():
                for _i, _t in enumerate(_tds_amts[: len(age_labels)]):
                    tds_corr_by_norm[_bn][_i] += _t
            for _bnk, _km in kpi_by_norm.items():
                _corr = tds_corr_by_norm.get(_bnk, [])
                for _i, _lab in enumerate(age_labels):
                    if _lab in _km and _i < len(_corr):
                        _km[_lab] = _km[_lab] - _f(_corr[_i] / INR_PER_LAKH)
            # Recompute all-scope KPI totals from the corrected per-norm dicts.
            kpi_lakh_all = {}
            for _km in kpi_by_norm.values():
                for _k, _v in _km.items():
                    kpi_lakh_all[_k] = kpi_lakh_all.get(_k, 0.0) + _v
        else:
            log.warning(
                "receivable_dashboard: TDS Ageing sheet not found in %r — "
                "bucket amounts include TDS",
                Path(p).name,
            )
        # ── end TDS subtraction ───────────────────────────────────────────────

        # Stale detection runs on TDS-adjusted agg_parties.  After TDS subtraction
        # bucket_sum ≈ net_due for healthy parties; genuine SAP staleness (payments
        # posted but AR extract not yet refreshed) still shows as a gap here.
        stale_codes: set[str] = set()
        stale_party_meta: list[dict[str, Any]] = []
        for _ap in agg_parties:
            _code = str(_ap.code or "").strip()
            if not _code:
                continue
            _tb = _party_total_bucket_inr(_ap)
            _nd = _ap.net_due if _ap.net_due is not None else Decimal(0)
            _gap = _tb - _nd
            if (
                _tb > 0
                and _nd >= 0
                and _gap > INR_PER_LAKH
                and (_nd == 0 or _gap > _nd * Decimal("0.1"))
            ):
                stale_codes.add(_code)
                stale_party_meta.append({
                    "code": _code,
                    "name": _ap.name,
                    "bu": _ap.bu,
                    "bucket_sum_lakh": _f(_tb / INR_PER_LAKH),
                    "net_due_lakh": _f(_nd / INR_PER_LAKH),
                    "gap_lakh": _f(_gap / INR_PER_LAKH),
                })
        stale_party_meta.sort(key=lambda x: x["gap_lakh"], reverse=True)

        grid_all, grid_by_bu = _build_grids(
            agg_parties, send_by_code, age_labels, net_pending_by_code, stale_codes
        )
        top_all = _top_parties(agg_parties, age_labels, send_by_code, 100)
        all_parties_all = _top_parties(agg_parties, age_labels, send_by_code, None)
        by_norm_parties: dict[str, list[_Party]] = defaultdict(list)
        for _pr in agg_parties:
            if _pr.bu:
                by_norm_parties[normalize_bu_key(_pr.bu)].append(_pr)
        top_bu: dict[str, list[dict[str, Any]]] = {}
        all_parties_bu: dict[str, list[dict[str, Any]]] = {}
        top_by_norm: dict[str, list[dict[str, Any]]] = {}
        all_by_norm: dict[str, list[dict[str, Any]]] = {}
        for u in sorted(bu_set_pre):
            bnk = normalize_bu_key(u)
            if bnk not in top_by_norm:
                top_by_norm[bnk] = _top_parties(
                    by_norm_parties[bnk],
                    age_labels,
                    send_by_code,
                    100,
                )
                all_by_norm[bnk] = _top_parties(
                    by_norm_parties[bnk],
                    age_labels,
                    send_by_code,
                    None,
                )
            top_bu[u] = top_by_norm[bnk]
            all_parties_bu[u] = all_by_norm[bnk]
    finally:
        wb.close()

    bu_set = {x.bu for x in agg_parties if x.bu}
    unbilled_total = Decimal(0)
    unbilled_by: dict[str, float] = {}
    if ubn and bu_set:
        ut, ub = _parse_unbilled(p, ubn, bu_set)
        unbilled_total = ut
        unbilled_by = {k: _f(v / INR_PER_LAKH) for k, v in ub.items()}

    business_units = ["All", *sorted(bu_set)]
    meta0: dict[str, Any] = {
        "parser_version": PARSER_VERSION,
        "reminder_grid_format": REMINDER_GRID_FORMAT,
        "workbook": Path(p).name,
        "receivable_sheet": rname,
        "unbilled_sheet": ubn,
        "source_message_id": source_message_id,
        "excluded_payable_ledger_rows": n_excl_pay,
        "receivable_layout": {
            "header_excel_row": header_excel_row,
            "kpi_excel_row": kpi_excel_row,
            "data_start_excel_row": data_start_excel_row,
            # Sheet column titles used for party ageing / grid / Top 10 (due-ageing only).
            "age_column_labels": list(age_labels),
            "tds_sheet": tds_sheet_name,
        },
    }
    if reminder_sends_meta:
        meta0["reminder_sends"] = jsonable(dict(reminder_sends_meta))
    meta0["data_quality"] = {
        "stale_bucket_count": len(stale_party_meta),
        "stale_bucket_parties": stale_party_meta,
        "tds_clip_count": len(tds_clip_parties),
        "tds_clip_parties": tds_clip_parties,
    }

    by_nk_check: dict[str, list[_Party]] = defaultdict(list)
    for x in parties:
        if x.bu:
            by_nk_check[normalize_bu_key(x.bu)].append(x)
    _seen_nk: set[str] = set()
    for u in sorted(bu_set):
        bnk = normalize_bu_key(u)
        if bnk in _seen_nk:
            continue
        _seen_nk.add(bnk)
        km = kpi_bu.get(u) or {}
        k_due = sum(
            v
            for k, v in km.items()
            if _kpi_key_is_due_ageing_for_summary(k)
        )
        sum_tb = float(
            sum(_party_total_bucket_inr(x) for x in by_nk_check[bnk])
            / INR_PER_LAKH
        )
        if abs(k_due - sum_tb) > max(
            0.2, 1e-3 * max(abs(k_due), abs(sum_tb), 1.0)
        ):
            log.warning(
                "receivable_dashboard: BU norm %r — KPI due-ageing (%.4f L) != "
                "party bucket sum (%.4f L); subtotal/merged columns?",
                bnk,
                k_due,
                sum_tb,
            )

    return jsonable(
        {
            "meta": meta0,
            "business_units": business_units,
            "kpi_lakh": {
                "all": kpi_lakh_all,
                "by_business_unit": kpi_bu,
            },
            "unbilled_lakh": {
                "all": _f(unbilled_total / INR_PER_LAKH),
                "by_business_unit": unbilled_by,
            },
            "top_parties": {"all": top_all, "by_business_unit": top_bu},
            "all_parties": {"all": all_parties_all, "by_business_unit": all_parties_bu},
            "collection_grids": {
                "all": grid_all,
                "by_business_unit": grid_by_bu,
            },
        }
    )


REMINDER_DASHBOARD_STATUSES = ("approved", "rendered", "sent")


def _reminder_sends_for_dashboard_filter():
    return and_(
        EmailAutomationSend.status.in_(REMINDER_DASHBOARD_STATUSES),
        EmailAutomationSend.workflow_type == WORKFLOW_REMINDER,
        EmailAutomationSend.test_mode.is_(False),
    )


async def load_reminder_send_rollup_for_receivable_dashboard(
    db: AsyncSession,
) -> tuple[dict[str, int], dict[str, Any]]:
    """``COUNT(DISTINCT period_key)`` per ``business_key`` for reminder rows.

    Includes ``approved``, ``rendered``, and ``sent`` so rollups match plans
    created in the same ingest tick before dispatch updates status to ``sent``.
    """
    flt = _reminder_sends_for_dashboard_filter()
    stmt = (
        select(
            EmailAutomationSend.business_key,
            func.count(func.distinct(EmailAutomationSend.period_key)),
        )
        .where(flt)
        .group_by(EmailAutomationSend.business_key)
    )
    rows = (await db.execute(stmt)).all()
    counts: dict[str, int] = {}
    for r in rows:
        if r[0] is None:
            continue
        ks = _normalize_hana_key_for_send_lookup(str(r[0]))
        if not ks:
            continue
        counts[ks] = int(r[1])
    stats_stmt = select(
        func.count().label("n_rows"),
        func.count(func.distinct(EmailAutomationSend.business_key)).label("n_bk"),
        func.count(func.distinct(EmailAutomationSend.period_key)).label("n_periods"),
    ).where(flt)
    st = (await db.execute(stats_stmt)).one()
    meta = {
        "table": "email_automation_sends",
        "filters": {
            "status_in": list(REMINDER_DASHBOARD_STATUSES),
            "workflow_type": WORKFLOW_REMINDER,
            "test_mode": False,
        },
        "aggregation": "per business_key: count(distinct period_key)",
        "row_count": int(st.n_rows),
        "distinct_business_keys": int(st.n_bk),
        "distinct_period_keys": int(st.n_periods),
    }
    return counts, meta


async def load_reminder_send_counts_by_business_key(
    db: AsyncSession,
) -> dict[str, int]:
    """Back-compat: only the per-code map. Prefer :func:`load_reminder_send_rollup_for_receivable_dashboard` when you need DB stats in ``meta``."""
    counts, _ = await load_reminder_send_rollup_for_receivable_dashboard(db)
    return counts


async def load_net_pending_by_code(db: AsyncSession) -> dict[str, Decimal]:
    """Latest overdue-pending INR per HANA from reminder sends (collection-grid enrichment).

    Returns a dict keyed by stripped HANA code → Decimal amount in **INR** (not lakh).
    Only the most-recent send per code is used.

    Uses ``_overdue_pending`` when > 0 (ePharma Jun-2026+). Falls back to
    ``_net_pending`` only on legacy sends that predate ``_overdue_pending``.
    Zero or missing values mean *no override* — the dashboard keeps Excel bucket
    sums (important for Not Due–only heads-up emails where total pending > 0
    but overdue is 0).
    """
    flt = _reminder_sends_for_dashboard_filter()
    # Subquery: latest sent_at per business_key so we pick the most recent aggregation.
    sub = (
        select(
            EmailAutomationSend.business_key,
            func.max(EmailAutomationSend.sent_at).label("max_sent"),
        )
        .where(flt)
        .group_by(EmailAutomationSend.business_key)
        .subquery()
    )
    stmt = (
        select(EmailAutomationSend)
        .options(
            load_only(
                EmailAutomationSend.business_key,
                EmailAutomationSend.aggregated_data,
            )
        )
        .join(
            sub,
            and_(
                EmailAutomationSend.business_key == sub.c.business_key,
                EmailAutomationSend.sent_at == sub.c.max_sent,
            ),
        )
        .where(flt)
    )
    rows = (await db.execute(stmt)).scalars().all()
    result: dict[str, Decimal] = {}
    for row in rows:
        code = _normalize_hana_key_for_send_lookup(str(row.business_key or ""))
        if not code:
            continue
        totals = (row.aggregated_data or {}).get("totals") or {}
        overdue_str = totals.get("_overdue_pending")
        net_str = totals.get("_net_pending")
        dec: Decimal | None = None
        if overdue_str is not None:
            try:
                d = Decimal(str(overdue_str))
                if d > 0:
                    dec = d
            except Exception:
                pass
        elif net_str is not None:
            # Legacy sends before ``_overdue_pending`` was snapshotted.
            try:
                d = Decimal(str(net_str))
                if d > 0:
                    dec = d
            except Exception:
                pass
        if dec is not None:
            result[code] = dec
    return result


async def try_ingest_receivable_snapshot(
    db: AsyncSession,
    path: Path,
    *,
    source_message_id: uuid.UUID | None,
) -> None:
    from app.email_automation.pipeline.workbook_attachments import (
        RECEIVABLE_WORKBOOK_INGEST_RE,
        normalize_receivable_workbook,
    )

    if not RECEIVABLE_WORKBOOK_INGEST_RE.search(path.name):
        return
    try:
        path = normalize_receivable_workbook(path)
    except Exception as e:
        log.warning("receivable snapshot: could not normalize workbook %r: %s", path, e)
        return
    try:
        send_counts, reminder_meta = await load_reminder_send_rollup_for_receivable_dashboard(
            db
        )
    except Exception as e:
        log.exception("receivable snapshot: could not load send counts: %s", e)
        return
    try:
        net_pending = await load_net_pending_by_code(db)
    except Exception as e:
        log.exception("receivable snapshot: could not load net_pending: %s", e)
        net_pending = None
    smid: str | None
    if source_message_id is not None:
        smid = str(source_message_id)
    else:
        smid = None
    try:
        payload = await asyncio.to_thread(
            lambda: build_receivable_payload(
                path,
                send_counts,
                source_message_id=smid,
                reminder_sends_meta=reminder_meta,
                net_pending_by_code=net_pending,
            )
        )
    except Exception as e:
        log.exception("receivable payload build failed: %s", e)
        return
    if payload is None:
        log.warning(
            "receivable_dashboard_ingest_skip reason=no_parseable_payload path=%r "
            "source_message_id=%s",
            path.name,
            source_message_id,
        )
        return
    ingest_row = ReceivableDashboardSnapshot(
        source_message_id=source_message_id, payload=payload
    )
    db.add(ingest_row)
    await db.flush()
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    bu_n = 0
    if isinstance(payload, dict):
        bus = payload.get("business_units")
        if isinstance(bus, list):
            bu_n = len(bus)
    log.info(
        "receivable_dashboard_ingest_ok snapshot_id=%s parser_version=%s "
        "reminder_grid_format=%s workbook=%s receivable_sheet=%s business_unit_options=%s",
        ingest_row.id,
        meta.get("parser_version") if isinstance(meta, dict) else None,
        meta.get("reminder_grid_format") if isinstance(meta, dict) else None,
        meta.get("workbook") if isinstance(meta, dict) else None,
        meta.get("receivable_sheet") if isinstance(meta, dict) else None,
        bu_n,
    )
