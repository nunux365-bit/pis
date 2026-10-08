"""Promised ETA — collect candidates from all sources, resolve first vs current comms."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.order_rca.constants import (
    DELIVERY_BREACH_DELIVERED_LATE,
    DELIVERY_BREACH_DELIVERED_ON_TIME,
    DELIVERY_BREACH_OPEN_PAST,
    DELIVERY_BREACH_PENDING_WITHIN,
    DELIVERY_BREACH_UNKNOWN_ANCHOR,
    ORDER_RCA_DISPLAY_TZ_LABEL,
    ORDER_RCA_DISPLAY_TZ_NAME,
)
from app.agents.order_rca.time_utils import (
    ORDER_RCA_DISPLAY_TZ,
    anchor_ist,
    format_duration_minutes,
    format_ist_wall,
    format_instant_ist,
    parse_eta_to_unix,
    parse_num,
    parse_str,
    parse_unix_ts,
)

_SNAPSHOT_FIRST_SOURCES = ("order.promised_eta", "order.eta.eta_to", "order.eta.to_date")
_COMMS_SORT_MISSING_CREATED = 2**62
_INSTANT_DEDUPE_TOLERANCE_SEC = 60

_CUSTOMER_DATE_ONLY_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s*,?\s+\d{4}$", re.I)
_CUSTOMER_COMMS_RE = re.compile(
    r"^\d{1,2}\s+[A-Za-z]+\s*,?\s+\d{4}(\s+\d{1,2}:\d{2})?$",
    re.I,
)
_ETA_COMMS_COMMENT_RE = re.compile(
    r"eta_communicated\|from:\s*(.*?)\|to:\s*(.*)$",
    re.IGNORECASE,
)
_COMMS_DATE_FMTS: tuple[tuple[str, bool], ...] = (
    ("%d %B, %Y %H:%M", True),
    ("%d %b, %Y %H:%M", True),
    ("%d %B, %Y", False),
    ("%d %b, %Y", False),
    ("%d %B %Y %H:%M", True),
    ("%d %b %Y %H:%M", True),
    ("%d %B %Y", False),
    ("%d %b %Y", False),
    ("%d %B, %Y %H:%M:%S", True),
    ("%d %b, %Y %H:%M:%S", True),
)

_FIRST_PROMISE_SOURCE_RANK: dict[str, int] = {
    "history.eta_communicated": 0,
    "order.confirmation_eta_information": 1,
    "analytics.confirmation_eta_information": 1,
    "analytics.order_sla": 2,
    "order_lines.rapid_details.rapid_eta": 3,
    "analytics.eta": 4,
}


@dataclass(frozen=True)
class EtaCandidate:
    kind: str
    source: str
    communicated_at: int | None
    instant: datetime
    has_time: bool
    raw: Any
    comms_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "communicated_at": self.communicated_at,
            "instant": self.instant.isoformat(),
            "has_time": self.has_time,
            "raw": self.raw,
            "comms_from": self.comms_from,
        }


def parse_eta_communicated_comment(comment: str) -> tuple[str | None, str | None]:
    """
    Parse ``PO|eta_communicated|from: …|to: …`` history lines.

    SLA and display always use the **to** segment (revised promise). ``from`` is context only.
    """
    s = (comment or "").strip()
    if "eta_communicated" not in s.lower():
        return None, None
    m = _ETA_COMMS_COMMENT_RE.search(s)
    if m:
        from_part = (m.group(1) or "").strip() or None
        to_part = (m.group(2) or "").strip() or None
        return from_part, to_part
    lower = s.lower()
    to_part: str | None = None
    from_part: str | None = None
    if "|to:" in lower:
        to_idx = lower.index("|to:")
        to_part = s[to_idx + 4 :].strip()
    if "|from:" in lower:
        from_idx = lower.index("|from:")
        end = lower.index("|to:") if "|to:" in lower else len(s)
        from_part = s[from_idx + 6 : end].strip().strip("|") or None
    return from_part, to_part


def _anchor_ist(dt: datetime) -> datetime:
    return anchor_ist(dt)


def _normalize_comms_date_string(value: str) -> str:
    return " ".join(value.replace(",", " ").split())


def _parse_customer_comms_string(value: Any) -> tuple[datetime | None, bool]:
    """Customer-facing comms strings (history |to:, analytics eta_to) are IST wall clock."""
    s = parse_str(value)
    if not s:
        return None, False
    normalized = _normalize_comms_date_string(s)
    if _CUSTOMER_DATE_ONLY_RE.match(s) or _CUSTOMER_COMMS_RE.match(s):
        for fmt, has_time in _COMMS_DATE_FMTS:
            try:
                dt = datetime.strptime(normalized.replace("  ", " ").strip(), fmt)
                return _anchor_ist(dt), has_time
            except ValueError:
                continue
        for fmt, has_time in (
            ("%d %B %Y %H:%M", True),
            ("%d %b %Y %H:%M", True),
            ("%d %B %Y", False),
            ("%d %b %Y", False),
        ):
            try:
                dt = datetime.strptime(normalized, fmt)
                return _anchor_ist(dt), has_time
            except ValueError:
                continue
    return None, False


def _instants_match(a: datetime, b: datetime, *, tolerance_sec: int = _INSTANT_DEDUPE_TOLERANCE_SEC) -> bool:
    return abs((a - b).total_seconds()) <= tolerance_sec


def _decode_utc_epoch(value: Any) -> datetime | None:
    """True UTC unix epoch → IST-aware datetime."""
    dt = parse_unix_ts(value)
    return _anchor_ist(dt) if dt else None


def _decode_eta_wall_epoch(value: Any) -> datetime | None:
    """``order.eta.eta_to`` unix — IST wall clock stored in the unix slot."""
    dt = parse_eta_to_unix(value)
    return _anchor_ist(dt) if dt else None


def _parse_order_snapshot_instant(source: str, value: Any) -> tuple[datetime | None, bool]:
    if source == "order.eta.eta_to":
        dt = _decode_eta_wall_epoch(value)
        return (dt, True) if dt else (None, True)
    dt = _decode_utc_epoch(value)
    return (dt, True) if dt else (None, True)


def _communicated_at_unix(value: Any) -> int | None:
    n = parse_num(value)
    if n is None or n <= 0:
        return None
    return int(n)


def _history_items(history_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not history_payload:
        return []
    items = history_payload.get("history") or history_payload.get("data") or []
    return [e for e in items if isinstance(e, dict)]


def _history_for_order(history_payload: dict[str, Any] | None, order_id: str) -> list[dict[str, Any]]:
    oid = order_id.strip().upper()
    out: list[dict[str, Any]] = []
    for entry in _history_items(history_payload):
        comment = parse_str(entry.get("comment")) or ""
        if oid not in comment or "eta_communicated" not in comment or "|to:" not in comment:
            continue
        out.append(entry)
    return out


def _analytics_rows(analytics_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not analytics_payload:
        return []
    data = analytics_payload.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _allocation_instants_from_analytics(analytics: dict[str, Any] | None) -> list[datetime]:
    instants: list[datetime] = []
    for row in _analytics_rows(analytics):
        if parse_str(row.get("analytics_data_type")) != "allocation_eta_information":
            continue
        data = row.get("analytics_data")
        if not isinstance(data, dict):
            continue
        unix = data.get("order_confirmed_eta") or (data.get("sla_eta") or {}).get("eta_to")
        dt = _decode_utc_epoch(unix)
        if dt:
            instants.append(dt)
    return instants


def _placement_unix_from_order_sla(data: dict[str, Any]) -> tuple[Any, int | None]:
    """Earliest opted ``sku_sla.eta_to`` from cart SLA payload."""
    best_unix: Any = None
    best_created: int | None = None
    cart_sla = data.get("cart_sla")
    if not isinstance(cart_sla, list):
        return None, None
    for block in cart_sla:
        if not isinstance(block, dict):
            continue
        for shipment in block.get("shipments") or []:
            if not isinstance(shipment, dict):
                continue
            for sku_sla in shipment.get("sku_sla") or []:
                if not isinstance(sku_sla, dict) or not sku_sla.get("opted"):
                    continue
                eta_to = sku_sla.get("eta_to")
                if eta_to is None:
                    continue
                if best_unix is None:
                    best_unix = eta_to
    return best_unix, best_created


def _corroborated_placement_unix(
    order: dict[str, Any],
    analytics: dict[str, Any] | None,
) -> set[int]:
    """Unix values that appear on both placement-style order fields and analytics."""
    anchors: set[int] = set()

    def _add_anchor(value: Any) -> None:
        n = parse_num(value)
        if n is not None and n > 0:
            anchors.add(int(n))

    conf_unix: int | None = None
    sla_unix: int | None = None
    order_conf = order.get("confirmation_eta_information")
    if isinstance(order_conf, dict):
        n = parse_num(
            order_conf.get("min_order_confirmed_eta")
            or order_conf.get("max_order_confirmed_eta")
            or order_conf.get("order_confirmed_eta")
        )
        if n is not None:
            conf_unix = int(n)
            _add_anchor(n)

    for row in _analytics_rows(analytics):
        atype = parse_str(row.get("analytics_data_type"))
        data = row.get("analytics_data")
        if not isinstance(data, dict):
            continue
        if atype == "confirmation_eta_information":
            _add_anchor(data.get("min_order_confirmed_eta"))
            _add_anchor(data.get("max_order_confirmed_eta"))
            n = parse_num(data.get("min_order_confirmed_eta") or data.get("max_order_confirmed_eta"))
            if n is not None:
                conf_unix = int(n)
        elif atype == "order_sla":
            u, _ = _placement_unix_from_order_sla(data)
            n = parse_num(u)
            if n is not None:
                sla_unix = int(n)

    _add_anchor(order.get("promised_eta"))
    _add_anchor(order.get("least_eta"))
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        rd = ln.get("rapid_details")
        if isinstance(rd, dict):
            _add_anchor(rd.get("rapid_eta"))

    corroborated: set[int] = set()
    if conf_unix is not None and conf_unix in anchors:
        corroborated.add(conf_unix)
    if sla_unix is not None and sla_unix in anchors:
        corroborated.add(sla_unix)
    if conf_unix is not None and sla_unix is not None and conf_unix == sla_unix:
        corroborated.add(conf_unix)
    return corroborated


def collect_eta_candidates(
    order_id: str,
    order: dict[str, Any],
    history: dict[str, Any] | None,
    analytics: dict[str, Any] | None,
) -> list[EtaCandidate]:
    """Gather ETA signals for one PO (no source preference)."""
    candidates: list[EtaCandidate] = []
    eta_o = order.get("eta") or {}

    if eta_o.get("eta_to"):
        dt = _decode_eta_wall_epoch(eta_o.get("eta_to"))
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="order_snapshot",
                    source="order.eta.eta_to",
                    communicated_at=None,
                    instant=dt,
                    has_time=True,
                    raw=eta_o.get("eta_to"),
                )
            )
    if order.get("promised_eta"):
        dt, ht = _parse_order_snapshot_instant("order.promised_eta", order.get("promised_eta"))
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="order_snapshot",
                    source="order.promised_eta",
                    communicated_at=None,
                    instant=dt,
                    has_time=ht,
                    raw=order.get("promised_eta"),
                )
            )
    to_date = parse_str(eta_o.get("to_date"))
    if to_date:
        dt, ht = _parse_customer_comms_string(to_date)
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="order_snapshot",
                    source="order.eta.to_date",
                    communicated_at=None,
                    instant=dt,
                    has_time=ht,
                    raw=to_date,
                )
            )

    order_conf = order.get("confirmation_eta_information")
    if isinstance(order_conf, dict):
        unix = (
            order_conf.get("min_order_confirmed_eta")
            or order_conf.get("max_order_confirmed_eta")
            or order_conf.get("order_confirmed_eta")
        )
        placed_at = _communicated_at_unix(order_conf.get("order_confirmation_timestamp"))
        dt = _decode_utc_epoch(unix)
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="placement",
                    source="order.confirmation_eta_information",
                    communicated_at=placed_at,
                    instant=dt,
                    has_time=True,
                    raw=unix,
                )
            )

    for entry in _history_for_order(history, order_id):
        comment = parse_str(entry.get("comment")) or ""
        from_raw, to_raw = parse_eta_communicated_comment(comment)
        if not to_raw:
            continue
        dt, ht = _parse_customer_comms_string(to_raw)
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="comms",
                    source="history.eta_communicated",
                    communicated_at=_communicated_at_unix(entry.get("created")),
                    instant=dt,
                    has_time=ht,
                    raw=to_raw,
                    comms_from=from_raw,
                )
            )

    for row in _analytics_rows(analytics):
        atype = parse_str(row.get("analytics_data_type"))
        created = _communicated_at_unix(row.get("created"))
        data = row.get("analytics_data")
        if atype == "confirmation_eta_information" and isinstance(data, dict):
            unix = data.get("min_order_confirmed_eta") or data.get("max_order_confirmed_eta")
            dt = _decode_utc_epoch(unix)
            placed_at = _communicated_at_unix(data.get("order_confirmation_timestamp")) or created
            if dt:
                candidates.append(
                    EtaCandidate(
                        kind="placement",
                        source="analytics.confirmation_eta_information",
                        communicated_at=placed_at,
                        instant=dt,
                        has_time=True,
                        raw=unix,
                    )
                )
        elif atype == "order_sla" and isinstance(data, dict):
            unix, _ = _placement_unix_from_order_sla(data)
            dt = _decode_utc_epoch(unix)
            if dt:
                candidates.append(
                    EtaCandidate(
                        kind="placement",
                        source="analytics.order_sla",
                        communicated_at=created,
                        instant=dt,
                        has_time=True,
                        raw=unix,
                    )
                )
        elif atype == "eta" and isinstance(data, dict):
            eta_to = data.get("eta_to")
            eta_from = parse_str(data.get("eta_from")) or parse_str(data.get("text"))
            if not eta_to:
                continue
            dt, ht = _parse_customer_comms_string(eta_to)
            if dt:
                candidates.append(
                    EtaCandidate(
                        kind="comms",
                        source="analytics.eta",
                        communicated_at=created,
                        instant=dt,
                        has_time=ht,
                        raw=eta_to,
                        comms_from=eta_from or None,
                    )
                )
        elif atype == "allocation_eta_information" and isinstance(data, dict):
            unix = data.get("order_confirmed_eta") or (data.get("sla_eta") or {}).get("eta_to")
            dt = _decode_utc_epoch(unix)
            if dt:
                candidates.append(
                    EtaCandidate(
                        kind="allocation",
                        source="analytics.allocation_eta_information",
                        communicated_at=created,
                        instant=dt,
                        has_time=True,
                        raw=unix,
                    )
                )

    corroborated = _corroborated_placement_unix(order, analytics)
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        rd = ln.get("rapid_details")
        if not isinstance(rd, dict):
            continue
        unix = rd.get("rapid_eta")
        n = parse_num(unix)
        if n is None or int(n) not in corroborated:
            continue
        dt = _decode_utc_epoch(unix)
        if dt:
            candidates.append(
                EtaCandidate(
                    kind="placement",
                    source="order_lines.rapid_details.rapid_eta",
                    communicated_at=None,
                    instant=dt,
                    has_time=True,
                    raw=unix,
                )
            )

    return candidates


def _first_promise_candidates(candidates: list[EtaCandidate]) -> list[EtaCandidate]:
    return [c for c in candidates if c.kind in ("comms", "placement")]


def _first_promise_sort_key(c: EtaCandidate) -> tuple[int, int, int, datetime]:
    at = c.communicated_at if c.communicated_at is not None else _COMMS_SORT_MISSING_CREATED
    has_time_rank = 0 if c.has_time else 1
    source_rank = _FIRST_PROMISE_SOURCE_RANK.get(c.source, 9)
    return (at, has_time_rank, source_rank, c.instant)


def _comms_sort_key(c: EtaCandidate) -> tuple[int, int, int, datetime]:
    """Chronological comms for timeline (history before analytics at same second)."""
    at = c.communicated_at if c.communicated_at is not None else _COMMS_SORT_MISSING_CREATED
    has_time_rank = 0 if c.has_time else 1
    source_rank = 0 if c.source == "history.eta_communicated" else 1
    return (at, has_time_rank, source_rank, c.instant)


def _order_eta_fillers(candidates: list[EtaCandidate], day: datetime.date) -> list[EtaCandidate]:
    out: list[EtaCandidate] = []
    for f in candidates:
        if not f.has_time or f.instant.date() != day:
            continue
        out.append(f)
    for c in candidates:
        if c.source != "order.eta.eta_to" or c.raw is None:
            continue
        wall_dt = _decode_eta_wall_epoch(c.raw)
        utc_dt = _decode_utc_epoch(c.raw)
        for label, dt in (("wall", wall_dt), ("utc", utc_dt)):
            if not dt:
                continue
            inst = _anchor_ist(dt)
            if inst.date() != day:
                continue
            if any(x.source == f"order.eta.eta_to ({label})" for x in out):
                continue
            out.append(
                EtaCandidate(
                    kind="order_snapshot",
                    source=f"order.eta.eta_to ({label})",
                    communicated_at=None,
                    instant=inst,
                    has_time=True,
                    raw=c.raw,
                )
            )
    return out


def _fill_time_on_day(instant: datetime, fillers: list[EtaCandidate]) -> tuple[datetime, str | None]:
    day = instant.date()
    same_day = _order_eta_fillers(fillers, day)
    same_day.sort(
        key=lambda f: (
            0 if f.kind == "allocation" else 1,
            0 if f.kind == "comms" and f.has_time else 2,
            0 if f.source == "order.eta.eta_to" else 3,
            f.communicated_at if f.communicated_at is not None else 0,
        )
    )
    if same_day:
        t = same_day[0].instant
        merged = instant.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        return _anchor_ist(merged), same_day[0].source
    eod = instant.replace(hour=23, minute=59, second=0, microsecond=0)
    return _anchor_ist(eod), "date-only EOD"


def _resolve_comms_row(
    row: EtaCandidate,
    candidates: list[EtaCandidate],
) -> tuple[EtaCandidate, str | None]:
    if row.has_time:
        return row, None
    instant, fill_src = _fill_time_on_day(row.instant, candidates)
    return (
        EtaCandidate(
            kind=row.kind,
            source=row.source,
            communicated_at=row.communicated_at,
            instant=instant,
            has_time=True,
            raw=row.raw,
            comms_from=row.comms_from,
        ),
        fill_src,
    )


def _format_recorded_at(communicated_at: int | None) -> str | None:
    if not communicated_at:
        return None
    dt = _decode_utc_epoch(communicated_at)
    if not dt:
        return None
    return format_instant_ist(dt)


def _format_comms_from_display(from_raw: str | None) -> str | None:
    if not from_raw:
        return None
    dt, _ = _parse_customer_comms_string(from_raw)
    if dt:
        return format_ist_wall(dt)
    return from_raw if from_raw.upper().endswith(ORDER_RCA_DISPLAY_TZ_LABEL) else f"{from_raw} {ORDER_RCA_DISPLAY_TZ_LABEL}"


def _format_resolved(
    candidate: EtaCandidate,
    time_filled_from: str | None = None,
    *,
    comms_from: str | None = None,
) -> dict[str, Any]:
    display = format_ist_wall(candidate.instant)
    source = candidate.source
    if time_filled_from:
        source = f"{source} + {time_filled_from}"
    recorded_at = _format_recorded_at(candidate.communicated_at)
    from_display = _format_comms_from_display(comms_from if comms_from is not None else candidate.comms_from)
    return {
        "instant": candidate.instant.isoformat(),
        "display": display,
        "source": source,
        "communicated_at": candidate.communicated_at,
        "communicated_at_display": recorded_at,
        "recorded_at_display": recorded_at,
        "comms_from_display": from_display,
        "kind": candidate.kind,
        "has_time": candidate.has_time,
    }


def _snapshot_for_first(snapshots: list[EtaCandidate]) -> EtaCandidate:
    for src in _SNAPSHOT_FIRST_SOURCES:
        for c in snapshots:
            if c.source == src:
                return c
    return min(snapshots, key=lambda c: c.instant)


def _snapshot_for_current(snapshots: list[EtaCandidate]) -> EtaCandidate:
    for src in ("order.eta.eta_to", "order.promised_eta", "order.eta.to_date"):
        for c in snapshots:
            if c.source == src:
                return c
    return max(snapshots, key=lambda c: c.instant)


def _dedupe_timeline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate promise instants for delivery ETA UI (any position)."""
    out: list[dict[str, Any]] = []
    seen: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        iso = row.get("instant")
        if not iso:
            continue
        try:
            inst = _anchor_ist(datetime.fromisoformat(str(iso)))
        except (TypeError, ValueError):
            continue
        if any(_instants_match(prev, inst) for prev in seen):
            continue
        seen.append(inst)
        out.append(row)
    return out


def build_eta_jumps(
    *,
    promised_first: dict[str, Any] | None,
    promised_current: dict[str, Any] | None,
    eta_timeline: list[dict[str, Any]] | None,
    actual_display: str | None,
) -> list[dict[str, Any]]:
    """
    Delivery ETA strip: First promised → ETA 1..N → Actual (IST).

    When the order snapshot ``promised_current`` is newer than the last comms revision,
    it is appended as the next ETA N (no separate Current label).
    """
    revisions = _dedupe_timeline_rows(list(eta_timeline or []))
    if not revisions and isinstance(promised_first, dict) and promised_first.get("instant"):
        revisions = [promised_first]

    current = promised_current if isinstance(promised_current, dict) else {}
    cur_iso = current.get("instant")
    if cur_iso:
        try:
            cur_inst = _anchor_ist(datetime.fromisoformat(str(cur_iso)))
            known = [
                _anchor_ist(datetime.fromisoformat(str(r.get("instant"))))
                for r in revisions
                if r.get("instant")
            ]
            if not any(_instants_match(k, cur_inst) for k in known):
                revisions.append(current)
        except (TypeError, ValueError):
            pass
    revisions = _dedupe_timeline_rows(revisions)

    jumps: list[dict[str, Any]] = []
    for i, row in enumerate(revisions):
        label = "First promised" if i == 0 else f"ETA {i}"
        jumps.append(
            {
                "label": label,
                "role": "revision",
                "display": row.get("display"),
                "instant": row.get("instant"),
                "from_display": row.get("comms_from_display"),
                "recorded_at_display": row.get("recorded_at_display"),
                "source": row.get("source"),
            }
        )

    if actual_display:
        jumps.append(
            {
                "label": "Actual",
                "role": "actual",
                "display": actual_display,
                "instant": None,
                "from_display": None,
                "recorded_at_display": None,
                "source": "order.shipment_detail.delivery_date",
            }
        )

    return jumps


def resolve_promised_eta(candidates: list[EtaCandidate]) -> dict[str, Any]:
    """
    Promised SLA = earliest placement/comms. Current = latest comms or order snapshot if newer.
    """
    promise_pool = _first_promise_candidates(candidates)
    timeline: list[dict[str, Any]] = []

    if promise_pool:
        sorted_pool = sorted(promise_pool, key=_first_promise_sort_key)
        for row in sorted_pool:
            resolved, fill = _resolve_comms_row(row, candidates)
            entry = _format_resolved(resolved, fill, comms_from=row.comms_from)
            timeline.append(entry)

        timeline = _dedupe_timeline_rows(timeline)

        first_row = sorted_pool[0]
        first_resolved, first_fill = _resolve_comms_row(first_row, candidates)

        comms_only = [c for c in candidates if c.kind == "comms"]
        if comms_only:
            sorted_comms = sorted(comms_only, key=_comms_sort_key)
            last_row = sorted_comms[-1]
        else:
            last_row = sorted_pool[-1]
        last_resolved, last_fill = _resolve_comms_row(last_row, candidates)

        current_resolved = last_resolved
        current_fill = last_fill
        snapshots = [c for c in candidates if c.kind == "order_snapshot"]
        if snapshots:
            snap = _snapshot_for_current(snapshots)
            if snap.instant > current_resolved.instant:
                current_resolved = snap
                current_fill = None

        promised_first = _format_resolved(first_resolved, first_fill, comms_from=first_row.comms_from)
        promised_current = _format_resolved(
            current_resolved, current_fill, comms_from=last_row.comms_from
        )
        mode = "first_comms"
    else:
        snapshots = [c for c in candidates if c.kind == "order_snapshot" and c.instant]
        if not snapshots:
            return {
                "promised_first": None,
                "promised_current": None,
                "promised_mode": "none",
                "eta_timeline": [],
                "candidates": [c.to_dict() for c in candidates],
            }
        promised_first = _format_resolved(_snapshot_for_first(snapshots))
        promised_current = _format_resolved(_snapshot_for_current(snapshots))
        timeline = [promised_first]
        if not _instants_match(
            datetime.fromisoformat(str(promised_first["instant"])),
            datetime.fromisoformat(str(promised_current["instant"])),
        ):
            timeline.append(promised_current)
        mode = "order_snapshot_only"

    return {
        "promised_first": promised_first,
        "promised_current": promised_current,
        "promised_mode": mode,
        "eta_timeline": timeline,
        "candidates": [c.to_dict() for c in candidates],
    }


def promised_instant_for_sla(resolved: dict[str, Any]) -> datetime | None:
    first = resolved.get("promised_first")
    if not isinstance(first, dict):
        return None
    iso = first.get("instant")
    if not iso:
        return None
    try:
        return _anchor_ist(datetime.fromisoformat(iso))
    except ValueError:
        return None


def _eta_breached_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def compute_delivery_sla(
    *,
    promised_first: dict[str, Any] | None,
    actual_delivery: str | None = None,
    late_minutes: int | None = None,
    is_eta_breached_order_api: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """
    Canonical delivery SLA for RCA (UI, Perfect Order, LLM).

    - ``is_eta_breached``: computed vs promised_first only.
    - ``breach_kind``: drives wording (delivered late vs open past promise).
    - ``breach_minutes``: late_minutes when delivered late; minutes past first promise when open.
    """
    delivered = bool(parse_str(actual_delivery))
    late_n = int(late_minutes) if isinstance(late_minutes, (int, float)) and late_minutes > 0 else None

    anchor_dt: datetime | None = None
    past_anchor = False
    minutes_past: int | None = None
    first = promised_first if isinstance(promised_first, dict) else {}
    instant_iso = first.get("instant")

    if instant_iso:
        try:
            anchor_dt = _anchor_ist(datetime.fromisoformat(str(instant_iso)))
            now = as_of if as_of is not None else datetime.now(anchor_dt.tzinfo)
            if now.tzinfo is None:
                now = now.replace(tzinfo=anchor_dt.tzinfo)
            else:
                now = now.astimezone(anchor_dt.tzinfo)
            if now > anchor_dt:
                past_anchor = True
                minutes_past = max(0, int((now - anchor_dt).total_seconds() // 60))
        except (TypeError, ValueError):
            anchor_dt = None

    if delivered:
        if late_n:
            breach_kind = DELIVERY_BREACH_DELIVERED_LATE
            breach_minutes = late_n
        else:
            breach_kind = DELIVERY_BREACH_DELIVERED_ON_TIME
            breach_minutes = None
    elif past_anchor:
        breach_kind = DELIVERY_BREACH_OPEN_PAST
        breach_minutes = minutes_past
    elif anchor_dt is not None:
        breach_kind = DELIVERY_BREACH_PENDING_WITHIN
        breach_minutes = None
    else:
        breach_kind = DELIVERY_BREACH_UNKNOWN_ANCHOR
        breach_minutes = None

    is_breached = breach_kind in (DELIVERY_BREACH_OPEN_PAST, DELIVERY_BREACH_DELIVERED_LATE)
    if breach_kind == DELIVERY_BREACH_UNKNOWN_ANCHOR:
        is_breached = _eta_breached_flag(is_eta_breached_order_api)

    diagnosed = as_of if as_of is not None else datetime.now(ZoneInfo(ORDER_RCA_DISPLAY_TZ_NAME))
    if diagnosed.tzinfo is None:
        diagnosed = diagnosed.replace(tzinfo=ZoneInfo(ORDER_RCA_DISPLAY_TZ_NAME))

    return {
        "breach_kind": breach_kind,
        "is_eta_breached": is_breached,
        "breach_minutes": breach_minutes,
        "breach_minutes_display": format_duration_minutes(breach_minutes),
        "delivered": delivered,
        "diagnosed_at": diagnosed.isoformat(),
        "is_eta_breached_order_api": is_eta_breached_order_api,
    }


def compute_delivery_sla_narrative(
    *,
    promised_first: dict[str, Any] | None,
    actual_delivery: str | None = None,
    late_minutes: int | None = None,
    is_eta_breached: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Deprecated alias — use ``compute_delivery_sla``."""
    return compute_delivery_sla(
        promised_first=promised_first,
        actual_delivery=actual_delivery,
        late_minutes=late_minutes,
        is_eta_breached_order_api=is_eta_breached,
        as_of=as_of,
    )
