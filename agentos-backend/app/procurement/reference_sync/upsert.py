"""Upsert :class:`ReferenceRow` into ``pr_po_reference_values``."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.reference_domains import FIXED_MASTER_DOMAINS
from app.procurement.reference_sync.constants import UPSERT_COMMIT_EVERY
from app.procurement.reference_sync.rows import ReferenceRow, dedupe_reference_rows

log = logging.getLogger(__name__)

_PRUNE_KEY_INSERT_CHUNK = 2000


def _row_payload(row: ReferenceRow) -> dict[str, Any] | None:
    if not row.code or not row.domain:
        return None
    return {
        "id": uuid.uuid4(),
        "domain": row.domain,
        "document_type": row.document_type or "",
        "applies_to_kind": row.applies_to_kind or "",
        "code": row.code,
        "label": row.label or row.code,
        "sort_order": row.sort_order,
        "extra": row.extra,
    }


async def upsert_reference_rows(
    session: AsyncSession,
    rows: list[ReferenceRow],
    *,
    merge_extra_on_update: bool = False,
    skip_domains: frozenset[str] | None = FIXED_MASTER_DOMAINS,
) -> tuple[int, int]:
    """Bulk upsert via Postgres ``ON CONFLICT``. Return ``(inserted, updated)``."""
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if skip_domains and row.domain in skip_domains:
            continue
        payload = _row_payload(row)
        if payload is None:
            continue
        payloads.append(payload)
    if not payloads:
        return 0, 0

    stmt = pg_insert(PrPoReferenceValue).values(payloads)
    excluded = stmt.excluded
    if merge_extra_on_update:
        # Preserve existing JSON keys; overlay only provided ones (matches ORM merge_extra).
        extra_expr = text(
            "CASE"
            " WHEN EXCLUDED.extra IS NULL THEN pr_po_reference_values.extra"
            " ELSE coalesce(pr_po_reference_values.extra, '{}'::jsonb)"
            "      || coalesce(EXCLUDED.extra, '{}'::jsonb)"
            " END"
        )
    else:
        extra_expr = text(
            "CASE"
            " WHEN EXCLUDED.extra IS NULL THEN pr_po_reference_values.extra"
            " ELSE EXCLUDED.extra"
            " END"
        )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_pr_po_ref_domain_type_code_kind",
        set_={
            "label": excluded.label,
            "sort_order": excluded.sort_order,
            "extra": extra_expr,
        },
    ).returning(literal_column("(xmax = 0)").label("was_inserted"))

    result = await session.execute(stmt)
    flags = [bool(row[0]) for row in result.all()]
    inserted = sum(1 for f in flags if f)
    updated = len(flags) - inserted
    return inserted, updated


async def upsert_reference_rows_batched(
    session: AsyncSession,
    rows: list[ReferenceRow],
    *,
    merge_extra_on_update: bool = False,
    skip_domains: frozenset[str] | None = FIXED_MASTER_DOMAINS,
    commit_every: int = UPSERT_COMMIT_EVERY,
    commit: bool = True,
) -> tuple[int, int]:
    rows = dedupe_reference_rows(rows)
    total_in = 0
    total_up = 0
    batch: list[ReferenceRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= commit_every:
            ins, upd = await upsert_reference_rows(
                session,
                batch,
                merge_extra_on_update=merge_extra_on_update,
                skip_domains=skip_domains,
            )
            if commit:
                await session.commit()
            total_in += ins
            total_up += upd
            log.info(
                "reference_sync upsert progress: +%s in / +%s up (running total in=%s up=%s)",
                ins,
                upd,
                total_in,
                total_up,
            )
            batch = []
    if batch:
        ins, upd = await upsert_reference_rows(
            session,
            batch,
            merge_extra_on_update=merge_extra_on_update,
            skip_domains=skip_domains,
        )
        if commit:
            await session.commit()
        total_in += ins
        total_up += upd
    return total_in, total_up


def _reference_row_keys(rows: list[ReferenceRow]) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not row.code or not row.domain:
            continue
        key = (row.document_type or "", row.code, row.applies_to_kind or "")
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


async def prune_reference_domain_rows(
    session: AsyncSession,
    *,
    domain: str,
    rows: list[ReferenceRow],
    commit: bool = True,
) -> int:
    """Delete ``pr_po_reference_values`` rows in ``domain`` not present in ``rows``.

    Refuses to wipe the domain when ``rows`` is empty (failed/empty SAP fetch).
    """
    keys = _reference_row_keys(rows)
    if not keys:
        log.warning(
            "reference_sync %s: prune skipped — 0 SAP keys (refusing to wipe domain)",
            domain,
        )
        return 0

    await session.execute(
        text(
            """
            CREATE TEMP TABLE _ref_sync_keys (
                document_type text NOT NULL DEFAULT '',
                code text NOT NULL,
                applies_to_kind text NOT NULL DEFAULT ''
            ) ON COMMIT DROP
            """
        )
    )
    for i in range(0, len(keys), _PRUNE_KEY_INSERT_CHUNK):
        chunk = keys[i : i + _PRUNE_KEY_INSERT_CHUNK]
        await session.execute(
            text(
                """
                INSERT INTO _ref_sync_keys (document_type, code, applies_to_kind)
                VALUES (:document_type, :code, :applies_to_kind)
                """
            ),
            [
                {"document_type": dt, "code": code, "applies_to_kind": atk}
                for dt, code, atk in chunk
            ],
        )
    result = await session.execute(
        text(
            """
            DELETE FROM pr_po_reference_values AS v
            WHERE v.domain = :domain
              AND NOT EXISTS (
                SELECT 1 FROM _ref_sync_keys AS k
                WHERE k.document_type = coalesce(v.document_type, '')
                  AND k.code = v.code
                  AND k.applies_to_kind = coalesce(v.applies_to_kind, '')
              )
            """
        ),
        {"domain": domain},
    )
    if commit:
        await session.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        log.info("reference_sync %s: pruned=%s", domain, deleted)
    return deleted
