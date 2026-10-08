"""Excel reader — header-row detection, NBSP normalization, duplicate-header aware.

The upstream spreadsheets have three production quirks we handle explicitly:

1. **Variable header row.** Some sheets have title rows above the header. We score
   the first ``max_header_scan`` rows against a pack-supplied hint set and pick the
   highest-scoring row (ties: earliest wins).
2. **Non-breaking spaces & stray whitespace** in header cells — we fold ``\u00a0``,
   collapse runs, strip, and casefold for matching while preserving the original
   header for display.
3. **Duplicate headers** (``Remarks`` appears twice on ``T(labs)``). We index them
   ``{"{norm}#{occurrence}"}`` so DSL references such as
   ``{"name": "Remarks", "occurrence": 1}`` land on the right column.

No LLM, no pandas — ``openpyxl`` in read-only mode.

Sheet tab names are resolved with **case + whitespace-insensitive** matching
(see :func:`_resolve_sheet_name`) so finance renames like
``Invoice details-H(All) & T(PSP)`` still match the pack's canonical
``Invoice details-H(all)&T(Psp)``. After strict fingerprinting, a **loose**
fingerprint drops brackets and **hyphens**, then folds whitespace, so labels
like ``… H All …`` and ``…-H(All) …`` align. When several tabs collide on the
same loose key, the tab whose **strict** fingerprint is closest to the requested
name wins (longest common prefix, then length distance).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook


_HEADER_NORMALIZER_RE = re.compile(r"\s+")
# Remove every Unicode whitespace run for sheet-tab fingerprinting.
_SHEET_WS_RE = re.compile(r"\s+", re.UNICODE)
# Finance tabs often use en dash / minus sign instead of ASCII hyphen; normalize
# before fingerprinting so ``Invoice details-H…`` matches ``Invoice details–H…``.
_SHEET_DASH_CHARS = (
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2015",  # horizontal bar
    "\u2212",  # minus sign
    "\uFE58",  # small em dash
    "\uFE63",  # small hyphen-minus
    "\uFF0D",  # fullwidth hyphen-minus
)
# Removed after dash/spacing normalization so ``H(all)`` and ``H All`` fingerprint alike.
_SHEET_LOOSE_DROP = frozenset("()[]{}（）［］｛｝")


def _normalize_sheet_punctuation_for_match(name: str) -> str:
    """Fold punctuation that Excel/users swap but keep distinct Unicode codepoints."""

    t = str(name).replace("\u00a0", " ")
    for ch in _SHEET_DASH_CHARS:
        t = t.replace(ch, "-")
    t = t.replace("\uff06", "&").replace("\uff08", "(").replace("\uff09", ")")
    return t


def _norm(s: object) -> str:
    if s is None:
        return ""
    t = str(s).replace("\u00a0", " ").strip()
    t = _HEADER_NORMALIZER_RE.sub(" ", t)
    return t.casefold()


@dataclass(frozen=True, slots=True)
class SheetData:
    """A parsed Excel sheet."""

    sheet_name: str
    # Original headers in display order, after NBSP/whitespace normalization but
    # preserving case — use these in email templates.
    display_headers: tuple[str, ...]
    # Canonical keys aligned with ``display_headers``: ``"{norm_name}#{occurrence}"``.
    canonical_keys: tuple[str, ...]
    # Each row as {canonical_key -> raw cell value} (may be None / "" / numbers / etc.).
    rows: tuple[dict[str, object], ...]
    header_row_index: int  # 0-based row index in the source sheet


@dataclass(frozen=True, slots=True)
class HeaderDetectionConfig:
    """Per-sheet header hints used to locate the header row."""

    # Lowercased header substrings. A row scores +2 per cell whose normalized value
    # is an *exact* hint match and +1 per short alphabetic cell — so a title row of
    # numbers scores 0.
    hints: frozenset[str]
    max_header_scan: int = 10
    short_len_max: int = 60


def _row_score(cells: list[object], cfg: HeaderDetectionConfig) -> int:
    score = 0
    for c in cells:
        t = _norm(c)
        if not t:
            continue
        if t in cfg.hints:
            score += 2
        elif 2 <= len(t) <= cfg.short_len_max and any(ch.isalpha() for ch in t):
            score += 1
    return score


def detect_header_row(
    rows: list[list[object]], cfg: HeaderDetectionConfig
) -> int:
    """Return the best header-row index within the first ``cfg.max_header_scan`` rows."""

    best_score = -1
    best_idx = 0
    scan_limit = min(cfg.max_header_scan, len(rows))
    for i in range(scan_limit):
        score = _row_score(rows[i], cfg)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _make_canonical_keys(display_headers: Iterable[str]) -> tuple[str, ...]:
    """Suffix duplicates with ``#{occurrence}`` so duplicates are addressable."""

    seen: dict[str, int] = {}
    out: list[str] = []
    for h in display_headers:
        norm = _norm(h)
        occ = seen.get(norm, 0)
        seen[norm] = occ + 1
        out.append(f"{norm}#{occ}")
    return tuple(out)


def _sheet_norm_key(name: str) -> str:
    """Fingerprint for tab matching: casefold + strip *all* whitespace (incl. NBSP)."""

    t = _normalize_sheet_punctuation_for_match(name)
    t = _SHEET_WS_RE.sub("", t)
    return t.casefold()


def _sheet_loose_norm_key(name: str) -> str:
    """Aggressive fingerprint: brackets removed, hyphens removed, then ws-stripped."""

    t = _normalize_sheet_punctuation_for_match(name)
    t = "".join(ch for ch in t if ch not in _SHEET_LOOSE_DROP)
    t = t.replace("-", "")
    t = _SHEET_WS_RE.sub("", t)
    return t.casefold()


def _best_sheet_name_match(requested: str, candidates: list[str]) -> str:
    """Pick one physical tab when several share the same loose fingerprint."""

    if not candidates:
        raise ValueError("candidates must be non-empty")
    if len(candidates) == 1:
        return candidates[0]
    req_s = _sheet_norm_key(requested)
    best: str | None = None
    best_lcp = -1
    best_len_diff = 10**9
    for n in sorted(candidates):
        sn = _sheet_norm_key(n)
        lcp = 0
        for i in range(min(len(req_s), len(sn))):
            if req_s[i] != sn[i]:
                break
            lcp += 1
        ld = abs(len(sn) - len(req_s))
        if lcp > best_lcp or (lcp == best_lcp and ld < best_len_diff):
            best_lcp = lcp
            best_len_diff = ld
            best = n
    assert best is not None
    return best


def _sheet_lookup_keys(requested: str) -> tuple[str, ...]:
    """Keys to try against the workbook map, longest-first.

    AR sometimes drops the ``-H&T`` suffix on ``Party wise Ageing-H&T``; when the
    configured name still carries it, also try the stem so both tabs resolve.
    """

    base = _sheet_norm_key(requested)
    keys = [base]
    stem = re.sub(r"[-]?h&t\Z", "", base)
    if stem and stem != base:
        keys.append(stem)
    return tuple(dict.fromkeys(keys))


def _sheet_loose_lookup_keys(requested: str) -> tuple[str, ...]:
    """Same stem fallback as :func:`_sheet_lookup_keys`, using loose fingerprints."""

    base = _sheet_loose_norm_key(requested)
    keys = [base]
    stem = re.sub(r"[-]?h&t\Z", "", base)
    if stem and stem != base:
        keys.append(stem)
    return tuple(dict.fromkeys(keys))


def _resolve_truncated_tab_name(wb, sheet_name: str) -> str | None:
    """Match when Excel cut the tab to 31 characters so the norm key is a prefix.

    Example: configured ``Invoice details-H(all)&T(Psp)`` vs physical
    ``Invoice details-H(All) & T(P`` (31 chars).
    """

    req_strict = _sheet_lookup_keys(sheet_name)
    req_loose = _sheet_loose_lookup_keys(sheet_name)
    hits: list[str] = []
    for n in wb.sheetnames:
        if len(n) != 31:
            continue
        sk_s = _sheet_norm_key(n)
        matched = False
        for rk in req_strict:
            if len(sk_s) < len(rk) and rk.startswith(sk_s):
                hits.append(n)
                matched = True
                break
        if matched:
            continue
        sk_l = _sheet_loose_norm_key(n)
        for rk in req_loose:
            if len(sk_l) < len(rk) and rk.startswith(sk_l):
                hits.append(n)
                break
    seen: set[str] = set()
    deduped: list[str] = []
    for x in hits:
        if x not in seen:
            seen.add(x)
            deduped.append(x)
    if not deduped:
        return None
    if len(deduped) == 1:
        return deduped[0]
    return _best_sheet_name_match(sheet_name, deduped)


def _resolve_sheet_name(wb, sheet_name: str) -> str:
    """Return the workbook's actual tab name for a pack-configured ``sheet_name``.

    Resolution order:

    1. Exact string match against ``wb.sheetnames``.
    2. Casefold + **strip-ends-only** (legacy — cheap path for trivial drift).
    3. **Casefold + remove-all-whitespace** fingerprint (handles ``&`` spacing,
       ``(All)`` vs ``(all)``, etc.).
    4. Same as (3) but also try without a trailing ``-H&T`` / ``H&T`` on the
       *requested* name so ``Party wise Ageing-H&T`` matches ``Party wise Ageing``.
    5. **Loose fingerprint** — strip brackets/braces (ASCII + fullwidth), strip ASCII
       hyphens (unicode dashes were folded to hyphen earlier), then same whitespace
       fold as (3). When several tabs share the same loose key, pick the one whose
       **strict** fingerprint is closest to the requested name
       (:func:`_best_sheet_name_match`).
    6. **Truncated tab** — Excel limits sheet names to 31 characters; if a tab is
       exactly 31 chars and its strict **or** loose fingerprint is a **prefix** of
       the requested fingerprint, treat it as a match (then best-match if several).
    """

    if sheet_name in wb.sheetnames:
        return sheet_name
    lower_strip = {n.casefold().strip(): n for n in wb.sheetnames}
    real = lower_strip.get(sheet_name.casefold().strip())
    if real:
        return real

    by_norm: dict[str, str] = {}
    for n in wb.sheetnames:
        k = _sheet_norm_key(n)
        by_norm.setdefault(k, n)

    for variant in _sheet_lookup_keys(sheet_name):
        hit = by_norm.get(variant)
        if hit:
            return hit

    loose_groups: dict[str, list[str]] = {}
    for n in wb.sheetnames:
        lk = _sheet_loose_norm_key(n)
        loose_groups.setdefault(lk, []).append(n)

    for variant in _sheet_loose_lookup_keys(sheet_name):
        cands = loose_groups.get(variant)
        if cands:
            return _best_sheet_name_match(sheet_name, cands)

    trunc = _resolve_truncated_tab_name(wb, sheet_name)
    if trunc:
        return trunc

    merged_inv = _resolve_merged_invoice_details_ht_tab(wb, sheet_name)
    if merged_inv:
        return merged_inv

    recv = _resolve_receivable_as_on_tab(wb, sheet_name)
    if recv:
        return recv

    raise KeyError(
        f"Sheet {sheet_name!r} not found. Available: {wb.sheetnames!r}"
    )


# Jun-2026 receivables workbook: finance merged ``Invoice details-H(all)&T(Psp)``
# and ``Invoice details-T(labs)`` into a single ``Invoice details-H&T`` tab.
_INVOICE_DETAILS_HT_LOOSE = _sheet_loose_norm_key("Invoice details-H&T")
_LEGACY_INVOICE_DETAILS_LOOSE = frozenset(
    {
        _sheet_loose_norm_key("Invoice details-H(all)&T(Psp)"),
        _sheet_loose_norm_key("Invoice detailsH(all)&T(PSP)"),
        _sheet_loose_norm_key("Invoice details-T(labs)"),
    }
)

_RECV_AS_ON_NORM = _sheet_norm_key("Receivable as on")


def _configured_receivable_as_on_tab(sheet_name: str) -> bool:
    """True when the pack names the dated receivable tab without a fixed suffix."""

    n = _sheet_norm_key(sheet_name)
    return n == _RECV_AS_ON_NORM or n.startswith(_RECV_AS_ON_NORM)


def _resolve_merged_invoice_details_ht_tab(wb, sheet_name: str) -> str | None:
    """Resolve legacy invoice tabs to ``Invoice details-H&T`` (post Jun-2026 format)."""

    if _sheet_loose_norm_key(sheet_name) not in _LEGACY_INVOICE_DETAILS_LOOSE:
        return None
    loose_groups: dict[str, list[str]] = {}
    for n in wb.sheetnames:
        lk = _sheet_loose_norm_key(n)
        loose_groups.setdefault(lk, []).append(n)
    cands = loose_groups.get(_INVOICE_DETAILS_HT_LOOSE)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    return _best_sheet_name_match("Invoice details-H&T", cands)


def _resolve_receivable_as_on_tab(wb, sheet_name: str) -> str | None:
    """Resolve ``Receivable as on <date>`` when the pack configures ``Receivable as on``."""

    if not _configured_receivable_as_on_tab(sheet_name):
        return None
    from app.services.receivable_dashboard import is_receivable_tab

    hits = [n for n in wb.sheetnames if is_receivable_tab(n)]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return _best_sheet_name_match("Receivable as on", hits)


def read_sheet(
    xlsx_path: Path | str,
    sheet_name: str,
    *,
    header_hints: Iterable[str],
    max_header_scan: int = 10,
) -> SheetData:
    """Load a single sheet from ``xlsx_path`` and return :class:`SheetData`.

    Two-pass streaming (N5): first pass peeks only the top
    ``max_header_scan`` rows to locate the header row; second pass iterates
    data rows lazily and accumulates only non-blank ones. The full sheet is
    never materialized as a Python list — important for the receivables
    xlsx which can carry tens of thousands of invoice rows.
    """

    cfg = HeaderDetectionConfig(
        hints=frozenset(_norm(h) for h in header_hints if h),
        max_header_scan=max_header_scan,
    )
    wb = load_workbook(filename=str(xlsx_path), data_only=True, read_only=True)
    try:
        real_name = _resolve_sheet_name(wb, sheet_name)
        ws = wb[real_name]

        # Pass 1: header detection only reads the first ``max_header_scan``
        # rows. openpyxl's read-only mode streams, so this bounds memory.
        peek_rows: list[list[object]] = []
        row_iter = ws.iter_rows(values_only=True)
        for _ in range(cfg.max_header_scan):
            try:
                peek_rows.append(list(next(row_iter)))
            except StopIteration:
                break
        header_idx = detect_header_row(peek_rows, cfg)
        raw_headers = list(peek_rows[header_idx]) if header_idx < len(peek_rows) else []
        while raw_headers and (raw_headers[-1] is None or str(raw_headers[-1]).strip() == ""):
            raw_headers.pop()
        display_headers = tuple(
            ((str(h).replace("\u00a0", " ")).strip() if h is not None else "")
            for h in raw_headers
        )
        canonical_keys = _make_canonical_keys(display_headers)
        width = len(canonical_keys)

        def _emit(r: tuple[object, ...] | list[object]) -> dict[str, object] | None:
            slice_ = list(r[:width]) + [None] * max(0, width - len(r))
            if all(c is None or (isinstance(c, str) and c.strip() == "") for c in slice_):
                return None
            return {canonical_keys[i]: slice_[i] for i in range(width)}

        out_rows: list[dict[str, object]] = []
        # Any rows that were peeked beyond the header row are real data —
        # emit them before advancing the stream.
        for r in peek_rows[header_idx + 1 :]:
            emitted = _emit(r)
            if emitted is not None:
                out_rows.append(emitted)
        # Stream the remainder. No full-sheet list materialization.
        for r in row_iter:
            emitted = _emit(r)
            if emitted is not None:
                out_rows.append(emitted)

        return SheetData(
            sheet_name=real_name,
            display_headers=display_headers,
            canonical_keys=canonical_keys,
            rows=tuple(out_rows),
            header_row_index=header_idx,
        )
    finally:
        wb.close()
