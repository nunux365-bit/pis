"""List MySQL-backed compliance candidates — paginate ``calls`` (joined to conversations)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

from app.agents.compliance_call.batch_item import ComplianceBatchItem
from app.config.settings import settings
from app.infra.mysql_compliance import get_mysql_compliance_engine

log = logging.getLogger(__name__)


def _mysql_datetime_as_read_from_db(v: Any) -> str | None:
    """Same textual form as MySQL ``DATETIME`` / client display — no ISO-T, no TZ conversion."""
    if v is None:
        return None
    if isinstance(v, datetime):
        base = v.strftime("%Y-%m-%d %H:%M:%S")
        if v.microsecond:
            frac = f"{v.microsecond:06d}".rstrip("0")
            return f"{base}.{frac}" if frac else base
        return base
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def _parse_opinion_ids(raw: str) -> list[int]:
    out: list[int] = []
    for p in (raw or "").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            log.warning("compliance mysql: skip invalid second_opinion_id token %r", p)
    return out


def _mysql_candidate_filter_tz_name() -> str:
    """IANA zone for ``soc.updated_at`` / day-bound filters (rolling tick + backfill)."""
    return (settings.compliance_call_mysql_timezone or "UTC").strip() or "UTC"


def _window_naive_local() -> tuple[datetime, datetime]:
    """Start of previous calendar day → now, in ``compliance_call_mysql_timezone`` (naive local datetimes)."""
    tz_name = _mysql_candidate_filter_tz_name()
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    prev_date = now_local.date() - timedelta(days=1)
    start_local = datetime.combine(prev_date, time.min).replace(tzinfo=tz)
    end_local = now_local
    return start_local.replace(tzinfo=None), end_local.replace(tzinfo=None)


async def fetch_mysql_compliance_candidates(
    *,
    max_candidates: int | None = None,
    after_call_id: int = 0,
) -> list[ComplianceBatchItem]:
    """Walk ``calls`` rows with stable ordering ``calls.id ASC`` where the parent conversation matches filters.

    Keyset: ``calls.id > after_call_id`` — pass :func:`~app.services.compliance_mysql_watermark.max_completed_mysql_call_id`
    so each tick continues after the last **completed** ingest (no separate cursor table).

    When ``max_candidates`` is set, stop once that many rows are collected.
    """
    if not settings.compliance_call_mysql_enabled:
        return []
    url = (settings.compliance_call_mysql_url or "").strip()
    if not url:
        log.warning("compliance mysql: enabled but COMPLIANCE_CALL_MYSQL_URL is empty")
        return []

    opinion_ids = _parse_opinion_ids(settings.compliance_call_second_opinion_ids or "")
    if not opinion_ids:
        log.warning("compliance mysql: COMPLIANCE_CALL_SECOND_OPINION_IDS is empty")
        return []

    page_size = int(settings.compliance_mysql_page_size or 20)
    page_size = max(1, min(page_size, 500))
    start_naive, end_naive = _window_naive_local()

    engine = get_mysql_compliance_engine()
    items: list[ComplianceBatchItem] = []

    sql_page = (
        text(
            """
            SELECT c.id, c.doctor_id, c.room_name, c.metadata, c.provider_reference_id,
                   c.updated_at AS mysql_call_updated_at,
                   c.second_opinion_conversation_id AS second_opinion_conversation_id,
                   soc.second_opinion_id AS second_opinion_id
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
        async with engine.connect() as conn:
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
            async with engine.connect() as conn:
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
                log.warning(
                    "compliance mysql: invalid second_opinion_conversation_id=%r on calls.id=%s (sheet conversation_id blank)",
                    soc_id,
                    cid,
                )
                soc_int = None

            mysql_upd_raw = _mysql_datetime_as_read_from_db(r.get("mysql_call_updated_at"))

            items.append(
                ComplianceBatchItem(
                    source="mysql_call",
                    mysql_call_id=cid,
                    mysql_second_opinion_id=int(r.get("second_opinion_id")) if r.get("second_opinion_id") is not None else None,
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

    log.info(
        "compliance mysql: collected %d call rows (opinion_ids=%s window=%s→%s after_call_id=%s max_candidates=%s)",
        len(items),
        opinion_ids,
        start_naive,
        end_naive,
        after_call_id,
        max_candidates,
    )
    return items
