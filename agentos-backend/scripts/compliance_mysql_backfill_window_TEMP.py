#!/usr/bin/env python3
# TEMPORARY — delete this file after one-off backfill. Not wired to CI or the scheduler.
"""One-off MySQL compliance_call backfill for ``soc.updated_at`` in a calendar window (local TZ).

Duplicates listing + claim logic from ``mysql_candidates`` / ``run.py``. Uses
``mysql_candidates._mysql_datetime_as_read_from_db`` for ``calls.updated_at`` (same as production).

From ``agentos-backend/``::

  PYTHONPATH=. python scripts/compliance_mysql_backfill_window_TEMP.py --start 2026-05-01 --end 2026-05-06 --dry-run
  PYTHONPATH=. python scripts/compliance_mysql_backfill_window_TEMP.py --start 2026-05-01 --end 2026-05-06

Day bounds use ``mysql_candidates._mysql_candidate_filter_tz_name()`` (currently
``compliance_call_mysql_timezone``): inclusive
``[start 00:00:00, end 23:59:59]`` as naive local datetimes (same convention as the rolling tick).

Requires the same ``.env`` as the batch job (Postgres, MySQL URL, opinion ids, system user id, STT/OpenAI, …).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_BACKEND_ROOT / ".env")
except Exception:
    pass

from sqlalchemy import and_, bindparam, or_, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.agents.compliance_call.batch_item import ComplianceBatchItem  # noqa: E402
from app.agents.compliance_call.fingerprint import make_ingest_fingerprint_mysql  # noqa: E402
from app.agents.compliance_call.graph import ComplianceCallState, run_single_compliance_call  # noqa: E402
from app.agents.compliance_call.mysql_candidates import (  # noqa: E402
    _mysql_candidate_filter_tz_name,
    _mysql_datetime_as_read_from_db,
)
from app.config.settings import settings  # noqa: E402
from app.db.models import WorkflowRun, WorkflowRunStatus  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.infra.mysql_compliance import get_mysql_compliance_engine  # noqa: E402
from app.services.compliance_call_projection import upsert_compliance_call_run  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

log = logging.getLogger("compliance_mysql_backfill_TEMP")

_WORKFLOW_KEY = "compliance_call"


def _parse_opinion_ids(raw: str) -> list[int]:
    out: list[int] = []
    for p in (raw or "").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            log.warning("skip invalid second_opinion_id token %r", p)
    return out


def _naive_mysql_range_inclusive_days(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    if start_day > end_day:
        raise ValueError("start must be <= end")
    tz_name = _mysql_candidate_filter_tz_name()
    tz = ZoneInfo(tz_name)
    s = datetime.combine(start_day, time.min).replace(tzinfo=tz).replace(tzinfo=None)
    e = datetime.combine(end_day, time(23, 59, 59)).replace(tzinfo=tz).replace(tzinfo=None)
    return s, e


def _system_user_id() -> uuid.UUID | None:
    raw = (settings.compliance_call_system_user_id or "").strip()
    if not raw:
        log.warning("COMPLIANCE_CALL_SYSTEM_USER_ID is not set")
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        log.warning("invalid COMPLIANCE_CALL_SYSTEM_USER_ID")
        return None


def _skip_tag(item: ComplianceBatchItem) -> dict[str, str]:
    return {"mysql_call_id": str(item.mysql_call_id or "")}


async def _fetch_window_batch(
    *,
    start_naive: datetime,
    end_naive: datetime,
    after_call_id: int,
    max_candidates: int | None,
) -> list[ComplianceBatchItem]:
    """Same predicates as ``fetch_mysql_compliance_candidates`` but fixed ``start_at``/``end_at``; ``after_call_id`` from 0 for backfill."""
    opinion_ids = _parse_opinion_ids(settings.compliance_call_second_opinion_ids or "")
    if not opinion_ids:
        log.warning("COMPLIANCE_CALL_SECOND_OPINION_IDS is empty")
        return []

    page_size = int(settings.compliance_mysql_page_size or 20)
    page_size = max(1, min(page_size, 500))
    engine_mysql = get_mysql_compliance_engine()
    items: list[ComplianceBatchItem] = []

    sql_page = (
        text(
            """
            SELECT c.id, c.doctor_id, c.room_name, c.metadata, c.provider_reference_id,
                   c.updated_at AS mysql_call_updated_at,
                   c.second_opinion_conversation_id AS second_opinion_conversation_id
            FROM calls c
            INNER JOIN second_opinion_conversations soc ON soc.id = c.second_opinion_conversation_id
            WHERE soc.second_opinion_id IN :opinion_ids
              AND soc.status = :st
              AND soc.updated_at >= :start_at
              AND soc.updated_at <= :end_at
              AND c.metadata IS NOT NULL
              AND c.id > :after_call_id
            ORDER BY c.id ASC
            LIMIT :lim
            """
        )
        .bindparams(bindparam("opinion_ids", expanding=True))
    )
    sql_docs = text(
        """
        SELECT id, first_name, last_name FROM doctors WHERE id IN :doc_ids
        """
    ).bindparams(bindparam("doc_ids", expanding=True))

    key_after = int(after_call_id)
    while True:
        async with engine_mysql.connect() as conn:
            call_rows = (
                await conn.execute(
                    sql_page,
                    {
                        "opinion_ids": opinion_ids,
                        "st": 4,
                        "start_at": start_naive,
                        "end_at": end_naive,
                        "after_call_id": key_after,
                        "lim": page_size,
                    },
                )
            ).mappings().all()

        if not call_rows:
            break

        key_after = max(int(r["id"]) for r in call_rows)

        doc_ids = sorted({int(r["doctor_id"]) for r in call_rows if r.get("doctor_id") is not None})
        names: dict[int, tuple[str, str]] = {}
        if doc_ids:
            async with engine_mysql.connect() as conn:
                drows = (await conn.execute(sql_docs, {"doc_ids": doc_ids})).mappings().all()
            for d in drows:
                did = int(d["id"])
                fn = d.get("first_name")
                ln = d.get("last_name")
                slug = f"doctor_{did}"
                fn_s = str(fn or "").strip()
                ln_s = str(ln or "").strip()
                disp = f"{fn_s} {ln_s}".strip() or slug
                names[did] = (slug, disp)

        for r in call_rows:
            cid = int(r["id"])
            did = r.get("doctor_id")
            if did is not None:
                di = int(did)
                slug, dname = names.get(di, (f"doctor_{di}", f"doctor_{di}"))
            else:
                slug, dname = "doctor_unknown", "unknown"
            meta = r.get("metadata")
            if isinstance(meta, str):
                try:
                    meta_obj = json.loads(meta)
                except Exception:
                    meta_obj = {}
            elif isinstance(meta, dict):
                meta_obj = meta
            else:
                meta_obj = {}

            rn = r.get("room_name")
            room_s = str(rn).strip() if rn is not None else ""

            soc_id = r.get("second_opinion_conversation_id")
            try:
                soc_int = int(soc_id) if soc_id is not None else None
            except (TypeError, ValueError):
                log.warning("invalid second_opinion_conversation_id=%r calls.id=%s", soc_id, cid)
                soc_int = None

            mysql_upd_raw = _mysql_datetime_as_read_from_db(r.get("mysql_call_updated_at"))

            items.append(
                ComplianceBatchItem(
                    source="mysql_call",
                    mysql_call_id=cid,
                    mysql_second_opinion_conversation_id=soc_int,
                    mysql_room_name=room_s or None,
                    mysql_metadata=meta_obj,
                    mysql_provider_reference_id=str(r.get("provider_reference_id") or ""),
                    mysql_doctor_id=int(did) if did is not None else None,
                    mysql_call_updated_at=mysql_upd_raw,
                    filename=f"call_{cid}.media",
                    mime="application/octet-stream",
                    relative_path=f"mysql/calls/{cid}",
                    doctor_slug=slug,
                    doctor_name=dname,
                )
            )
            if max_candidates is not None and len(items) >= max_candidates:
                break

        if max_candidates is not None and len(items) >= max_candidates:
            break

    return items


async def _process_one_mysql(
    item: ComplianceBatchItem,
    *,
    uid: uuid.UUID,
    use_skip: bool,
) -> dict[str, Any]:
    """Mirror ``run_compliance_batch_async`` mysql branch (claim + graph)."""
    if item.mysql_call_id is None:
        return {"kind": "skipped", **_skip_tag(item), "reason": "missing_mysql_call_id"}

    fp = make_ingest_fingerprint_mysql(mysql_call_id=item.mysql_call_id)
    slug, dname = item.doctor_slug, item.doctor_name

    stale_before = datetime.now(timezone.utc) - timedelta(
        minutes=max(5, int(settings.compliance_stale_running_minutes or 45))
    )
    wid: uuid.UUID | None = None

    for _attempt in range(5):
        try:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    done = (
                        await db.execute(
                            select(WorkflowRun).where(
                                WorkflowRun.workflow_key == _WORKFLOW_KEY,
                                WorkflowRun.ingest_fingerprint == fp,
                                WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
                            )
                        )
                    ).scalars().first()
                    if done is not None:
                        return {"kind": "skipped", **_skip_tag(item), "reason": "already_completed"}

                    stmt = (
                        select(WorkflowRun)
                        .where(
                            WorkflowRun.workflow_key == _WORKFLOW_KEY,
                            WorkflowRun.ingest_fingerprint == fp,
                            or_(
                                WorkflowRun.status == WorkflowRunStatus.FAILED.value,
                                and_(
                                    WorkflowRun.status == WorkflowRunStatus.RUNNING.value,
                                    WorkflowRun.updated_at < stale_before,
                                ),
                            ),
                        )
                        .limit(1)
                    )
                    if use_skip:
                        stmt = stmt.with_for_update(skip_locked=True)
                    else:
                        stmt = stmt.with_for_update()

                    row = (await db.execute(stmt)).scalars().first()

                    inp_mysql = {
                        "source": "mysql_call",
                        "source_type": "mysql",
                        "mysql_call_id": item.mysql_call_id,
                        "mysql_second_opinion_conversation_id": item.mysql_second_opinion_conversation_id,
                        "mysql_call_updated_at": item.mysql_call_updated_at,
                        "source_file_id": str(item.mysql_call_id),
                        "mysql_room_name": item.mysql_room_name,
                        "mysql_metadata": item.mysql_metadata,
                        "mysql_provider_reference_id": item.mysql_provider_reference_id,
                        "mime": item.mime,
                        "filename": item.filename,
                        "doctor_slug": slug,
                        "doctor_name": dname,
                        "relative_path": item.relative_path,
                        "ingest_fingerprint": fp,
                    }

                    if row is not None:
                        row.status = WorkflowRunStatus.RUNNING.value
                        row.error_message = None
                        row.output_data = None
                        row.user_id = uid
                        row.input_data = inp_mysql
                        row.mysql_call_id = int(item.mysql_call_id)
                        await db.flush()
                        await upsert_compliance_call_run(db, row)
                        wid = row.id
                        break

                    wr = WorkflowRun(
                        user_id=uid,
                        workflow_key=_WORKFLOW_KEY,
                        status=WorkflowRunStatus.RUNNING.value,
                        ingest_fingerprint=fp,
                        input_data=inp_mysql,
                        mysql_call_id=int(item.mysql_call_id),
                    )
                    db.add(wr)
                    await db.flush()
                    await upsert_compliance_call_run(db, wr)
                    wid = wr.id
                    break
        except IntegrityError:
            await asyncio.sleep(0.05)
            continue

    if wid is None:
        async with AsyncSessionLocal() as dbx:
            r0 = (
                await dbx.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.workflow_key == _WORKFLOW_KEY,
                        WorkflowRun.ingest_fingerprint == fp,
                    )
                )
            ).scalars().first()
        if r0 and r0.status == WorkflowRunStatus.COMPLETED.value:
            return {"kind": "skipped", **_skip_tag(item), "reason": "already_completed"}
        if r0 and r0.status == WorkflowRunStatus.RUNNING.value:
            return {"kind": "skipped", **_skip_tag(item), "reason": "concurrent_running"}
        return {"kind": "skipped", **_skip_tag(item), "reason": "claim_unavailable"}

    st: ComplianceCallState = {
        "workflow_run_id": str(wid),
        "user_id": str(uid),
        "ingest_source": "mysql_call",
        "filename": item.filename,
        "mime": item.mime,
        "relative_path": item.relative_path,
        "media_mode": item.media_mode,
        "doctor_slug": slug,
        "doctor_name": dname,
        "mysql_call_id": int(item.mysql_call_id or 0),
        "mysql_room_name": str(item.mysql_room_name or ""),
        "mysql_metadata": item.mysql_metadata if isinstance(item.mysql_metadata, dict) else {},
        "drive_file_id": "",
        "revision": "",
    }
    if item.mysql_second_opinion_conversation_id is not None:
        st["mysql_second_opinion_conversation_id"] = int(item.mysql_second_opinion_conversation_id)
    if item.mysql_call_updated_at:
        st["mysql_call_updated_at"] = item.mysql_call_updated_at

    try:
        out = await run_single_compliance_call(st)
        return {"kind": "ok", "workflow_run_id": str(wid), "out": out}
    except Exception as e:
        log.exception("batch item failed mysql_call_id=%s", item.mysql_call_id)
        async with AsyncSessionLocal() as db2:
            r2 = await db2.get(WorkflowRun, wid)
            if r2:
                r2.status = WorkflowRunStatus.FAILED.value
                r2.error_message = str(e)[:2000]
                r2.output_data = {"error_code": "batch_invoke", "error_message": str(e)[:2000]}
                await upsert_compliance_call_run(db2, r2)
                await db2.commit()
        return {"kind": "error", "workflow_run_id": str(wid), "error": str(e)[:2000]}


def _parse_date(s: str) -> date:
    parts = s.strip().split("-")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


async def _amain(*, start: date, end: date, dry_run: bool, batch_cap: int) -> int:
    if not settings.compliance_call_mysql_enabled:
        log.error("compliance_call_mysql_enabled is false — refusing backfill")
        return 1
    if not settings.compliance_call_enabled:
        log.error("compliance_call_enabled is false")
        return 1
    uid = _system_user_id()
    if uid is None:
        return 1
    if not (settings.compliance_call_mysql_url or "").strip():
        log.error("COMPLIANCE_CALL_MYSQL_URL is empty")
        return 1

    w0, w1 = _naive_mysql_range_inclusive_days(start, end)
    log.info("window (naive local %s): %s → %s", _mysql_candidate_filter_tz_name(), w0, w1)

    dialect = engine.sync_engine.dialect.name
    use_skip = dialect == "postgresql"
    sem = asyncio.Semaphore(max(1, settings.compliance_batch_concurrency))

    after_id = 0
    total_rows = 0
    pages = 0
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    async def run_item(it: ComplianceBatchItem) -> None:
        async with sem:
            rec = await _process_one_mysql(it, uid=uid, use_skip=use_skip)
            if rec.get("kind") == "skipped":
                skipped.append({k: rec[k] for k in rec if k != "kind"})
            elif rec.get("kind") == "ok":
                results.append({"workflow_run_id": rec["workflow_run_id"], "out": rec.get("out")})
            elif rec.get("kind") == "error":
                results.append({"workflow_run_id": rec["workflow_run_id"], "error": rec.get("error")})

    while pages < 50_000:
        pages += 1
        batch = await _fetch_window_batch(
            start_naive=w0,
            end_naive=w1,
            after_call_id=after_id,
            max_candidates=batch_cap,
        )
        if not batch:
            break
        total_rows += len(batch)
        after_id = max(int(b.mysql_call_id or 0) for b in batch)
        log.info("page %s rows=%s after_call_id→%s", pages, len(batch), after_id)
        if not dry_run:
            await asyncio.gather(*[run_item(b) for b in batch])
        if len(batch) < batch_cap:
            break

    summary = {
        "dry_run": dry_run,
        "window_start": str(w0),
        "window_end": str(w1),
        "mysql_rows_total": total_rows,
        "pages": pages,
        "finished": len(results),
        "skipped": len(skipped),
    }
    log.info("%s", json.dumps(summary))
    if skipped and not dry_run:
        log.info("sample_skipped=%s", skipped[:20])
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="TEMP: MySQL compliance backfill for soc.updated_at window")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--batch-size", type=int, default=200, help="max rows per MySQL fetch (default 200)")
    args = p.parse_args()
    if args.start > args.end:
        p.error("--start must be <= --end")
    cap = max(1, min(int(args.batch_size), 500))
    return asyncio.run(_amain(start=args.start, end=args.end, dry_run=args.dry_run, batch_cap=cap))


if __name__ == "__main__":
    raise SystemExit(main())
