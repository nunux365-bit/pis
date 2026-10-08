"""
Pure helpers for contract ingest (no DB). Shared by ``ingest_async`` and tests.

All agenos persistence lives in ``ingest_async`` + ``run_agenos_async``; sync ``ingest.ingest_contract_payload``
is a thin wrapper only.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, Iterable


def _normalize_site_label_for_dedupe(value: str | None) -> str:
    """Lowercase, collapse spaces, normalize Unicode dashes — for matching duplicate plants."""
    s = (value or "").strip().lower()
    for ch in ("\u2013", "\u2014", "\u2212", "–", "—"):
        s = s.replace(ch, "-")
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if len(s) < 8:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _site_key_token_overlap(incoming_sk: str | None, candidate_sk: str | None) -> int:
    """Count of significant shared tokens between two ``site_key`` strings (underscore-separated)."""
    if not incoming_sk or not candidate_sk:
        return 0
    ta = {t for t in str(incoming_sk).lower().split("_") if len(t) >= 3}
    tb = {t for t in str(candidate_sk).lower().split("_") if len(t) >= 3}
    return len(ta & tb)


def _incoming_site_key(site: dict[str, Any]) -> str | None:
    sk_in = site.get("site_key")
    if sk_in is None:
        return None
    if isinstance(sk_in, str):
        return sk_in.strip() or None
    return str(sk_in).strip() or None


def _service_site_label_matches_from_rows(
    rows: Iterable[dict[str, Any]],
    site: dict[str, Any],
) -> list[tuple[uuid.UUID, str | None, Any]]:
    """
    Rows are mapping-like dicts with keys id, site_key, canonical_name, display_name, created_at
    (same shape as ``ingest_async`` SELECT).
    """
    canon = (site.get("canonical_name") or "").strip()
    disp = (site.get("display_name") or "").strip()
    nc = _normalize_site_label_for_dedupe(canon) if canon else ""
    nd = _normalize_site_label_for_dedupe(disp) if disp else ""
    if not nc and not nd:
        return []

    matches: list[tuple[uuid.UUID, str | None, Any]] = []
    for d in rows:
        rid = d["id"]
        msk = d.get("site_key")
        cn = d.get("canonical_name") or ""
        dn = d.get("display_name") or ""
        mcr = d.get("created_at")
        hit = False
        if nc and _normalize_site_label_for_dedupe(str(cn)) == nc:
            hit = True
        if nd and _normalize_site_label_for_dedupe(str(dn)) == nd:
            hit = True
        if hit:
            matches.append((uuid.UUID(str(rid)), str(msk).strip() if msk else None, mcr))
    return matches


def _pick_existing_service_site_from_matches(
    matches: list[tuple[uuid.UUID, str | None, Any]],
    *,
    incoming_site_key: str | None,
    billable_site_ids: set[str],
) -> uuid.UUID | None:
    """
    When several ``service_site`` rows share the same normalized label, pick one winner or None.

    See ``ingest_async._find_existing_service_site_id_async`` for full semantics.
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][0]

    def _created_sort(mcr: Any) -> datetime:
        if mcr is None:
            return datetime(9999, 12, 31)
        if isinstance(mcr, datetime):
            return mcr.replace(tzinfo=None) if mcr.tzinfo else mcr
        return datetime(9999, 12, 31)

    def sort_key(m: tuple[uuid.UUID, str | None, Any]) -> tuple:
        mid, msk2, mcr = m
        b = 1 if str(mid) in billable_site_ids else 0
        ov = _site_key_token_overlap(incoming_site_key, msk2)
        return (-b, -ov, _created_sort(mcr))

    matches = list(matches)
    matches.sort(key=sort_key)
    best = matches[0]
    second = matches[1]
    b0 = 1 if str(best[0]) in billable_site_ids else 0
    o0 = _site_key_token_overlap(incoming_site_key, best[1])
    b1 = 1 if str(second[0]) in billable_site_ids else 0
    o1 = _site_key_token_overlap(incoming_site_key, second[1])
    if (b0, o0) == (b1, o1):
        return None
    return best[0]


def _slug(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")[:80]
    return s or "client"


_NO_OHC_ROSTER_BILLING_MODELS = frozenset(
    {"fixed_monthly", "as_per_actuals", "milestone", "retainer_variable", "per_head"}
)

_NON_HUMAN_ROLE_SUBSTRINGS = (
    "AMBULANCE",
    "BMW",
    "MEDICINES",
    "EQUIP",
    "HEALTH_PACKAGE",
    "DRIVER",
    "DISPOSAL",
    "WASTE",
)

_STAFF_ATTENDANCE_ROLE_PREFIXES = (
    "MO_",
    "FMO_",
    "SR_MO_",
    "JR_MO_",
    "COMPANY_MO_",
    "NURSE_",
    "PARAMEDIC_",
)

_STAFF_ATTENDANCE_ROLE_CODES = frozenset({"DOCTOR", "PHYSICIAN", "VISITING_MEDICAL_OFFICER"})


def _role_code_is_non_human_medical_staffing(rc: str) -> bool:
    return any(tok in rc for tok in _NON_HUMAN_ROLE_SUBSTRINGS)


def _normalize_rate_lines_attendance_required(rates: list[dict[str, Any]]) -> bool:
    """
    Set ``attendance_required=true`` on **human** physician/nurse/paramedic lines when the extract
    left it false, so MIS paths do not treat them as anonymous lump fees.

    Returns True if any rate line was modified.
    """
    changed = False
    for rl in rates:
        if not isinstance(rl, dict):
            continue
        bm = str(rl.get("billing_model") or "").strip()
        if bm in _NO_OHC_ROSTER_BILLING_MODELS:
            continue
        rc = str(rl.get("role_code") or "").strip().upper()
        if not rc:
            continue
        if _role_code_is_non_human_medical_staffing(rc):
            continue
        human = rc in _STAFF_ATTENDANCE_ROLE_CODES or any(rc.startswith(p) for p in _STAFF_ATTENDANCE_ROLE_PREFIXES)
        if not human:
            continue
        before = rl.get("attendance_required")
        rl["attendance_required"] = True
        if before is not True:
            changed = True
    return changed
