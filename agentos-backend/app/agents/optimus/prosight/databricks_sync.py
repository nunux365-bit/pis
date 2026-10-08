"""Databricks SQL connector for Prosight data sync."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.agents.optimus.prosight.service import (
    is_databricks_configured,
    sync_actionables,
    upsert_snapshot,
)
from app.config.settings import settings
from app.infra.row_claim import PROSIGHT_ACTIONABLES_CLAIM, claimed_session

log = logging.getLogger(__name__)

# Databricks SQL API endpoints
DATABRICKS_SQL_API = "/api/2.0/sql/statements"

# Default Prosight dashboard table, used when PROSIGHT_DATABRICKS_TABLE is unset.
DEFAULT_PROSIGHT_TABLE = "data_warehouse.ai_transformation.prosight_dashboard"

# The table name is interpolated into SQL, so it is restricted to a
# fully-qualified identifier (catalog.schema.table) — letters, digits and
# underscores only — to prevent SQL injection via the environment variable.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$")


class DatabricksSyncError(Exception):
    """Error during Databricks sync."""


def _resolve_table() -> str:
    """Return the configured source table, validated against SQL-injection."""
    table = (settings.prosight_databricks_table or "").strip() or DEFAULT_PROSIGHT_TABLE
    if not _TABLE_NAME_RE.match(table):
        raise DatabricksSyncError(
            f"Invalid PROSIGHT_DATABRICKS_TABLE {table!r}: expected catalog.schema.table."
        )
    return table


def _resolve_actionables_table() -> str:
    """Return the configured actionables table, validated against SQL-injection."""
    table = (settings.prosight_actionables_table or "").strip()
    if not table:
        raise DatabricksSyncError("PROSIGHT_ACTIONABLES_TABLE is not set.")
    if not _TABLE_NAME_RE.match(table):
        raise DatabricksSyncError(
            f"Invalid PROSIGHT_ACTIONABLES_TABLE {table!r}: expected catalog.schema.table."
        )
    return table


async def _execute_databricks_query(query: str) -> dict:
    """Execute a SQL statement on the configured warehouse and return the completed result."""
    if not is_databricks_configured():
        raise DatabricksSyncError("Databricks not configured. Set PROSIGHT_DATABRICKS_* env vars.")

    host = settings.prosight_databricks_host.rstrip("/")
    token = settings.prosight_databricks_token
    warehouse_id = settings.prosight_databricks_warehouse_id

    url = f"https://{host}{DATABRICKS_SQL_API}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # Databricks caps wait_timeout at 50s (or 0 to disable); longer configured
    # timeouts are still honored via polling in _poll_statement_completion.
    wait_timeout_seconds = max(5, min(settings.prosight_databricks_query_timeout_seconds, 50))
    payload = {
        "warehouse_id": warehouse_id,
        "statement": query,
        "wait_timeout": f"{wait_timeout_seconds}s",
        # The Prosight dashboard `data` blob is a single row that can exceed the
        # 25 MiB cap for disposition=INLINE. EXTERNAL_LINKS serves the result as
        # presigned chunk URLs (stitched back into data_array by
        # _materialize_result), so the payload size is unbounded.
        "disposition": "EXTERNAL_LINKS",
        "format": "JSON_ARRAY",
    }

    # Add 10 s buffer above Databricks wait_timeout so the httpx client
    # doesn't race the API response.
    _http_timeout = float(settings.prosight_databricks_query_timeout_seconds) + 10.0
    async with httpx.AsyncClient(timeout=_http_timeout) as client:
        try:
            # Execute SQL statement
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()

            status = result.get("status", {}).get("state")
            if status == "FAILED":
                error = result.get("status", {}).get("error", {})
                raise DatabricksSyncError(f"Query failed: {error.get('message', 'Unknown error')}")

            if status in ("PENDING", "RUNNING"):
                # Need to poll for completion
                statement_id = result.get("statement_id")
                result = await _poll_statement_completion(client, host, headers, statement_id)

            # With disposition=EXTERNAL_LINKS the rows are not inline; fetch the
            # presigned chunk(s) and stitch them into result["result"]["data_array"]
            # so downstream extractors read the result exactly as before.
            result = await _materialize_result(client, host, headers, result)
            return result

        except httpx.HTTPStatusError as e:
            log.error("Databricks API error: %s", e.response.text)
            raise DatabricksSyncError(f"Databricks API error: {e.response.status_code}") from e
        except DatabricksSyncError:
            raise
        except Exception as e:
            log.error("Failed to fetch from Databricks: %s", e)
            raise DatabricksSyncError(str(e)) from e


async def fetch_prosight_data_from_databricks() -> dict[str, Any]:
    """Fetch Prosight dashboard data from Databricks SQL Warehouse.

    Returns the dashboard JSON payload.
    Raises DatabricksSyncError if fetch fails.
    """
    # Query to fetch the latest prosight data as JSON.
    # Assumes the table has a 'data' column with the full dashboard JSON
    # and a 'snapshot_date' column.
    table = _resolve_table()
    query = f"""
    SELECT snapshot_date, data, model_version, total_series, qualified_flagged
    FROM {table}
    ORDER BY snapshot_date DESC
    LIMIT 1
    """

    result = await _execute_databricks_query(query)
    return _extract_dashboard_from_result(result)


def actionables_window_start(today: date | None = None) -> date:
    """First as_of_date the actionables sync fetches (inclusive).

    The sync reads a rolling window rather than the full table. Callers that
    demote stale rows must scope the demote to this same window — rows older
    than it were never fetched, so their absence is not staleness.
    """
    days = settings.prosight_actionables_lookback_days
    if not isinstance(days, int) or days < 1:
        raise DatabricksSyncError(
            f"Invalid PROSIGHT_ACTIONABLES_LOOKBACK_DAYS {days!r}: expected a positive integer."
        )
    return (today or date.today()) - timedelta(days=days - 1)


def _resolve_max_rows() -> int:
    """Return the validated row cap. Interpolated into SQL, so it must be a plain int."""
    max_rows = settings.prosight_actionables_max_rows
    if not isinstance(max_rows, int) or max_rows < 1:
        raise DatabricksSyncError(
            f"Invalid PROSIGHT_ACTIONABLES_MAX_ROWS {max_rows!r}: expected a positive integer."
        )
    return max_rows


async def fetch_actionables_from_databricks() -> list[dict[str, Any]]:
    """Fetch actionables from Databricks — all BUs, for the rolling date window.

    Bounded by `actionables_window_start()`: the source keeps one partition per
    BU per day indefinitely, so an unbounded read grows past the query timeout
    and the upsert's parameter budget. The as_of_date predicate also lets the
    warehouse prune before the window function runs. LIMIT is a backstop only —
    hitting it is logged as an error.

    Old model versions coexist in the source table; QUALIFY keeps only the
    latest batch (max created_at) per (as_of_date, bu). Includes upstream
    "No actionable today." marker rows (used to distinguish processed-but-empty
    days from unprocessed ones). An empty list is valid.
    Raises DatabricksSyncError if fetch fails.
    """
    table = _resolve_actionables_table()
    max_rows = _resolve_max_rows()
    window_start = actionables_window_start()
    query = f"""
    SELECT as_of_date, bu, segment, lens, rank, action, action_id, model_version,
           segment_label, dimension, impact_display, fact, l2_pocket, why, lever,
           news_summary, news_source, news_url, news_relation, day_summary,
           run_days, wow_pct, dod_pct, daily_order_gap, l2_gap_orders,
           l2_share_pct, impact_inr_1d, impact_inr_3d
    FROM {table}
    WHERE as_of_date >= DATE '{window_start.isoformat()}'
    QUALIFY created_at = MAX(created_at) OVER (PARTITION BY as_of_date, bu)
    ORDER BY as_of_date DESC, bu, rank ASC
    LIMIT {max_rows}
    """

    result = await _execute_databricks_query(query)

    manifest = result.get("manifest", {})
    columns = [col["name"] for col in manifest.get("schema", {}).get("columns", [])]
    data_array = result.get("result", {}).get("data_array", []) or []

    rows = [dict(zip(columns, row)) for row in data_array]
    if len(rows) >= max_rows:
        # Silently truncating would demote whatever fell off the end, so make it loud.
        log.error(
            "Actionables fetch hit the %d-row cap (window from %s) — result is truncated",
            max_rows,
            window_start,
        )
    return rows


async def _poll_statement_completion(
    client: httpx.AsyncClient,
    host: str,
    headers: dict,
    statement_id: str,
    max_attempts: int = 30,
) -> dict:
    """Poll Databricks SQL statement until completion."""
    import asyncio

    url = f"https://{host}{DATABRICKS_SQL_API}/{statement_id}"

    for _ in range(max_attempts):
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        result = response.json()

        status = result.get("status", {}).get("state")
        if status == "SUCCEEDED":
            return result
        if status == "FAILED":
            error = result.get("status", {}).get("error", {})
            raise DatabricksSyncError(f"Query failed: {error.get('message', 'Unknown error')}")

        await asyncio.sleep(1)

    raise DatabricksSyncError("Query timed out waiting for completion")


def _resolve_materialize_limits() -> tuple[int, int, float]:
    """Return validated (max_chunks, max_bytes, deadline_seconds) for chunk paging."""
    max_chunks = settings.prosight_databricks_max_result_chunks
    max_mb = settings.prosight_databricks_max_result_mb
    timeout_seconds = settings.prosight_databricks_materialize_timeout_seconds
    for name, value in (
        ("PROSIGHT_DATABRICKS_MAX_RESULT_CHUNKS", max_chunks),
        ("PROSIGHT_DATABRICKS_MAX_RESULT_MB", max_mb),
        ("PROSIGHT_DATABRICKS_MATERIALIZE_TIMEOUT_SECONDS", timeout_seconds),
    ):
        if not isinstance(value, int) or value < 1:
            raise DatabricksSyncError(f"Invalid {name} {value!r}: expected a positive integer.")
    return max_chunks, max_mb * 1024 * 1024, float(timeout_seconds)


async def _materialize_result(
    client: httpx.AsyncClient,
    host: str,
    headers: dict,
    result: dict,
) -> dict:
    """Collect EXTERNAL_LINKS chunks into result["result"]["data_array"].

    With disposition=EXTERNAL_LINKS, row data is served as one or more presigned
    URLs (chunks) rather than inline. Each JSON_ARRAY chunk is an array of rows;
    fetch every chunk and concatenate the rows so callers can keep reading
    result["result"]["data_array"] unchanged. If the result is already inline
    (no external_links), it is returned as-is.

    Databricks — not this client — decides how many chunks a result is split
    into, so the walk is bounded by size, chunk count and wall-clock. Exceeding
    any of them raises: the rows accumulate in memory with no backpressure, and
    returning a partial data_array would read downstream as a complete result.
    """
    res = result.get("result") or {}
    if res.get("external_links") is None:
        # Inline disposition or an empty result — nothing to materialize.
        return result

    max_chunks, max_bytes, deadline_seconds = _resolve_materialize_limits()

    # The manifest sizes the whole result up front, so an oversized query fails
    # before a single chunk is downloaded rather than part-way through paging.
    manifest = result.get("manifest") or {}
    total_bytes = manifest.get("total_byte_count")
    if isinstance(total_bytes, int) and total_bytes > max_bytes:
        raise DatabricksSyncError(
            f"Result is {total_bytes / 1024 / 1024:.0f} MB, over the "
            f"{max_bytes // 1024 // 1024} MB cap (PROSIGHT_DATABRICKS_MAX_RESULT_MB)."
        )
    total_chunks = manifest.get("total_chunk_count")
    if isinstance(total_chunks, int) and total_chunks > max_chunks:
        raise DatabricksSyncError(
            f"Result spans {total_chunks} chunks, over the {max_chunks}-chunk cap "
            "(PROSIGHT_DATABRICKS_MAX_RESULT_CHUNKS)."
        )

    statement_id = result.get("statement_id")
    rows: list = []
    seen: set[int] = set()
    pending = list(res.get("external_links") or [])
    fetched_chunks = 0
    fetched_bytes = 0
    deadline = time.monotonic() + deadline_seconds

    while pending:
        # Re-checked each iteration: the manifest is a hint, and next_chunk_index
        # can keep pointing forward past whatever it advertised.
        if time.monotonic() > deadline:
            raise DatabricksSyncError(
                f"Timed out after {deadline_seconds:.0f}s materializing result "
                f"({fetched_chunks} chunks, {fetched_bytes / 1024 / 1024:.0f} MB read)."
            )

        link = pending.pop(0)
        idx = link.get("chunk_index")
        if idx in seen:
            continue
        url = link.get("external_link")
        if url:
            if fetched_chunks >= max_chunks:
                raise DatabricksSyncError(
                    f"Result exceeded the {max_chunks}-chunk cap "
                    "(PROSIGHT_DATABRICKS_MAX_RESULT_CHUNKS)."
                )
            # Presigned cloud-storage URL — send NO auth header, and allow a
            # generous timeout since a chunk can be tens of MB.
            chunk_resp = await client.get(url, timeout=120.0)
            chunk_resp.raise_for_status()
            fetched_chunks += 1
            # Checked on the raw bytes, before json() expands them several-fold.
            fetched_bytes += len(chunk_resp.content)
            if fetched_bytes > max_bytes:
                raise DatabricksSyncError(
                    f"Result exceeded the {max_bytes // 1024 // 1024} MB cap "
                    f"(PROSIGHT_DATABRICKS_MAX_RESULT_MB) after {fetched_chunks} chunks."
                )
            rows.extend(chunk_resp.json() or [])
        if idx is not None:
            seen.add(idx)
        nxt = link.get("next_chunk_index")
        if nxt is not None and nxt not in seen:
            chunk_url = f"https://{host}{DATABRICKS_SQL_API}/{statement_id}/result/chunks/{nxt}"
            meta = await client.get(chunk_url, headers=headers)
            meta.raise_for_status()
            pending.extend(meta.json().get("external_links") or [])

    log.debug(
        "Materialized %d rows from %d chunks (%.1f MB)",
        len(rows),
        fetched_chunks,
        fetched_bytes / 1024 / 1024,
    )
    result.setdefault("result", {})["data_array"] = rows
    return result


def _extract_dashboard_from_result(result: dict) -> dict[str, Any]:
    """Extract dashboard data from Databricks SQL result."""
    manifest = result.get("manifest", {})
    data_array = result.get("result", {}).get("data_array", [])

    if not data_array:
        raise DatabricksSyncError("No data returned from Databricks")

    # Get column names from manifest
    columns = [col["name"] for col in manifest.get("schema", {}).get("columns", [])]

    # First row of results
    row = data_array[0]
    row_dict = dict(zip(columns, row))

    # Parse the data column (it might be a JSON string)
    data = row_dict.get("data")
    if isinstance(data, str):
        data = json.loads(data)

    return {
        "snapshot_date": row_dict.get("snapshot_date"),
        "data": data,
        "model_version": row_dict.get("model_version"),
        "total_series": row_dict.get("total_series"),
        "qualified_flagged": row_dict.get("qualified_flagged"),
    }


async def sync_prosight_from_databricks() -> dict[str, Any]:
    """Fetch latest data from Databricks and store in PostgreSQL.

    Dashboard upsert is unique on snapshot_date (last-write-wins). Actionables
    demote is claimed with FOR UPDATE SKIP LOCKED on ``prosight:actionables``.
    Databricks fetches stay unlocked. A busy claim skips the actionables write
    rather than waiting.

    Returns status dict with sync result: "success" or "error".
    """
    log.info("Starting Prosight sync from Databricks")

    try:
        # Fetch from Databricks
        result = await fetch_prosight_data_from_databricks()

        # Parse snapshot date
        snapshot_date_str = result.get("snapshot_date")
        if isinstance(snapshot_date_str, str):
            snapshot_date = date.fromisoformat(snapshot_date_str)
        elif isinstance(snapshot_date_str, date):
            snapshot_date = snapshot_date_str
        else:
            snapshot_date = date.today()

        # Extract summary stats from data
        data = result.get("data", {})
        summary = data.get("summary", {})

        # Helper to safely convert to int (Databricks may return strings)
        def to_int(val: Any) -> int | None:
            if val is None:
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                return None

        # Get values with fallback to summary
        total_series_raw = result.get("total_series") or summary.get("total_series")
        qualified_flagged_raw = result.get("qualified_flagged") or summary.get("qualified_flagged")

        # Upsert to PostgreSQL
        snapshot_id = await upsert_snapshot(
            snapshot_date=snapshot_date,
            data=data,
            model_version=result.get("model_version") or summary.get("model_version"),
            total_series=to_int(total_series_raw),
            qualified_flagged=to_int(qualified_flagged_raw),
        )

        log.info("Prosight sync completed: snapshot_id=%s, date=%s", snapshot_id, snapshot_date)

        result_status: dict[str, Any] = {
            "status": "success",
            "snapshot_id": snapshot_id,
            "snapshot_date": snapshot_date.isoformat(),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        # Actionables sync is additive — a failure here must not fail the
        # dashboard sync that already succeeded.
        try:
            window_start = actionables_window_start()
            actionable_rows = await fetch_actionables_from_databricks()
            if not actionable_rows:
                # Zero rows is a successful query returning nothing, so it would
                # otherwise be reported as a clean sync. Upstream marks every
                # processed day explicitly, so an empty window means the source is
                # broken (or the lookback is misconfigured) — surface it.
                log.error(
                    "Databricks returned 0 actionables since %s — live set left unchanged",
                    window_start,
                )
                result_status["actionables_error"] = (
                    f"Databricks returned 0 actionables since {window_start.isoformat()}; "
                    "live set left unchanged"
                )
            else:
                async with claimed_session(PROSIGHT_ACTIONABLES_CLAIM) as session:
                    if session is None:
                        log.warning(
                            "Prosight actionables write already in progress — skipping demote"
                        )
                        result_status["actionables_error"] = (
                            "another Prosight actionables write is in progress"
                        )
                    else:
                        result_status["actionables_synced"] = await sync_actionables(
                            actionable_rows,
                            session=session,
                            window_start=window_start,
                        )
        except DatabricksSyncError as e:
            log.error("Actionables sync failed: %s", e)
            result_status["actionables_error"] = str(e)
        except Exception as e:
            log.exception("Unexpected error during actionables sync")
            result_status["actionables_error"] = f"Unexpected error: {e}"

        return result_status

    except DatabricksSyncError as e:
        log.error("Prosight sync failed: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.exception("Unexpected error during Prosight sync")
        return {
            "status": "error",
            "error": f"Unexpected error: {e}",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
