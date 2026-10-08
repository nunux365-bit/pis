#!/usr/bin/env python3
"""Backfill denormalized responder eval columns (issue tags + JSON split).

**Required before deploying dashboard code that reads denormalized columns only**
(no eval_json fallback on chart aggregates).

Also re-tags hierarchical attributes (hallucination modes, policy buckets,
traceability reasons, resolution lifecycle) by replaying claim/reason helpers
from stored chat + GT — no LLM rejudge. Rebuilds missing segment
``turn_indexes`` from chat roles when sparse.

Run after Alembic revision ``053_responder_eval_denormalized_storage``.

From ``agentos-backend/``::

  export PYTHONPATH=.
  python scripts/backfill_responder_eval_denormalized.py --dry-run
  python scripts/backfill_responder_eval_denormalized.py --batch-size 200
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from sqlalchemy import and_, func, or_, select, update

from app.agents.responder_eval.constants import EVAL_VERSION
from app.agents.responder_eval.denormalized import merge_eval_detail_json, split_eval_storage
from app.agents.responder_eval.issue_tags import (
    compute_issue_tags,
    enrich_eval_attr_fields,
    eval_dict_for_issue_tags,
)
from app.agents.responder_eval.models import ResponderEvalRun

log = logging.getLogger("backfill_responder_eval_denormalized")

_LOW_GRADE_FILTER = or_(
    ResponderEvalRun.letter_grade.in_(("C", "D", "E")),
    and_(
        ResponderEvalRun.bot_grade.in_(("C", "D", "E")),
        ResponderEvalRun.bot_score.isnot(None),
    ),
    and_(
        ResponderEvalRun.human_grade.in_(("C", "D", "E")),
        ResponderEvalRun.human_score.isnot(None),
    ),
)


def _full_eval_dict(row: ResponderEvalRun) -> dict[str, Any]:
    return merge_eval_detail_json(
        row.eval_json if isinstance(row.eval_json, dict) else {},
        chat_json=row.chat_json if isinstance(row.chat_json, dict) else None,
        ground_truth_json=(
            row.ground_truth_json if isinstance(row.ground_truth_json, dict) else None
        ),
    )


def _needs_attr_replay(ev: dict[str, Any]) -> bool:
    reasons = {str(r) for r in (ev.get("hard_gate_reasons") or [])}
    want_modes = any(r.startswith("hallucination_") for r in reasons) or bool(
        ev.get("hallucination_flagged")
    )
    want_trace = ev.get("traceability_pass") is False
    return want_modes or want_trace


async def _count_estimates(*, all_versions: bool) -> dict[str, int]:
    from app.db.session import AsyncSessionLocal

    version_filter = (
        ()
        if all_versions
        else (ResponderEvalRun.eval_version == EVAL_VERSION,)
    )
    async with AsyncSessionLocal() as session:
        total = int(
            await session.scalar(
                select(func.count()).select_from(ResponderEvalRun).where(*version_filter)
            )
            or 0
        )
        low = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(*version_filter, _LOW_GRADE_FILTER)
            )
            or 0
        )
        empty_all = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(
                    *version_filter,
                    _LOW_GRADE_FILTER,
                    func.cardinality(ResponderEvalRun.composite_issues) == 0,
                    func.cardinality(ResponderEvalRun.bot_issues) == 0,
                    func.cardinality(ResponderEvalRun.human_issues) == 0,
                )
            )
            or 0
        )
        low_with_any_tags = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(
                    *version_filter,
                    _LOW_GRADE_FILTER,
                    or_(
                        func.cardinality(ResponderEvalRun.composite_issues) > 0,
                        func.cardinality(ResponderEvalRun.bot_issues) > 0,
                        func.cardinality(ResponderEvalRun.human_issues) > 0,
                    ),
                )
            )
            or 0
        )
        return {
            "total": total,
            "low_grade": low,
            "low_empty_issue_arrays": empty_all,
            "low_with_any_tags": low_with_any_tags,
        }


async def _fetch_batch(
    last_id: uuid.UUID | None,
    limit: int,
    *,
    all_versions: bool,
) -> list[ResponderEvalRun]:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        q = select(ResponderEvalRun).order_by(ResponderEvalRun.id.asc()).limit(limit)
        if not all_versions:
            q = q.where(ResponderEvalRun.eval_version == EVAL_VERSION)
        if last_id is not None:
            q = q.where(ResponderEvalRun.id > last_id)
        rows = (await session.execute(q)).scalars().all()
        return list(rows)


def _prepare_row(row: ResponderEvalRun) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Return enriched/tags/split + optional skip_reason for attr enrich."""
    full = _full_eval_dict(row)
    chat = row.chat_json if isinstance(row.chat_json, dict) else full.get("chat")
    artifact = (
        row.ground_truth_json
        if isinstance(row.ground_truth_json, dict)
        else full.get("eval_ground_truth")
    )
    skip_reason: str | None = None
    if _needs_attr_replay(full) and not (
        isinstance(chat, dict) and isinstance(artifact, dict)
    ):
        skip_reason = "missing_chat_or_ground_truth"

    enriched = enrich_eval_attr_fields(
        full,
        chat=chat if isinstance(chat, dict) else None,
        artifact=artifact if isinstance(artifact, dict) else None,
        force=True,
    )
    if enriched.pop("_attr_enrich_skipped", None) and skip_reason is None:
        skip_reason = str(enriched.pop("_attr_enrich_skip_reason", "") or "enrich_skipped")
    else:
        enriched.pop("_attr_enrich_skip_reason", None)

    tags = compute_issue_tags(
        eval_dict_for_issue_tags(
            enriched,
            letter_grade=row.letter_grade,
            bot_grade=row.bot_grade,
            human_grade=row.human_grade,
            bot_score=row.bot_score,
            human_score=row.human_score,
        )
    )
    # Strip internal markers before persist
    enriched.pop("_attr_enrich_skipped", None)
    enriched.pop("_attr_enrich_skip_reason", None)
    enriched.pop("_attr_enrich_partial", None)
    rubric, chat_json, ground_truth_json = split_eval_storage(enriched)
    return enriched, tags, rubric, chat_json, ground_truth_json, skip_reason


def _row_unchanged(
    row: ResponderEvalRun,
    tags: dict[str, list[str]],
    enriched: dict[str, Any],
) -> bool:
    return (
        list(row.composite_issues or []) == tags["composite_issues"]
        and list(row.bot_issues or []) == tags["bot_issues"]
        and list(row.human_issues or []) == tags["human_issues"]
        and "chat" not in (row.eval_json or {})
        and "eval_ground_truth" not in (row.eval_json or {})
        and (row.eval_json or {}).get("hallucination_failure_modes")
        == enriched.get("hallucination_failure_modes")
        and (row.eval_json or {}).get("traceability_failure_reasons")
        == enriched.get("traceability_failure_reasons")
        and list((row.eval_json or {}).get("hard_gate_reasons") or [])
        == list(enriched.get("hard_gate_reasons") or [])
        and (row.eval_json or {}).get("hallucination_severity")
        == enriched.get("hallucination_severity")
    )


async def _backfill_batch(rows: list[ResponderEvalRun], *, dry_run: bool) -> dict[str, int]:
    stats = {"updated": 0, "unchanged": 0, "errors": 0, "enrich_skipped": 0, "would_update": 0}
    if not rows:
        return stats

    if dry_run:
        for row in rows:
            try:
                enriched, tags, rubric, chat_json, ground_truth_json, skip_reason = _prepare_row(
                    row
                )
                if skip_reason:
                    stats["enrich_skipped"] += 1
                    log.warning(
                        "attr enrich incomplete chat_id=%s reason=%s",
                        row.chat_id,
                        skip_reason,
                    )
                if _row_unchanged(row, tags, enriched):
                    stats["unchanged"] += 1
                else:
                    stats["would_update"] += 1
            except Exception:
                stats["errors"] += 1
                log.exception("backfill prepare failed chat_id=%s id=%s", row.chat_id, row.id)
        return stats

    from app.db.session import AsyncSessionLocal

    # Re-read under FOR UPDATE so a live persist cannot be overwritten with a
    # stale snapshot. Concurrent backfills wait on the same row.
    for row in rows:
        try:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    fresh = (
                        await session.execute(
                            select(ResponderEvalRun)
                            .where(ResponderEvalRun.id == row.id)
                            .with_for_update()
                        )
                    ).scalars().first()
                    if fresh is None:
                        continue
                    enriched, tags, rubric, chat_json, ground_truth_json, skip_reason = (
                        _prepare_row(fresh)
                    )
                    if skip_reason:
                        stats["enrich_skipped"] += 1
                        log.warning(
                            "attr enrich incomplete chat_id=%s reason=%s",
                            fresh.chat_id,
                            skip_reason,
                        )
                    if _row_unchanged(fresh, tags, enriched):
                        stats["unchanged"] += 1
                        continue
                    await session.execute(
                        update(ResponderEvalRun)
                        .where(ResponderEvalRun.id == fresh.id)
                        .values(
                            **tags,
                            eval_json=rubric,
                            chat_json=chat_json,
                            ground_truth_json=ground_truth_json,
                        )
                    )
                    stats["updated"] += 1
        except Exception:
            stats["errors"] += 1
            log.exception("backfill update failed chat_id=%s id=%s", row.chat_id, row.id)
    return stats


async def run_backfill(
    *,
    batch_size: int,
    dry_run: bool,
    max_batches: int | None,
    all_versions: bool,
) -> None:
    await _run_backfill(
        batch_size=batch_size,
        dry_run=dry_run,
        max_batches=max_batches,
        all_versions=all_versions,
    )


async def _run_backfill(
    *,
    batch_size: int,
    dry_run: bool,
    max_batches: int | None,
    all_versions: bool,
) -> None:
    est = await _count_estimates(all_versions=all_versions)
    log.info(
        "estimates (version=%s): total=%d low_grade=%d empty_issue_arrays=%d low_with_any_tags=%d",
        "ALL" if all_versions else EVAL_VERSION,
        est["total"],
        est["low_grade"],
        est["low_empty_issue_arrays"],
        est["low_with_any_tags"],
    )
    if dry_run:
        log.info("dry-run: will prepare+diff (including enrich) but not write")

    batches = 0
    totals = {"updated": 0, "unchanged": 0, "errors": 0, "enrich_skipped": 0, "would_update": 0}
    last_id: uuid.UUID | None = None
    while True:
        if max_batches is not None and batches >= max_batches:
            break
        rows = await _fetch_batch(last_id, batch_size, all_versions=all_versions)
        if not rows:
            break
        stats = await _backfill_batch(rows, dry_run=dry_run)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        batches += 1
        last_id = rows[-1].id
        log.info(
            "batch %d size=%d updated=%d unchanged=%d enrich_skipped=%d errors=%d%s",
            batches,
            len(rows),
            stats["updated"],
            stats["unchanged"],
            stats["enrich_skipped"],
            stats["errors"],
            f" would_update={stats['would_update']}" if dry_run else "",
        )

    log.info(
        "done: updated=%d unchanged=%d enrich_skipped=%d errors=%d%s",
        totals["updated"],
        totals["unchanged"],
        totals["enrich_skipped"],
        totals["errors"],
        f" would_update={totals['would_update']}" if dry_run else "",
    )
    if totals["errors"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill responder eval denormalized columns")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument(
        "--all-versions",
        action="store_true",
        help=f"Include rows outside EVAL_VERSION={EVAL_VERSION} (default: current version only).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    asyncio.run(
        run_backfill(
            batch_size=max(1, args.batch_size),
            dry_run=args.dry_run,
            max_batches=args.max_batches,
            all_versions=args.all_versions,
        )
    )


if __name__ == "__main__":
    main()
