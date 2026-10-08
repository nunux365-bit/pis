"""Shared Mail Master cell reads: header matching + yes-flags + contact lines.

Used by CHW and ePharma master trackers — same robustness: trim, casefold,
whitespace-tolerant header keys; ``yes`` cell tokens; name/phone display + dedupe.
"""

from __future__ import annotations

import html
from typing import Mapping, Sequence


def norm_header(s: str) -> str:
    """Match sheet column headers: trim, casefold, drop all whitespace (incl. NBSP)."""

    t = str(s).replace("\u00a0", " ")
    return "".join(t.split()).casefold()


def cell_by_logical_header(row: Mapping[str, object], logical: str) -> object | None:
    """Get cell value when the row's keys match ``logical`` after :func:`norm_header`."""

    want = norm_header(logical)
    for k, v in row.items():
        if norm_header(str(k)) == want:
            return v
    return None


def norm_yes_token(v: object) -> str:
    """Trim, collapse internal whitespace, casefold — for ``yes`` / ``no`` style flags."""

    if v is None:
        return ""
    t = str(v).replace("\u00a0", " ")
    return "".join(t.split()).casefold()


def display_name(v: object) -> str:
    if v is None:
        return ""
    t = str(v).replace("\u00a0", " ").strip()
    return " ".join(t.split())


def display_phone(v: object) -> str:
    if v is None:
        return ""
    t = str(v).replace("\u00a0", " ")
    return "".join(t.split())


def exclusion_yes_in_rows(
    matches: Sequence[Mapping[str, object]],
    exclusion_column_logical: str,
) -> bool:
    for row in matches:
        v = cell_by_logical_header(row, exclusion_column_logical)
        if v is None:
            continue
        if norm_yes_token(v) == "yes":
            return True
    return False


def contacts_block_html(
    matches: Sequence[Mapping[str, object]],
    *,
    name_column_logical: str,
    phone_column_logical: str,
) -> str:
    """``1/ name - phone`` lines, sheet order, deduped by (name, phone)."""

    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for row in matches:
        a = cell_by_logical_header(row, name_column_logical)
        b = cell_by_logical_header(row, phone_column_logical)
        if a is None or b is None:
            continue
        name = display_name(a)
        ph = display_phone(b)
        if not name or not ph:
            continue
        key = (name.casefold(), ph)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{name} - {ph}")
    if not lines:
        return ""
    inner = "<br/>".join(
        f"{i + 1}/ {html.escape(line)}" for i, line in enumerate(lines)
    )
    return (
        '<p style="margin-top:18px;">You can also contact us on the below-given numbers:</p>'
        f'<p style="margin:0;">{inner}</p>'
    )
