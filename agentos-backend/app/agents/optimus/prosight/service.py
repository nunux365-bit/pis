"""Prosight service — dashboard data management via Databricks."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import ProsightActionable, ProsightSnapshot
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)

# Cache keys and TTL
PROSIGHT_SNAPSHOT_CACHE_KEY = "prosight:latest_snapshot"
PROSIGHT_SNAPSHOT_CACHE_TTL = 60  # 60 seconds - short TTL since data changes daily

# Rows per actionables upsert statement — see the parameter-budget note in
# sync_actionables. 500 x ~32 columns leaves plenty of headroom under 32767.
_ACTIONABLE_UPSERT_CHUNK = 500

# Default/ceiling row counts for list_actionables, so an unfiltered call can
# never stream the whole live set into one response.
DEFAULT_ACTIONABLES_LIMIT = 500
MAX_ACTIONABLES_LIMIT = 2000


async def get_latest_snapshot(session: AsyncSession | None = None) -> dict[str, Any] | None:
    """Get the most recent Prosight dashboard snapshot.

    Returns the full dashboard JSON or None if no snapshots exist.
    Uses Redis caching to avoid repeated DB queries.
    """
    # Try cache first
    try:
        from app.infra.redis_client import get_redis
        redis = get_redis()
        cached = await redis.get(PROSIGHT_SNAPSHOT_CACHE_KEY)
        if cached:
            log.debug("Prosight snapshot cache HIT")
            return json.loads(cached)
    except Exception as e:
        log.warning("Redis cache read failed for prosight snapshot: %s", e)

    # Cache miss - query DB
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        result = await session.scalar(
            select(ProsightSnapshot)
            .order_by(ProsightSnapshot.snapshot_date.desc())
            .limit(1)
        )
        if result:
            snapshot = {
                "snapshot_date": result.snapshot_date.isoformat(),
                "model_version": result.model_version,
                "total_series": result.total_series,
                "qualified_flagged": result.qualified_flagged,
                "data": result.data,
                "created_at": result.created_at.isoformat() if result.created_at else None,
            }
            # Cache the result
            try:
                from app.infra.redis_client import get_redis
                redis = get_redis()
                await redis.set(
                    PROSIGHT_SNAPSHOT_CACHE_KEY,
                    json.dumps(snapshot),
                    ex=PROSIGHT_SNAPSHOT_CACHE_TTL
                )
                log.debug("Prosight snapshot cache SET (TTL=%ds)", PROSIGHT_SNAPSHOT_CACHE_TTL)
            except Exception as e:
                log.warning("Redis cache write failed for prosight snapshot: %s", e)
            return snapshot
        return None
    finally:
        if own_session:
            await session.close()


async def get_snapshot_by_date(
    snapshot_date: date, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """Get a specific date's Prosight dashboard snapshot."""
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        result = await session.scalar(
            select(ProsightSnapshot).where(ProsightSnapshot.snapshot_date == snapshot_date)
        )
        if result:
            return {
                "snapshot_date": result.snapshot_date.isoformat(),
                "model_version": result.model_version,
                "total_series": result.total_series,
                "qualified_flagged": result.qualified_flagged,
                "data": result.data,
                "created_at": result.created_at.isoformat() if result.created_at else None,
            }
        return None
    finally:
        if own_session:
            await session.close()


async def invalidate_snapshot_cache() -> None:
    """Invalidate the prosight snapshot cache."""
    try:
        from app.infra.redis_client import get_redis
        redis = get_redis()
        await redis.delete(PROSIGHT_SNAPSHOT_CACHE_KEY)
        log.debug("Prosight snapshot cache invalidated")
    except Exception as e:
        log.warning("Failed to invalidate prosight snapshot cache: %s", e)


async def upsert_snapshot(
    snapshot_date: date,
    data: dict[str, Any],
    model_version: str | None = None,
    total_series: int | None = None,
    qualified_flagged: int | None = None,
    session: AsyncSession | None = None,
) -> str:
    """Insert or update a Prosight snapshot for a given date.

    Returns the snapshot ID.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Use PostgreSQL upsert (INSERT ... ON CONFLICT DO UPDATE)
        stmt = insert(ProsightSnapshot).values(
            snapshot_date=snapshot_date,
            data=data,
            model_version=model_version,
            total_series=total_series,
            qualified_flagged=qualified_flagged,
            updated_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_prosight_snapshots_date",
            set_={
                "data": stmt.excluded.data,
                "model_version": stmt.excluded.model_version,
                "total_series": stmt.excluded.total_series,
                "qualified_flagged": stmt.excluded.qualified_flagged,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        stmt = stmt.returning(ProsightSnapshot.id)
        result = await session.execute(stmt)
        snapshot_id = result.scalar_one()
        await session.commit()

        # Invalidate cache after successful upsert
        await invalidate_snapshot_cache()

        log.info("Upserted Prosight snapshot for %s (id=%s)", snapshot_date, snapshot_id)
        return str(snapshot_id)
    finally:
        if own_session:
            await session.close()


async def list_snapshots(
    limit: int = 30, session: AsyncSession | None = None
) -> list[dict[str, Any]]:
    """List recent Prosight snapshots (metadata only, no full data)."""
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        result = await session.scalars(
            select(ProsightSnapshot)
            .order_by(ProsightSnapshot.snapshot_date.desc())
            .limit(limit)
        )
        snapshots = result.all()
        return [
            {
                "id": str(s.id),
                "snapshot_date": s.snapshot_date.isoformat(),
                "model_version": s.model_version,
                "total_series": s.total_series,
                "qualified_flagged": s.qualified_flagged,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in snapshots
        ]
    finally:
        if own_session:
            await session.close()


def is_databricks_configured() -> bool:
    """Check if Databricks credentials are configured."""
    return bool(
        settings.prosight_databricks_host
        and settings.prosight_databricks_token
        and settings.prosight_databricks_warehouse_id
    )


# ─────────────────────────────────────────────────────────────────────────────
# Actionables
# ─────────────────────────────────────────────────────────────────────────────


def compute_action_hash(
    action_id: str | None,
    bu: str = "",
    as_of_date: str = "",
    segment: str = "",
    lens: str = "",
    action: str = "",
) -> str:
    """Stable identity for an actionable — survives re-ranking across syncs.

    Prefers the upstream action_id (unique per BU/day/action); falls back to a
    composite of the content fields when action_id is missing.
    """
    key = action_id or f"{bu}|{as_of_date}|{segment}|{lens}|{action}"
    return hashlib.sha256(key.encode()).hexdigest()


def _is_marker(action_id: str, rank: int | None, action: str) -> bool:
    """Upstream 'No actionable today.' rows — processed day, nothing to act on."""
    if "::no_actionable::" in action_id:
        return True
    return rank == 0 and action.lower().startswith("no actionable")


def _actionable_to_dict(a: ProsightActionable) -> dict[str, Any]:
    return {
        "action_hash": a.action_hash,
        "as_of_date": a.as_of_date.isoformat() if a.as_of_date else None,
        "bu": a.bu,
        "segment": a.segment,
        "lens": a.lens,
        "rank": a.rank,
        "action": a.action,
        # Componentized display fields
        "segment_label": a.segment_label,
        "dimension": a.dimension,
        "impact_display": a.impact_display,
        "fact": a.fact,
        "l2_pocket": a.l2_pocket,
        "why": a.why,
        "lever": a.lever,
        "news_summary": a.news_summary,
        "news_source": a.news_source,
        "news_url": a.news_url,
        "news_relation": a.news_relation,
        "run_days": a.run_days,
        "wow_pct": a.wow_pct,
        "dod_pct": a.dod_pct,
        "daily_order_gap": a.daily_order_gap,
        "l2_gap_orders": a.l2_gap_orders,
        "l2_share_pct": a.l2_share_pct,
        "impact_inr_1d": a.impact_inr_1d,
        "impact_inr_3d": a.impact_inr_3d,
        # Feedback
        "is_actionable": a.is_actionable,
        "days_saved": a.days_saved,
        "feedback_updated_by": a.feedback_updated_by,
        "feedback_updated_at": (
            a.feedback_updated_at.isoformat() if a.feedback_updated_at else None
        ),
        "synced_at": a.synced_at.isoformat() if a.synced_at else None,
    }


def _to_str(val: Any) -> str | None:
    """Trimmed string or None — Databricks returns NULLs as None already."""
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _to_float(val: Any) -> float | None:
    """Finite float or None. NaN/±inf are treated as malformed cells, not values.

    `float("nan")` parses fine, so without the finiteness check a NaN would flow
    on to break two things downstream:

    * `_to_int` — `int(nan)` raises ValueError and `int(inf)` raises
      OverflowError, and neither is caught anywhere in `sync_actionables`, so a
      single bad cell aborts the **entire** batch rather than blanking one field.
    * the API response — `wow_pct` and friends are handed to the client verbatim
      by `_actionable_to_dict`, and `json.dumps` emits bare `NaN`/`Infinity`,
      which is not valid JSON and makes the browser's `JSON.parse` throw.

    Note `float("nan")` also accepts the *strings* `"nan"`/`"inf"`, so this is
    reachable from a text column, not only from a float one.
    """
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def _to_int(val: Any) -> int | None:
    f = _to_float(val)
    return int(f) if f is not None else None


def _parse_date(val: Any) -> date | None:
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val.strip())
        except ValueError:
            return None
    return None


def _normalize_row(row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Coerce one Databricks row into an upsertable record, or None to skip it.

    Every value is passed through the `_to_*` coercions, so a malformed cell
    becomes None rather than failing the batch. A row with no `action` text is
    skipped: its hash would identify nothing and there is nothing to render.
    """
    segment = str(row.get("segment") or "").strip()
    lens = str(row.get("lens") or "").strip()
    action = str(row.get("action") or "").strip()
    action_id = str(row.get("action_id") or "").strip()
    bu = str(row.get("bu") or "").strip().lower()
    as_of_date = _parse_date(row.get("as_of_date"))
    if not action:
        log.warning("Skipping actionable with empty action: %r", row)
        return None
    rank = _to_int(row.get("rank"))
    return {
        "as_of_date": as_of_date,
        "bu": bu,
        "segment": segment,
        "lens": lens,
        "rank": rank,
        "action": action,
        "action_id": action_id or None,
        "action_hash": compute_action_hash(
            action_id or None,
            bu,
            as_of_date.isoformat() if as_of_date else "",
            segment,
            lens,
            action,
        ),
        "is_marker": _is_marker(action_id, rank, action),
        "is_current": True,
        "synced_at": now,
        # Componentized display fields
        "model_version": _to_str(row.get("model_version")),
        "segment_label": _to_str(row.get("segment_label")),
        "dimension": _to_str(row.get("dimension")),
        "impact_display": _to_str(row.get("impact_display")),
        "fact": _to_str(row.get("fact")),
        "l2_pocket": _to_str(row.get("l2_pocket")),
        "why": _to_str(row.get("why")),
        "lever": _to_str(row.get("lever")),
        "news_summary": _to_str(row.get("news_summary")),
        "news_source": _to_str(row.get("news_source")),
        "news_url": _to_str(row.get("news_url")),
        "news_relation": _to_str(row.get("news_relation")),
        "day_summary": _to_str(row.get("day_summary")),
        "run_days": _to_int(row.get("run_days")),
        "wow_pct": _to_float(row.get("wow_pct")),
        "dod_pct": _to_float(row.get("dod_pct")),
        "daily_order_gap": _to_float(row.get("daily_order_gap")),
        "l2_gap_orders": _to_float(row.get("l2_gap_orders")),
        "l2_share_pct": _to_float(row.get("l2_share_pct")),
        "impact_inr_1d": _to_float(row.get("impact_inr_1d")),
        "impact_inr_3d": _to_float(row.get("impact_inr_3d")),
    }


def _normalize_rows(
    rows: list[dict[str, Any]], now: datetime
) -> dict[str, dict[str, Any]]:
    """Normalize a sync batch and de-dup it on action_hash.

    Duplicates keep the best (lowest) rank. De-duping here is what lets the
    bulk upsert run at all: two rows sharing a hash in one statement would
    conflict with each other, which ON CONFLICT cannot resolve.

    `now` is threaded in rather than read per row so every record in a batch
    carries the identical synced_at — `_demote_stale` relies on that to tell
    this run's rows from the previous run's.
    """
    by_hash: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = _normalize_row(row, now)
        if record is None:
            continue
        key = record["action_hash"]
        existing = by_hash.get(key)
        if existing is None or (
            record["rank"] is not None
            and (existing["rank"] is None or record["rank"] < existing["rank"])
        ):
            by_hash[key] = record

    if len(by_hash) < len(rows):
        log.info("Actionables sync: de-duped %d rows to %d", len(rows), len(by_hash))
    return by_hash


async def _chunked_upsert(
    session: AsyncSession, values: list[dict[str, Any]]
) -> None:
    """Upsert records on action_hash, refreshing every synced column.

    The feedback columns are absent from `values` and from the conflict update,
    so a sync can never clobber what the API wrote.

    One statement per chunk: asyncpg binds parameters through the Postgres
    extended-query protocol, whose Bind message counts them in an int16
    (32767 max). At ~32 columns a row, a single statement would cap out around
    a thousand rows and fail the whole sync.
    """
    if not values:
        return
    synced_cols = [c for c in values[0] if c != "action_hash"]
    for start in range(0, len(values), _ACTIONABLE_UPSERT_CHUNK):
        chunk = values[start : start + _ACTIONABLE_UPSERT_CHUNK]
        stmt = insert(ProsightActionable).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_prosight_actionables_hash",
            set_={c: getattr(stmt.excluded, c) for c in synced_cols},
        )
        await session.execute(stmt)


async def _demote_stale(
    session: AsyncSession, now: datetime, window_start: date | None
) -> None:
    """Flag rows this run didn't refresh as no longer current.

    Every row this run upserted carries synced_at=now, so anything older was
    absent from the batch. Must run after the upsert: keying off what actually
    landed is what makes the destructive statement safe, rather than trusting
    statement order.

    Scoped to `window_start` because rows older than the fetched window were
    never requested — their absence means "outside the window", not "stale".
    None demotes across all dates, correct only for a full-table fetch.
    """
    demote = update(ProsightActionable).where(
        ProsightActionable.synced_at < now,
        ProsightActionable.is_current.is_(True),
    )
    if window_start is not None:
        demote = demote.where(ProsightActionable.as_of_date >= window_start)
    await session.execute(demote.values(is_current=False))


async def sync_actionables(
    rows: list[dict[str, Any]],
    session: AsyncSession | None = None,
    window_start: date | None = None,
) -> int:
    """Store the actionables set from Databricks (all BUs, one date window).

    Upserts on action_hash without touching the feedback columns; rows absent
    from this sync are flagged is_current=False (kept for feedback history).
    An empty `rows` is a no-op — the previously synced set stays current.

    `window_start` is the first as_of_date the caller fetched. The demote is
    scoped to it, because rows older than the window were never requested and
    their absence means "outside the window", not "stale". Passing None demotes
    across all dates, which is only correct for a full-table fetch.

    Returns the number of current rows (markers included).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    now = datetime.now(timezone.utc)
    by_hash = _normalize_rows(rows, now)

    try:
        if not by_hash:
            # Nothing to sync. Upstream writes an explicit "No actionable today."
            # marker for processed-but-empty days, so a genuinely empty fetch means
            # something broke upstream (source table mid-rewrite, table rename,
            # permission change) — never a real "nothing anywhere" day. Demoting
            # here would flip every row to is_current=False with no re-insert,
            # leaving every BU reading "not_processed". Leave the live set alone.
            log.warning("Actionables sync returned 0 rows — keeping the existing set")
            return 0

        await _chunked_upsert(session, list(by_hash.values()))
        await _demote_stale(session, now, window_start)

        if own_session:
            await session.commit()
        log.info("Synced %d Prosight actionables", len(by_hash))
        return len(by_hash)
    finally:
        if own_session:
            await session.close()


async def list_actionables(
    bu: str | None = None,
    on_date: date | None = None,
    session: AsyncSession | None = None,
    limit: int = DEFAULT_ACTIONABLES_LIMIT,
) -> dict[str, Any]:
    """List current actionables for a BU/day, with a processing status.

    status:
      - "ok"             → actionable rows returned
      - "no_actionables" → day was processed, upstream marker says nothing to act on
      - "not_processed"  → no rows at all for this BU/day

    `limit` caps the rows read. Without it an unfiltered call returns every
    current actionable across every synced day in one response. Newest days come
    first, so a truncated result drops the oldest; `truncated` says when that
    happened.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    limit = max(1, min(limit, MAX_ACTIONABLES_LIMIT))

    try:
        query = select(ProsightActionable).where(ProsightActionable.is_current.is_(True))
        if bu:
            query = query.where(ProsightActionable.bu == bu.strip().lower())
        if on_date:
            query = query.where(ProsightActionable.as_of_date == on_date)
        # as_of_date/bu lead so that a capped unfiltered call keeps whole recent
        # days rather than an arbitrary mix; within one (date, bu) slice — the
        # only shape the UI asks for — this is still rank, then segment.
        query = query.order_by(
            ProsightActionable.as_of_date.desc().nulls_last(),
            ProsightActionable.bu,
            ProsightActionable.rank.asc().nulls_last(),
            ProsightActionable.segment,
        )
        # One extra row distinguishes "exactly full" from "truncated".
        rows = list((await session.scalars(query.limit(limit + 1))).all())
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
            log.warning(
                "list_actionables truncated at %d rows (bu=%s, date=%s)", limit, bu, on_date
            )

        real = [a for a in rows if not a.is_marker]
        if real:
            status = "ok"
        elif rows:
            status = "no_actionables"
        else:
            status = "not_processed"

        # day_summary is identical on every row of a (date, bu) slice, marker
        # included — the model's plain-English read of the whole day.
        day_summary = next((a.day_summary for a in rows if a.day_summary), None)

        return {
            "actionables": [_actionable_to_dict(a) for a in real],
            "status": status,
            "day_summary": day_summary,
            "synced_at": rows[0].synced_at.isoformat() if rows else None,
            "limit": limit,
            "truncated": truncated,
        }
    finally:
        if own_session:
            await session.close()


async def update_actionable_feedback(
    action_hash: str,
    is_actionable: bool | None,
    days_saved: float | None,
    user_email: str | None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Record user feedback on a current actionable.

    Returns the updated row dict, or None if the hash doesn't match a current
    actionable (e.g. the list changed under the client after a re-sync).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        actionable = await session.scalar(
            select(ProsightActionable).where(
                ProsightActionable.action_hash == action_hash,
                ProsightActionable.is_current.is_(True),
            )
        )
        if not actionable:
            return None

        actionable.is_actionable = is_actionable
        actionable.days_saved = days_saved
        actionable.feedback_updated_by = user_email
        actionable.feedback_updated_at = datetime.now(timezone.utc)
        await session.commit()
        return _actionable_to_dict(actionable)
    finally:
        if own_session:
            await session.close()
