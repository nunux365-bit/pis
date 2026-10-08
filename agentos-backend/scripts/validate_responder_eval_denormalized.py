#!/usr/bin/env python3
"""Validate denormalized responder eval columns against rubric JSON (read-only).

Run after backfill before deploying dashboard code that reads columns only (no eval_json
fallback on chart aggregates). Exit non-zero if tags or resolutions are out of sync.

Scans **all** low-grade rows (id cursor). Also flags chat/GT still embedded in
eval_json and unresolved attribute enrich (missing transcript/GT).

From agentos-backend/::

  export PYTHONPATH=.
  python scripts/validate_responder_eval_denormalized.py
  python scripts/validate_responder_eval_denormalized.py --sample-limit 0  # all low-grade
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from sqlalchemy import and_, func, or_, select, text

from app.agents.responder_eval.constants import EVAL_VERSION
from app.agents.responder_eval.denormalized import merge_eval_detail_json
from app.agents.responder_eval.issue_tags import (
    compute_issue_tags,
    enrich_eval_attr_fields,
    eval_dict_for_issue_tags,
)
from app.agents.responder_eval.models import ResponderEvalRun

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


def _expected_tags(row: ResponderEvalRun) -> tuple[dict[str, list[str]], bool, bool]:
    full = merge_eval_detail_json(
        row.eval_json,
        chat_json=row.chat_json,
        ground_truth_json=row.ground_truth_json,
    )
    chat = row.chat_json if isinstance(row.chat_json, dict) else full.get("chat")
    art = (
        row.ground_truth_json
        if isinstance(row.ground_truth_json, dict)
        else full.get("eval_ground_truth")
    )
    enriched = enrich_eval_attr_fields(
        full,
        chat=chat if isinstance(chat, dict) else None,
        artifact=art if isinstance(art, dict) else None,
        force=True,
    )
    enrich_skipped = bool(enriched.pop("_attr_enrich_skipped", None))
    enrich_partial = bool(enriched.pop("_attr_enrich_partial", None))
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
    return tags, enrich_skipped, enrich_partial


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=0,
        help="If >0, only check this many low-grade rows (newest first). 0 = all.",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail when attr enrich is skipped (missing chat/GT).",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="Warn only on Grade E↔hard-gate drift (default: fail those checks).",
    )
    args = parser.parse_args()

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
        low_n = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(ResponderEvalRun.eval_version == EVAL_VERSION, _LOW_GRADE_FILTER)
            )
            or 0
        )
        chat_in_json = int(
            await session.scalar(
                text(
                    """
                    SELECT count(*) FROM responder_eval_runs
                    WHERE eval_version = :ver AND eval_json ? 'chat'
                    """
                ),
                {"ver": EVAL_VERSION},
            )
            or 0
        )
        gt_in_json = int(
            await session.scalar(
                text(
                    """
                    SELECT count(*) FROM responder_eval_runs
                    WHERE eval_version = :ver AND eval_json ? 'eval_ground_truth'
                    """
                ),
                {"ver": EVAL_VERSION},
            )
            or 0
        )
        empty_low = int(
            await session.scalar(
                select(func.count())
                .select_from(ResponderEvalRun)
                .where(
                    ResponderEvalRun.eval_version == EVAL_VERSION,
                    _LOW_GRADE_FILTER,
                    func.cardinality(ResponderEvalRun.composite_issues) == 0,
                    func.cardinality(ResponderEvalRun.bot_issues) == 0,
                    func.cardinality(ResponderEvalRun.human_issues) == 0,
                )
            )
            or 0
        )
        res_mismatch = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT count(*) FROM responder_eval_runs
                        WHERE eval_version = :ver
                          AND composite_resolution IS NOT NULL
                          AND eval_json->>'resolution' IS NOT NULL
                          AND composite_resolution <> eval_json->>'resolution'
                        """
                    ),
                    {"ver": EVAL_VERSION},
                )
            ).scalar_one()
            or 0
        )

    # Cursor over low-grade rows
    tag_mismatch = 0
    enrich_skipped = 0
    enrich_partial = 0
    grade_soft_drift = 0
    grade_hall_stripped = 0
    grade_e_soft_score_band = 0
    checked = 0
    last_id: uuid.UUID | None = None
    examples: list[str] = []

    while True:
        if args.sample_limit and checked >= args.sample_limit:
            break
        limit = args.batch_size
        if args.sample_limit:
            limit = min(limit, args.sample_limit - checked)
        async with AsyncSessionLocal() as session:
            q = (
                select(ResponderEvalRun)
                .where(ResponderEvalRun.eval_version == EVAL_VERSION, _LOW_GRADE_FILTER)
                .order_by(ResponderEvalRun.id.asc())
                .limit(limit)
            )
            if last_id is not None:
                q = q.where(ResponderEvalRun.id > last_id)
            rows = list((await session.execute(q)).scalars().all())
        if not rows:
            break
        for row in rows:
            checked += 1
            last_id = row.id
            expected, skipped, partial = _expected_tags(row)
            if skipped:
                enrich_skipped += 1
            if partial:
                enrich_partial += 1
            mismatched = (
                list(row.composite_issues or []) != expected["composite_issues"]
                or list(row.bot_issues or []) != expected["bot_issues"]
                or list(row.human_issues or []) != expected["human_issues"]
            )
            if mismatched:
                tag_mismatch += 1
                if len(examples) < 8:
                    examples.append(row.chat_id)

            # Grade E ↔ hard-gate drift: only fail when eval_json still asserts hard_gate
            # but reasons/tags no longer cite PII or hallucination hard gates (incoherent).
            # Grade E + soft hall tags + hard_gate=False is valid (score-band E).
            reasons = [
                str(r)
                for r in ((row.eval_json or {}).get("hard_gate_reasons") or [])
            ]
            has_hard_hal = any(r.startswith("hallucination_") for r in reasons)
            has_pii = "pii_incident" in reasons
            hard_gate = bool((row.eval_json or {}).get("hard_gate"))
            tags_now = set(row.composite_issues or [])
            flagged = bool((row.eval_json or {}).get("hallucination_flagged"))
            if (
                row.letter_grade == "E"
                and "hallucination_severity:soft" in tags_now
                and not has_hard_hal
                and "hallucination_severity:P0" not in tags_now
                and "hallucination_severity:P1" not in tags_now
            ):
                if hard_gate and not has_pii:
                    grade_soft_drift += 1
                elif not hard_gate:
                    grade_e_soft_score_band += 1
            # Empty-turn strip clears hard reasons AND soft parent (no turns).
            if (
                row.letter_grade == "E"
                and flagged
                and not has_hard_hal
                and "hallucination" not in tags_now
                and "hallucination_severity:soft" not in tags_now
            ):
                grade_hall_stripped += 1

    print(f"total runs ({EVAL_VERSION}): {total}")
    print(f"low-grade runs: {low_n}")
    print(f"low-grade checked: {checked}")
    print(f"eval_json still has 'chat': {chat_in_json}")
    print(f"eval_json still has 'eval_ground_truth': {gt_in_json}")
    print(f"low-grade with all empty issue arrays: {empty_low}")
    print(f"composite_resolution vs json mismatches: {res_mismatch}")
    print(f"low-grade issue-tag mismatches: {tag_mismatch}")
    print(f"low-grade attr-enrich incomplete (missing chat/GT): {enrich_skipped}")
    print(f"low-grade attr-enrich partial (sparse GT, no reclassify): {enrich_partial}")
    print(f"grade E + soft hall tags without hard hall reason: {grade_soft_drift}")
    print(
        f"grade E + soft hall tags, score-band (hard_gate=false, OK): {grade_e_soft_score_band}"
    )
    print(f"grade E + hall flagged but no hall parent tags (stripped empty turns): {grade_hall_stripped}")
    if examples:
        print(f"mismatch examples: {examples}")

    hard_fails: list[str] = []
    warns: list[str] = []
    if chat_in_json or gt_in_json:
        hard_fails.append("chat/GT still embedded in eval_json — run migration 053 or backfill")
    if res_mismatch:
        hard_fails.append("resolution column drift from eval_json")
    if tag_mismatch:
        hard_fails.append(
            f"{tag_mismatch} low-grade rows differ from recompute — re-run backfill script"
        )
    if empty_low and low_n and empty_low == low_n:
        hard_fails.append("all low-grade rows have empty issue arrays — backfill never ran")
    if enrich_skipped:
        msg = (
            f"{enrich_skipped} low-grade rows cannot replay modes/reasons (missing chat/GT)"
        )
        (hard_fails if args.strict else warns).append(msg)
    if enrich_partial:
        warns.append(
            f"{enrich_partial} low-grade rows have sparse GT — hard reasons left unchanged"
        )
    if grade_soft_drift:
        msg = (
            f"{grade_soft_drift} Grade E rows have hard_gate=true but no PII/hallucination "
            "hard reasons while soft hall tags are present (incoherent eval_json)"
        )
        (warns if args.lenient else hard_fails).append(msg)
    if grade_hall_stripped:
        msg = (
            f"{grade_hall_stripped} Grade E rows still have hallucination_flagged but neither "
            "hard hall reasons nor soft/hard hall parent tags "
            "(likely empty-turn force enrich cleared gates without rescore)"
        )
        (warns if args.lenient else hard_fails).append(msg)

    if warns:
        print("\nWARN:")
        for w in warns:
            print(f"  - {w}")
    if hard_fails:
        print("\nFAIL:")
        for i in hard_fails:
            print(f"  - {i}")
        sys.exit(1)
    print("\nOK: denormalized storage looks consistent")


if __name__ == "__main__":
    asyncio.run(main())
