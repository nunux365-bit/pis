#!/usr/bin/env python3
"""Seed hand-off sample chats, re-run responder eval, validate dashboard coverage.

From ``agentos-backend/``::

  export PYTHONPATH=.
  python scripts/seed_and_rerun_handoff_eval.py --seed 500 --rerun-all
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.agents.responder_eval.constants import (
    EVAL_VERSION,
    LETTER_GRADE_NOT_GRADED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_NOT_GRADED,
)
from app.agents.responder_eval.denormalized import build_persist_row_fields, split_eval_storage
from app.agents.responder_eval.handoff_dashboard import eval_handoff_analysis
from app.agents.responder_eval.handoff_taxonomy import (
    HANDOFF_TAXONOMY,
    sub_bucket_label,
)
from app.agents.responder_eval.models import ResponderEvalRun
from app.agents.responder_eval.pipeline import run_eval_for_chat
from app.config.settings import settings

log = logging.getLogger("seed_handoff_eval")

_MINIMAL_ARTIFACT = {
    "preflight": {"order_status": "In Transit"},
    "perfect_order": {"overall_pass": True},
    "order_ops": {"payment_summary": {"total_refund_due": 0}},
}


def _all_sub_buckets() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for bucket, subs in HANDOFF_TAXONOMY.items():
        for sub in subs:
            out.append((bucket, sub, sub_bucket_label(sub)))
    return out


def _build_chat(
    chat_id: str,
    *,
    bucket: str,
    sub_bucket: str,
    sub_label: str,
    seq: int,
) -> dict[str, Any]:
    base = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC) + timedelta(minutes=seq)
    ts = [base + timedelta(minutes=i * 2) for i in range(6)]

    def _msg(role: str, content: str, i: int) -> dict[str, Any]:
        return {"role": role, "content": content, "created_at": ts[i].isoformat()}

    return {
        "chat_id": chat_id,
        "metadata": {
            "channel": "app",
            "handoff_bucket": bucket,
            "handoff_sub_bucket": sub_bucket,
            "seed_seq": seq,
        },
        "messages": [
            _msg("user", f"Hello — {sub_label}. Order PO{9000000000 + seq}.", 0),
            _msg("bot", "Welcome to Tata 1mg. How can I help you today?", 1),
            _msg("user", f"I still need help with: {sub_label}.", 2),
            _msg("bot", "Let me look into that for you.", 3),
            _msg("user", "Please connect me to a human agent now.", 4),
            _msg("agent", f"Hi, I am a live agent. I understand your concern about {sub_label}.", 5),
        ],
    }


def _day_for_index(idx: int, *, total: int) -> datetime:
    """Spread rows across D-2, D-1, and today (UTC)."""
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    day_offset = 2 - (idx % 3)  # 2,1,0 => D-2, D-1, Today
    day = today - timedelta(days=day_offset)
    # Stagger within day
    minute = (idx * 7) % (24 * 60)
    return day + timedelta(minutes=minute)


async def _upsert_eval_run(result: dict[str, Any], *, created_at: datetime) -> None:
    from app.db.session import AsyncSessionLocal

    graded = bool(result.get("graded", True))
    denorm = build_persist_row_fields(result)
    rubric_json, chat_json, ground_truth_json = split_eval_storage(result)
    row = {
        "chat_id": result["chat_id"],
        "order_id": result.get("order_id"),
        "rca_run_id": result.get("rca_run_id"),
        "eval_version": result.get("eval_version", EVAL_VERSION),
        "composite_score": result.get("composite_score") if graded else None,
        "letter_grade": str(
            result.get("letter_grade") or (LETTER_GRADE_NOT_GRADED if not graded else "E")
        ),
        **denorm,
        "eval_status": RUN_STATUS_COMPLETED if graded else RUN_STATUS_NOT_GRADED,
        "eval_json": rubric_json,
        "chat_json": chat_json,
        "ground_truth_json": ground_truth_json,
        "created_at": created_at,
        "updated_at": created_at,
    }
    stmt = insert(ResponderEvalRun).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ResponderEvalRun.chat_id],
        set_={k: stmt.excluded[k] for k in row if k not in ("chat_id", "created_at")},
    )
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(stmt)


async def _eval_and_upsert(
    chat: dict[str, Any],
    *,
    chat_id: str,
    order_id: str,
    created_at: datetime,
) -> dict[str, Any]:
    dump = SimpleNamespace(
        chat_id=chat_id,
        order_id=order_id,
        run_id=f"seed-{uuid.uuid4().hex[:12]}",
        eval_artifact=dict(_MINIMAL_ARTIFACT),
    )
    result = await run_eval_for_chat(dump, chat, chat_redacted=True)
    result["order_id"] = order_id
    result["rca_run_id"] = dump.run_id
    await _upsert_eval_run(result, created_at=created_at)
    return result


async def seed_chats(*, count: int) -> list[str]:
    subs = _all_sub_buckets()
    if count < len(subs):
        raise ValueError(f"--seed must be at least {len(subs)} to cover every sub-bucket")

    chat_ids: list[str] = []
    for i in range(count):
        bucket, sub_bucket, sub_label = subs[i % len(subs)]
        chat_id = f"handoff-seed-{i:04d}"
        order_id = f"POHANDOFF{i:010d}"
        chat = _build_chat(
            chat_id,
            bucket=bucket,
            sub_bucket=sub_bucket,
            sub_label=sub_label,
            seq=i,
        )
        created_at = _day_for_index(i, total=count)
        await _eval_and_upsert(chat, chat_id=chat_id, order_id=order_id, created_at=created_at)
        chat_ids.append(chat_id)
        if (i + 1) % 50 == 0:
            log.info("seeded %s / %s", i + 1, count)
    return chat_ids


async def rerun_all_evals() -> int:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ResponderEvalRun).where(ResponderEvalRun.eval_version == EVAL_VERSION)
                )
            )
            .scalars()
            .all()
        )

    n = 0
    for row in rows:
        chat = row.chat_json if isinstance(row.chat_json, dict) else None
        if not chat or not chat.get("messages"):
            log.warning("skip chat_id=%s — no chat_json", row.chat_id)
            continue
        await _eval_and_upsert(
            chat,
            chat_id=row.chat_id,
            order_id=row.order_id or f"PO{row.chat_id[-10:]}",
            created_at=row.created_at or datetime.now(UTC),
        )
        n += 1
        if n % 50 == 0:
            log.info("re-evaluated %s / %s", n, len(rows))
    return n


async def validate() -> dict[str, Any]:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        total = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(ResponderEvalRun.eval_version == EVAL_VERSION)
            )
            or 0
        )
        handoff_total = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(
                    ResponderEvalRun.eval_version == EVAL_VERSION,
                    ResponderEvalRun.handoff_bucket.isnot(None),
                    ResponderEvalRun.handoff_sub_bucket.isnot(None),
                )
            )
            or 0
        )
        distinct_subs = int(
            await session.scalar(
                select(func.count(func.distinct(ResponderEvalRun.handoff_sub_bucket))).where(
                    ResponderEvalRun.eval_version == EVAL_VERSION,
                    ResponderEvalRun.handoff_sub_bucket.isnot(None),
                )
            )
            or 0
        )
        dashboard = await eval_handoff_analysis(session)

    expected_subs = sum(len(v) for v in HANDOFF_TAXONOMY.values())
    return {
        "total_runs": total,
        "handoff_classified": handoff_total,
        "distinct_sub_buckets": distinct_subs,
        "expected_sub_buckets": expected_subs,
        "coverage_ok": distinct_subs >= expected_subs,
        "dashboard_volume": dashboard.get("volume"),
        "dashboard_days": len(dashboard.get("day_headers") or dashboard.get("days") or []),
    }


async def _main(args: argparse.Namespace) -> int:
    settings.responder_eval_mock_judge = True
    settings.responder_eval_presidio_enabled = False

    if args.seed > 0:
        log.info("seeding %s hand-off chats …", args.seed)
        await seed_chats(count=args.seed)

    if args.rerun_all:
        log.info("re-running eval on all runs …")
        n = await rerun_all_evals()
        log.info("re-evaluated %s runs", n)

    report = await validate()
    log.info("validation: %s", report)
    if not report["coverage_ok"]:
        log.error(
            "sub-bucket coverage incomplete: %s / %s",
            report["distinct_sub_buckets"],
            report["expected_sub_buckets"],
        )
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Seed / re-run hand-off responder eval data")
    p.add_argument("--seed", type=int, default=0, help="Number of sample chats to create")
    p.add_argument("--rerun-all", action="store_true", help="Re-run eval on every existing run")
    args = p.parse_args()
    if args.seed <= 0 and not args.rerun_all:
        p.error("pass --seed N and/or --rerun-all")
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
