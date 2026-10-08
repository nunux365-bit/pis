"""Dashboard aggregates for responder eval runs.

Chart aggregates read denormalized columns only (no eval_json fallback).
Requires Alembic 053 + ``scripts/backfill_responder_eval_denormalized.py`` before deploy.

Rollback: redeploy prior backend (ignores new columns) or ``alembic downgrade 053`` (merges
chat_json / ground_truth_json back into eval_json and drops denormalized columns).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, case, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.agents.responder_eval.cohorts import BOT_CLOSED, BOT_PRE_HANDOFF, BOT_PRESENT, HUMAN_PRESENT
from app.agents.responder_eval.constants import (
    EVAL_STATUS_ABANDONED,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_PENDING,
    EVAL_STATUS_PROCESSING,
    EVAL_STATUS_WAITING,
    EVAL_VERSION,
    LETTER_GRADE_NOT_GRADED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_NOT_GRADED,
)
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun

Granularity = Literal["day", "week"]
_LOW_GRADES = frozenset({"C", "D", "E"})
_ISSUE_GRADE_ORDER = ("E", "D", "C")
_SCORE_BUCKET_ORDER = ["90-100", "75-89", "50-74", "1-49", "0"]

_QUEUE_STATUSES = (
    EVAL_STATUS_PENDING,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_WAITING,
    EVAL_STATUS_ABANDONED,
    EVAL_STATUS_PROCESSING,
)

# Re-export aliases used throughout this module.
_BOT_PRESENT = BOT_PRESENT
_BOT_CLOSED = BOT_CLOSED
_BOT_PRE_HANDOFF = BOT_PRE_HANDOFF
_HUMAN_PRESENT = HUMAN_PRESENT


def _round_avg(val: Any) -> float | None:
    if val is None:
        return None
    return round(float(val), 1)


def _window_bounds(days: int) -> tuple[datetime, datetime]:
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    return since, until


def _window_filters(since: datetime, until: datetime) -> tuple[Any, ...]:
    return (
        ResponderEvalRun.created_at >= since,
        ResponderEvalRun.created_at < until,
        ResponderEvalRun.eval_version == EVAL_VERSION,
    )


def _graded_filters() -> tuple[Any, ...]:
    return (ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED,)


def _score_bucket_expr(score_col: Any) -> Any:
    return case(
        (score_col >= 90, "90-100"),
        (score_col >= 75, "75-89"),
        (score_col >= 50, "50-74"),
        (score_col >= 1, "1-49"),
        else_="0",
    )


# --- Hierarchical issue tags (parents + attributes) ---

_LEGACY_HARD_GATE = {
    "hard_gate:pii_incident": ("hard_gate:pii", None),
    # Unified hallucination parent (soft + former hard_gate:hallucination).
    "hard_gate:hallucination": ("hallucination", None),
    "hard_gate:hallucination_P0": ("hallucination", "hallucination_severity:P0"),
    "hard_gate:hallucination_P1": ("hallucination", "hallucination_severity:P1"),
}


def _normalize_stored_tags(raw: list[str] | None) -> set[str]:
    """Map legacy TEXT[] tags to canonical hierarchical tags for aggregation."""
    from app.agents.responder_eval.issue_tags import _dedupe_policy_leaked_from_guardrails
    from app.agents.responder_eval.policy_buckets import normalize_policy_rule

    out: set[str] = set()
    for tag in raw or []:
        t = str(tag)
        if t in _LEGACY_HARD_GATE:
            parent, sev = _LEGACY_HARD_GATE[t]
            out.add(parent)
            if sev:
                out.add(sev)
            continue
        if t.startswith("policy:"):
            rule = t[len("policy:") :]
            out.add(f"policy:{normalize_policy_rule(rule)}")
            continue
        out.add(t)
    # Mirror write-path dedupe for legacy dual policy+guardrail tags.
    return set(_dedupe_policy_leaked_from_guardrails(sorted(out)))


def _is_parent_tag(tag: str) -> bool:
    if tag in {
        "hard_gate:pii",
        "hard_gate:inherited_pii",
        "hard_gate:inherited_hallucination",
        "hard_gate:hallucination",  # legacy until backfill
        "traceability_failure",
        "resolution:no",
        "resolution:partial",
        "hallucination",
        "guardrail_violations",
    }:
        return True
    if tag.startswith("guardrail:") or tag.startswith("policy:"):
        return True
    return False


def _is_attr_tag(tag: str) -> bool:
    return (
        tag.startswith("pii:")
        or tag.startswith("hallucination:")
        or tag.startswith("hallucination_severity:")
        or tag.startswith("resolution_lifecycle:")
        or tag.startswith("traceability:")
    )


def _attr_id(tag: str) -> str:
    if tag.startswith("hallucination_severity:"):
        return f"severity_{tag.split(':', 1)[1]}"
    return tag.split(":", 1)[1] if ":" in tag else tag


def _bind_attr_parent(tag: str, parents_on_row: set[str]) -> str | None:
    if tag.startswith("pii:"):
        return "hard_gate:pii" if "hard_gate:pii" in parents_on_row else None
    if tag.startswith("hallucination:") or tag.startswith("hallucination_severity:"):
        if "hallucination" in parents_on_row:
            return "hallucination"
        if "hard_gate:hallucination" in parents_on_row:  # legacy
            return "hard_gate:hallucination"
        return None
    if tag.startswith("traceability:"):
        return "traceability_failure" if "traceability_failure" in parents_on_row else None
    if tag.startswith("resolution_lifecycle:"):
        if "resolution:no" in parents_on_row:
            return "resolution:no"
        if "resolution:partial" in parents_on_row:
            return "resolution:partial"
        return None
    return None


def _bump_issues_nested(
    parent_by_grade: dict[str, Counter[str]],
    attr_by_grade: dict[str, dict[str, Counter[str]]],
    *,
    grade: str | None,
    issues: list[str] | None,
) -> None:
    if grade not in _LOW_GRADES:
        return
    tags = _normalize_stored_tags(issues)
    parents = {t for t in tags if _is_parent_tag(t)}
    for p in parents:
        parent_by_grade[grade][p] += 1
    for t in tags:
        if not _is_attr_tag(t):
            continue
        parent = _bind_attr_parent(t, parents)
        if parent is None:
            continue
        attr_by_grade[grade].setdefault(parent, Counter())[_attr_id(t)] += 1


def _top_n_nested(
    parent_counter: Counter[str],
    attr_counters: dict[str, Counter[str]],
    *,
    limit: int,
    attr_limit: int = 8,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ranked = sorted(parent_counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    for issue, count in ranked:
        attrs = [
            {"id": aid, "count": c}
            for aid, c in sorted(
                (attr_counters.get(issue) or Counter()).items(),
                key=lambda item: (-item[1], item[0]),
            )[:attr_limit]
        ]
        item: dict[str, Any] = {"issue": issue, "count": count}
        if attrs:
            item["attributes"] = attrs
        out.append(item)
    return out


def _top_n_nested_by_grade(
    parents: dict[str, Counter[str]],
    attrs: dict[str, dict[str, Counter[str]]],
    *,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    return {
        g: _top_n_nested(parents.get(g, Counter()), attrs.get(g, {}), limit=limit)
        for g in _ISSUE_GRADE_ORDER
    }


def _empty_grade_attr_maps() -> dict[str, dict[str, Counter[str]]]:
    return {g: {} for g in _ISSUE_GRADE_ORDER}


def _infer_attr_parent_for_aggregate(tag: str) -> str | None:
    """Parent for attr tags when aggregating SQL unnest counts (no row co-occurrence)."""
    if tag.startswith("traceability:"):
        return "traceability_failure"
    if tag.startswith("hallucination:") or tag.startswith("hallucination_severity:"):
        return "hallucination"
    if tag.startswith("pii:"):
        return "hard_gate:pii"
    return None


def _accumulate_tag_count(
    parent_by_grade: dict[str, Counter[str]],
    attr_by_grade: dict[str, dict[str, Counter[str]]],
    *,
    grade: str | None,
    raw_tag: str,
    count: int,
) -> None:
    if grade not in _LOW_GRADES or count <= 0:
        return
    tags = _normalize_stored_tags([raw_tag])
    parents = {t for t in tags if _is_parent_tag(t)}
    for parent in parents:
        parent_by_grade[grade][parent] += count
    for tag in tags:
        if not _is_attr_tag(tag):
            continue
        if tag.startswith("resolution_lifecycle:"):
            continue
        parent = _bind_attr_parent(tag, parents) or _infer_attr_parent_for_aggregate(tag)
        if parent is None:
            continue
        attr_by_grade[grade].setdefault(parent, Counter())[_attr_id(tag)] += count


def _issue_counter_maps() -> tuple[
    dict[str, Counter[str]],
    dict[str, Counter[str]],
    dict[str, Counter[str]],
    dict[str, Counter[str]],
    dict[str, Counter[str]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
]:
    return (
        {g: Counter() for g in _ISSUE_GRADE_ORDER},
        {g: Counter() for g in _ISSUE_GRADE_ORDER},
        {g: Counter() for g in _ISSUE_GRADE_ORDER},
        {g: Counter() for g in _ISSUE_GRADE_ORDER},
        {g: Counter() for g in _ISSUE_GRADE_ORDER},
        _empty_grade_attr_maps(),
        _empty_grade_attr_maps(),
        _empty_grade_attr_maps(),
        _empty_grade_attr_maps(),
        _empty_grade_attr_maps(),
    )


_GRADE_SEGMENTS = ("composite", "bot", "bot_closed", "bot_pre_handoff", "human")


def _empty_grade_distribution_block() -> dict[str, Any]:
    return {"slices": [], "na_count": 0}


def _score_bucket_sql(score_col: str) -> str:
    return f"""CASE
      WHEN {score_col} >= 90 THEN '90-100'
      WHEN {score_col} >= 75 THEN '75-89'
      WHEN {score_col} >= 50 THEN '50-74'
      WHEN {score_col} >= 1 THEN '1-49'
      ELSE '0'
    END"""


def _apply_grade_distribution_rows(
    out: dict[str, dict[str, Any]],
    rows: list[Any],
) -> None:
    for row in rows:
        segment = str(row.segment)
        block = out.get(segment)
        if block is None:
            continue
        grade = str(row.grade or LETTER_GRADE_NOT_GRADED)
        count = int(row.n or 0)
        if grade == LETTER_GRADE_NOT_GRADED:
            block["na_count"] += count
        else:
            block["slices"].append({"grade": grade, "count": count})
    for block in out.values():
        block["slices"].sort(key=lambda item: (-item["count"], item["grade"]))


async def _sql_grade_distribution(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> dict[str, dict[str, Any]]:
    """One index-friendly scan: all segment grade histograms + na_count."""
    q = text(
        """
        SELECT segment, grade, COUNT(*)::int AS n
        FROM responder_eval_runs r
        CROSS JOIN LATERAL (
          VALUES
            ('composite'::text, r.letter_grade),
            ('bot', r.bot_grade),
            ('bot_closed', r.bot_grade),
            ('bot_pre_handoff', r.bot_grade),
            ('human', r.human_grade)
        ) AS v(segment, grade)
        WHERE r.created_at >= :since AND r.created_at < :until
          AND r.eval_version = :ver
          AND r.eval_status = 'completed'
          AND (
            v.segment = 'composite'
            OR (v.segment = 'bot' AND r.bot_score IS NOT NULL)
            OR (v.segment = 'bot_closed' AND r.bot_score IS NOT NULL AND r.human_score IS NULL)
            OR (v.segment = 'bot_pre_handoff' AND r.bot_score IS NOT NULL AND r.human_score IS NOT NULL)
            OR (v.segment = 'human' AND r.human_score IS NOT NULL)
          )
        GROUP BY segment, grade
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    out = {key: _empty_grade_distribution_block() for key in _GRADE_SEGMENTS}
    _apply_grade_distribution_rows(out, rows)
    return out


async def _sql_score_buckets(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    comp_b = _score_bucket_sql("r.composite_score")
    bot_b = _score_bucket_sql("r.bot_score")
    human_b = _score_bucket_sql("r.human_score")
    q = text(
        f"""
        SELECT segment, bucket, COUNT(*)::int AS n
        FROM responder_eval_runs r
        CROSS JOIN LATERAL (
          VALUES
            ('composite'::text, {comp_b}),
            ('bot', {bot_b}),
            ('bot_closed', {bot_b}),
            ('bot_pre_handoff', {bot_b}),
            ('human', {human_b})
        ) AS v(segment, bucket)
        WHERE r.created_at >= :since AND r.created_at < :until
          AND r.eval_version = :ver
          AND r.eval_status = 'completed'
          AND v.bucket IS NOT NULL
          AND (
            v.segment = 'composite'
            OR (v.segment = 'bot' AND r.bot_score IS NOT NULL)
            OR (v.segment = 'bot_closed' AND r.bot_score IS NOT NULL AND r.human_score IS NULL)
            OR (v.segment = 'bot_pre_handoff' AND r.bot_score IS NOT NULL AND r.human_score IS NOT NULL)
            OR (v.segment = 'human' AND r.human_score IS NOT NULL)
          )
        GROUP BY segment, bucket
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    raw: dict[str, dict[str, int]] = {key: {} for key in _GRADE_SEGMENTS}
    for row in rows:
        seg = str(row.segment)
        if seg in raw:
            raw[seg][str(row.bucket)] = int(row.n or 0)
    return {
        key: {
            "buckets": [{"range": rng, "count": raw[key].get(rng, 0)} for rng in _SCORE_BUCKET_ORDER],
        }
        for key in _GRADE_SEGMENTS
    }


async def _sql_resolution_distribution(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    q = text(
        """
        SELECT segment, resolution, COUNT(*)::int AS n
        FROM responder_eval_runs r
        CROSS JOIN LATERAL (
          VALUES
            ('composite'::text, r.composite_resolution),
            ('bot', r.bot_resolution),
            ('bot_closed', r.bot_resolution),
            ('bot_pre_handoff', r.bot_resolution),
            ('human', r.human_resolution)
        ) AS v(segment, resolution)
        WHERE r.created_at >= :since AND r.created_at < :until
          AND r.eval_version = :ver
          AND r.eval_status = 'completed'
          AND (
            v.segment = 'composite'
            OR (v.segment = 'bot' AND r.bot_score IS NOT NULL)
            OR (v.segment = 'bot_closed' AND r.bot_score IS NOT NULL AND r.human_score IS NULL)
            OR (v.segment = 'bot_pre_handoff' AND r.bot_score IS NOT NULL AND r.human_score IS NOT NULL)
            OR (v.segment = 'human' AND r.human_score IS NOT NULL)
          )
        GROUP BY segment, resolution
        ORDER BY segment, n DESC
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        key: {"slices": []} for key in _GRADE_SEGMENTS
    }
    for row in rows:
        seg = str(row.segment)
        if seg in out:
            out[seg]["slices"].append({
                "resolution": str(row.resolution or "unknown"),
                "count": int(row.n or 0),
            })
    return out


async def _sql_all_issue_tag_counts(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> list[tuple[str, str, str, int]]:
    """One scan with per-segment unnest for C/D/E tag counts."""
    q = text(
        """
        SELECT segment, grade, tag, COUNT(*)::int AS n
        FROM responder_eval_runs r
        CROSS JOIN LATERAL (
          SELECT 'composite'::text AS segment, r.letter_grade AS grade, u.tag
          FROM unnest(r.composite_issues) AS u(tag)
          UNION ALL
          SELECT 'bot', r.bot_grade, u.tag
          FROM unnest(r.bot_issues) AS u(tag)
          WHERE r.bot_score IS NOT NULL
          UNION ALL
          SELECT 'bot_closed', r.bot_grade, u.tag
          FROM unnest(r.bot_issues) AS u(tag)
          WHERE r.bot_score IS NOT NULL AND r.human_score IS NULL
          UNION ALL
          SELECT 'bot_pre_handoff', r.bot_grade, u.tag
          FROM unnest(r.bot_issues) AS u(tag)
          WHERE r.bot_score IS NOT NULL AND r.human_score IS NOT NULL
          UNION ALL
          SELECT 'human', r.human_grade, u.tag
          FROM unnest(r.human_issues) AS u(tag)
          WHERE r.human_score IS NOT NULL
        ) tagged
        WHERE r.created_at >= :since AND r.created_at < :until
          AND r.eval_version = :ver
          AND r.eval_status = 'completed'
          AND tagged.grade IN ('C', 'D', 'E')
        GROUP BY segment, grade, tag
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    return [(str(r.segment), str(r.grade), str(r.tag), int(r.n or 0)) for r in rows]


async def _sql_all_resolution_lifecycle_attr_counts(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> list[tuple[str, str, str, str, int]]:
    q = text(
        """
        SELECT segment, grade, parent, attr_id, COUNT(*)::int AS n
        FROM (
          SELECT 'composite'::text AS segment,
                 r.letter_grade AS grade,
                 CASE
                   WHEN r.composite_issues @> ARRAY['resolution:no']::text[] THEN 'resolution:no'
                   WHEN r.composite_issues @> ARRAY['resolution:partial']::text[] THEN 'resolution:partial'
                 END AS parent,
                 substr(tag, 22) AS attr_id
          FROM responder_eval_runs r
          CROSS JOIN LATERAL unnest(r.composite_issues) AS u(tag)
          WHERE r.created_at >= :since AND r.created_at < :until
            AND r.eval_version = :ver
            AND r.eval_status = 'completed'
            AND r.letter_grade IN ('C', 'D', 'E')
            AND tag LIKE 'resolution_lifecycle:%'
            AND (
              r.composite_issues @> ARRAY['resolution:no']::text[]
              OR r.composite_issues @> ARRAY['resolution:partial']::text[]
            )
          UNION ALL
          SELECT 'bot', r.bot_grade,
                 CASE
                   WHEN r.bot_issues @> ARRAY['resolution:no']::text[] THEN 'resolution:no'
                   WHEN r.bot_issues @> ARRAY['resolution:partial']::text[] THEN 'resolution:partial'
                 END,
                 substr(tag, 22)
          FROM responder_eval_runs r
          CROSS JOIN LATERAL unnest(r.bot_issues) AS u(tag)
          WHERE r.created_at >= :since AND r.created_at < :until
            AND r.eval_version = :ver
            AND r.eval_status = 'completed'
            AND r.bot_score IS NOT NULL
            AND r.bot_grade IN ('C', 'D', 'E')
            AND tag LIKE 'resolution_lifecycle:%'
            AND (
              r.bot_issues @> ARRAY['resolution:no']::text[]
              OR r.bot_issues @> ARRAY['resolution:partial']::text[]
            )
          UNION ALL
          SELECT 'bot_closed', r.bot_grade,
                 CASE
                   WHEN r.bot_issues @> ARRAY['resolution:no']::text[] THEN 'resolution:no'
                   WHEN r.bot_issues @> ARRAY['resolution:partial']::text[] THEN 'resolution:partial'
                 END,
                 substr(tag, 22)
          FROM responder_eval_runs r
          CROSS JOIN LATERAL unnest(r.bot_issues) AS u(tag)
          WHERE r.created_at >= :since AND r.created_at < :until
            AND r.eval_version = :ver
            AND r.eval_status = 'completed'
            AND r.bot_score IS NOT NULL AND r.human_score IS NULL
            AND r.bot_grade IN ('C', 'D', 'E')
            AND tag LIKE 'resolution_lifecycle:%'
            AND (
              r.bot_issues @> ARRAY['resolution:no']::text[]
              OR r.bot_issues @> ARRAY['resolution:partial']::text[]
            )
          UNION ALL
          SELECT 'bot_pre_handoff', r.bot_grade,
                 CASE
                   WHEN r.bot_issues @> ARRAY['resolution:no']::text[] THEN 'resolution:no'
                   WHEN r.bot_issues @> ARRAY['resolution:partial']::text[] THEN 'resolution:partial'
                 END,
                 substr(tag, 22)
          FROM responder_eval_runs r
          CROSS JOIN LATERAL unnest(r.bot_issues) AS u(tag)
          WHERE r.created_at >= :since AND r.created_at < :until
            AND r.eval_version = :ver
            AND r.eval_status = 'completed'
            AND r.bot_score IS NOT NULL AND r.human_score IS NOT NULL
            AND r.bot_grade IN ('C', 'D', 'E')
            AND tag LIKE 'resolution_lifecycle:%'
            AND (
              r.bot_issues @> ARRAY['resolution:no']::text[]
              OR r.bot_issues @> ARRAY['resolution:partial']::text[]
            )
          UNION ALL
          SELECT 'human', r.human_grade,
                 CASE
                   WHEN r.human_issues @> ARRAY['resolution:no']::text[] THEN 'resolution:no'
                   WHEN r.human_issues @> ARRAY['resolution:partial']::text[] THEN 'resolution:partial'
                 END,
                 substr(tag, 22)
          FROM responder_eval_runs r
          CROSS JOIN LATERAL unnest(r.human_issues) AS u(tag)
          WHERE r.created_at >= :since AND r.created_at < :until
            AND r.eval_version = :ver
            AND r.eval_status = 'completed'
            AND r.human_score IS NOT NULL
            AND r.human_grade IN ('C', 'D', 'E')
            AND tag LIKE 'resolution_lifecycle:%'
            AND (
              r.human_issues @> ARRAY['resolution:no']::text[]
              OR r.human_issues @> ARRAY['resolution:partial']::text[]
            )
        ) lifecycle
        WHERE parent IS NOT NULL AND attr_id IS NOT NULL
        GROUP BY segment, grade, parent, attr_id
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    return [
        (str(r.segment), str(r.grade), str(r.parent), str(r.attr_id), int(r.n or 0))
        for r in rows
    ]


_POLICY_GUARD_DEDUPE_PAIRS = (
    ("guardrail:order_selection_before_action", "policy:order_selection"),
    ("guardrail:explain_absence_dont_repeat", "policy:explain_absence"),
)


async def _sql_policy_guardrail_dedupe_counts(
    db: AsyncSession,
    since: datetime,
    until: datetime,
) -> list[tuple[str, str, str, int]]:
    """Rows carrying both guardrail + policy echo tags, per segment and grade."""
    segments = (
        ("composite", "letter_grade", "composite_issues", ""),
        ("bot", "bot_grade", "bot_issues", "AND r.bot_score IS NOT NULL"),
        (
            "bot_closed",
            "bot_grade",
            "bot_issues",
            "AND r.bot_score IS NOT NULL AND r.human_score IS NULL",
        ),
        (
            "bot_pre_handoff",
            "bot_grade",
            "bot_issues",
            "AND r.bot_score IS NOT NULL AND r.human_score IS NOT NULL",
        ),
        ("human", "human_grade", "human_issues", "AND r.human_score IS NOT NULL"),
    )
    q = text(
        f"""
        SELECT segment, grade, policy_tag, SUM(n)::int AS n
        FROM (
          {' UNION ALL '.join(
            f"""
            SELECT '{segment}'::text AS segment, r.{grade_col} AS grade,
                   '{policy_tag}'::text AS policy_tag, COUNT(*)::int AS n
            FROM responder_eval_runs r
            WHERE r.created_at >= :since AND r.created_at < :until
              AND r.eval_version = :ver
              AND r.eval_status = 'completed'
              AND r.{grade_col} IN ('C', 'D', 'E')
              {extra}
              AND r.{issues_col} @> ARRAY['{guard_tag}', '{policy_tag}']::text[]
            GROUP BY r.{grade_col}
            """
            for segment, grade_col, issues_col, extra in segments
            for guard_tag, policy_tag in _POLICY_GUARD_DEDUPE_PAIRS
          )}
        ) dedupe
        GROUP BY segment, grade, policy_tag
        """
    )
    rows = (
        await db.execute(q, {"since": since, "until": until, "ver": EVAL_VERSION})
    ).all()
    return [(str(r.segment), str(r.grade), str(r.policy_tag), int(r.n or 0)) for r in rows]


def _nested_issue_counts_from_maps(
    composite_p: dict[str, Counter[str]],
    bot_p: dict[str, Counter[str]],
    bot_closed_p: dict[str, Counter[str]],
    bot_pre_p: dict[str, Counter[str]],
    human_p: dict[str, Counter[str]],
    composite_a: dict[str, dict[str, Counter[str]]],
    bot_a: dict[str, dict[str, Counter[str]]],
    bot_closed_a: dict[str, dict[str, Counter[str]]],
    bot_pre_a: dict[str, dict[str, Counter[str]]],
    human_a: dict[str, dict[str, Counter[str]]],
    *,
    limit: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    return {
        "composite": _top_n_nested_by_grade(composite_p, composite_a, limit=limit),
        "bot": _top_n_nested_by_grade(bot_p, bot_a, limit=limit),
        "bot_closed": _top_n_nested_by_grade(bot_closed_p, bot_closed_a, limit=limit),
        "bot_pre_handoff": _top_n_nested_by_grade(bot_pre_p, bot_pre_a, limit=limit),
        "human": _top_n_nested_by_grade(human_p, human_a, limit=limit),
    }


async def _count_issue_tags_sql(
    db: AsyncSession,
    since: datetime,
    until: datetime,
    *,
    limit: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    (
        composite_p,
        bot_p,
        bot_closed_p,
        bot_pre_p,
        human_p,
        composite_a,
        bot_a,
        bot_closed_a,
        bot_pre_a,
        human_a,
    ) = _issue_counter_maps()
    parent_maps = {
        "composite": composite_p,
        "bot": bot_p,
        "bot_closed": bot_closed_p,
        "bot_pre_handoff": bot_pre_p,
        "human": human_p,
    }
    attr_maps = {
        "composite": composite_a,
        "bot": bot_a,
        "bot_closed": bot_closed_a,
        "bot_pre_handoff": bot_pre_a,
        "human": human_a,
    }

    for segment, grade, tag, n in await _sql_all_issue_tag_counts(db, since, until):
        _accumulate_tag_count(
            parent_maps[segment], attr_maps[segment], grade=grade, raw_tag=tag, count=n
        )
    for segment, grade, parent, attr_id, n in await _sql_all_resolution_lifecycle_attr_counts(
        db, since, until
    ):
        if grade in _LOW_GRADES and n > 0:
            attr_maps[segment].setdefault(grade, {}).setdefault(parent, Counter())[attr_id] += n
    for segment, grade, policy_tag, n in await _sql_policy_guardrail_dedupe_counts(
        db, since, until
    ):
        if grade in _LOW_GRADES and n > 0:
            parent_maps[segment][grade][policy_tag] -= n
            if parent_maps[segment][grade][policy_tag] <= 0:
                del parent_maps[segment][grade][policy_tag]

    return _nested_issue_counts_from_maps(
        composite_p,
        bot_p,
        bot_closed_p,
        bot_pre_p,
        human_p,
        composite_a,
        bot_a,
        bot_closed_a,
        bot_pre_a,
        human_a,
        limit=limit,
    )


async def eval_summary(db: AsyncSession, *, days: int) -> dict[str, Any]:
    since, until = _window_bounds(days)
    graded = ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED
    run_stats = (
        await db.execute(
            select(
                func.count().label("n_eval"),
                func.count().filter(graded).label("n_graded"),
                func.count().filter(ResponderEvalRun.eval_status == RUN_STATUS_NOT_GRADED).label(
                    "n_not_graded"
                ),
                func.count().filter(graded, _BOT_PRESENT).label("n_bot"),
                func.count().filter(graded, _BOT_CLOSED).label("n_bot_closed"),
                func.count().filter(graded, _BOT_PRE_HANDOFF).label("n_bot_pre_handoff"),
                func.count()
                .filter(graded, _HUMAN_PRESENT)
                .label("n_human"),
                func.avg(ResponderEvalRun.composite_score).filter(graded).label("avg_score"),
                func.avg(ResponderEvalRun.bot_score).filter(graded, _BOT_PRESENT).label("avg_bot"),
                func.avg(ResponderEvalRun.bot_score).filter(graded, _BOT_CLOSED).label(
                    "avg_bot_closed"
                ),
                func.avg(ResponderEvalRun.bot_score).filter(graded, _BOT_PRE_HANDOFF).label(
                    "avg_bot_pre_handoff"
                ),
                func.avg(ResponderEvalRun.human_score)
                .filter(graded, _HUMAN_PRESENT)
                .label("avg_human"),
            ).where(*_window_filters(since, until))
        )
    ).one()
    dump_stats = (
        await db.execute(
            select(
                func.count()
                .filter(OrderRcaEvalDump.eval_status.in_(_QUEUE_STATUSES))
                .label("n_pending"),
                func.count()
                .filter(OrderRcaEvalDump.eval_status == EVAL_STATUS_WAITING)
                .label("n_waiting"),
                func.count()
                .filter(OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING)
                .label("n_processing"),
            )
        )
    ).one()
    graded_count = int(run_stats.n_graded or 0)
    bot_count = int(run_stats.n_bot or 0)
    bot_closed_count = int(run_stats.n_bot_closed or 0)
    bot_pre_count = int(run_stats.n_bot_pre_handoff or 0)
    human_count = int(run_stats.n_human or 0)
    return {
        "window_days": days,
        "evaluated": int(run_stats.n_eval or 0),
        "graded": graded_count,
        "not_graded": int(run_stats.n_not_graded or 0),
        "graded_bot_runs": bot_count,
        "graded_bot_closed_runs": bot_closed_count,
        "graded_bot_pre_handoff_runs": bot_pre_count,
        "graded_human_runs": human_count,
        "pending_dumps": int(dump_stats.n_pending or 0),
        "waiting_chats": int(dump_stats.n_waiting or 0),
        "processing_dumps": int(dump_stats.n_processing or 0),
        "avg_composite_score": _round_avg(run_stats.avg_score) if graded_count > 0 else None,
        "avg_bot_score": _round_avg(run_stats.avg_bot) if bot_count > 0 else None,
        "avg_bot_closed_score": (
            _round_avg(run_stats.avg_bot_closed) if bot_closed_count > 0 else None
        ),
        "avg_bot_pre_handoff_score": (
            _round_avg(run_stats.avg_bot_pre_handoff) if bot_pre_count > 0 else None
        ),
        "avg_human_score": _round_avg(run_stats.avg_human) if human_count > 0 else None,
    }


async def eval_grade_distribution(db: AsyncSession, *, days: int) -> dict[str, Any]:
    since, until = _window_bounds(days)
    blocks = await _sql_grade_distribution(db, since, until)
    return {"window_days": days, **blocks}


async def eval_timeseries(
    db: AsyncSession, *, days: int, granularity: Granularity = "day"
) -> dict[str, Any]:
    since, until = _window_bounds(days)
    trunc = "week" if granularity == "week" else "day"
    bucket = func.date_trunc(trunc, ResponderEvalRun.created_at).label("bucket")
    q = (
        select(
            bucket,
            func.count().label("total"),
            func.count().filter(_BOT_PRESENT).label("total_bot"),
            func.count().filter(_BOT_CLOSED).label("total_bot_closed"),
            func.count().filter(_BOT_PRE_HANDOFF).label("total_bot_pre_handoff"),
            func.count().filter(_HUMAN_PRESENT).label("total_human"),
            func.avg(ResponderEvalRun.composite_score).label("avg_score"),
            func.avg(ResponderEvalRun.bot_score).filter(_BOT_PRESENT).label("avg_bot"),
            func.avg(ResponderEvalRun.bot_score).filter(_BOT_CLOSED).label("avg_bot_closed"),
            func.avg(ResponderEvalRun.bot_score)
            .filter(_BOT_PRE_HANDOFF)
            .label("avg_bot_pre_handoff"),
            func.avg(ResponderEvalRun.human_score).filter(_HUMAN_PRESENT).label("avg_human"),
        )
        .where(
            *_window_filters(since, until),
            ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED,
        )
        .group_by(bucket)
        .order_by(bucket.asc())
    )
    points = []
    for row in (await db.execute(q)).all():
        if row.bucket is None:
            continue
        points.append({
            "bucket_start": row.bucket.isoformat(),
            "total": int(row.total or 0),
            "total_bot": int(row.total_bot or 0),
            "total_bot_closed": int(row.total_bot_closed or 0),
            "total_bot_pre_handoff": int(row.total_bot_pre_handoff or 0),
            "total_human": int(row.total_human or 0),
            "avg_score": _round_avg(row.avg_score),
            "avg_bot_score": _round_avg(row.avg_bot),
            "avg_bot_closed_score": _round_avg(row.avg_bot_closed),
            "avg_bot_pre_handoff_score": _round_avg(row.avg_bot_pre_handoff),
            "avg_human_score": _round_avg(row.avg_human),
        })
    return {"window_days": days, "granularity": granularity, "points": points}


async def eval_score_buckets(db: AsyncSession, *, days: int) -> dict[str, Any]:
    since, until = _window_bounds(days)
    blocks = await _sql_score_buckets(db, since, until)
    return {"window_days": days, **blocks}


async def eval_resolution_distribution(db: AsyncSession, *, days: int) -> dict[str, Any]:
    since, until = _window_bounds(days)
    blocks = await _sql_resolution_distribution(db, since, until)
    return {"window_days": days, **blocks}


def _count_issue_tags(
    rows: list[Any],
    *,
    limit: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    composite_p: dict[str, Counter[str]] = {g: Counter() for g in _ISSUE_GRADE_ORDER}
    bot_p: dict[str, Counter[str]] = {g: Counter() for g in _ISSUE_GRADE_ORDER}
    bot_closed_p: dict[str, Counter[str]] = {g: Counter() for g in _ISSUE_GRADE_ORDER}
    bot_pre_p: dict[str, Counter[str]] = {g: Counter() for g in _ISSUE_GRADE_ORDER}
    human_p: dict[str, Counter[str]] = {g: Counter() for g in _ISSUE_GRADE_ORDER}
    composite_a = _empty_grade_attr_maps()
    bot_a = _empty_grade_attr_maps()
    bot_closed_a = _empty_grade_attr_maps()
    bot_pre_a = _empty_grade_attr_maps()
    human_a = _empty_grade_attr_maps()

    for row in rows:
        _bump_issues_nested(
            composite_p, composite_a, grade=row.letter_grade, issues=row.composite_issues
        )
        if row.bot_score is not None:
            _bump_issues_nested(bot_p, bot_a, grade=row.bot_grade, issues=row.bot_issues)
            if row.human_score is None:
                _bump_issues_nested(
                    bot_closed_p, bot_closed_a, grade=row.bot_grade, issues=row.bot_issues
                )
            else:
                _bump_issues_nested(
                    bot_pre_p, bot_pre_a, grade=row.bot_grade, issues=row.bot_issues
                )
        if row.human_score is not None:
            _bump_issues_nested(
                human_p, human_a, grade=row.human_grade, issues=row.human_issues
            )
    return {
        "composite": _top_n_nested_by_grade(composite_p, composite_a, limit=limit),
        "bot": _top_n_nested_by_grade(bot_p, bot_a, limit=limit),
        "bot_closed": _top_n_nested_by_grade(bot_closed_p, bot_closed_a, limit=limit),
        "bot_pre_handoff": _top_n_nested_by_grade(bot_pre_p, bot_pre_a, limit=limit),
        "human": _top_n_nested_by_grade(human_p, human_a, limit=limit),
    }


async def eval_top_issues(db: AsyncSession, *, days: int, limit: int = 10) -> dict[str, Any]:
    """Top issue tags for C/D/E, keyed by grade. ``limit`` is per grade (not pooled)."""
    since, until = _window_bounds(days)
    counts = await _count_issue_tags_sql(db, since, until, limit=limit)
    return {"window_days": days, **counts}


async def eval_charts(
    db: AsyncSession,
    *,
    days: int,
    granularity: Granularity = "day",
    issues_limit: int = 10,
) -> dict[str, Any]:
    """Single round-trip bundle for dashboard charts (one DB session)."""
    summary = await eval_summary(db, days=days)
    grades = await eval_grade_distribution(db, days=days)
    timeseries = await eval_timeseries(db, days=days, granularity=granularity)
    buckets = await eval_score_buckets(db, days=days)
    resolutions = await eval_resolution_distribution(db, days=days)
    top_issues = await eval_top_issues(db, days=days, limit=issues_limit)
    return {
        "summary": summary,
        "grades": grades,
        "timeseries": timeseries,
        "buckets": buckets,
        "resolutions": resolutions,
        "top_issues": top_issues,
    }


async def eval_pending_dumps(
    db: AsyncSession, *, limit: int, offset: int
) -> dict[str, Any]:
    q = (
        select(OrderRcaEvalDump)
        .options(
            load_only(
                OrderRcaEvalDump.id,
                OrderRcaEvalDump.chat_id,
                OrderRcaEvalDump.order_id,
                OrderRcaEvalDump.run_id,
                OrderRcaEvalDump.eval_status,
                OrderRcaEvalDump.retry_count,
                OrderRcaEvalDump.last_error,
                OrderRcaEvalDump.created_at,
                OrderRcaEvalDump.updated_at,
            )
        )
        .where(OrderRcaEvalDump.eval_status.in_(_QUEUE_STATUSES))
        .order_by(OrderRcaEvalDump.created_at.asc(), OrderRcaEvalDump.id.asc())
        .offset(offset)
        .limit(limit)
    )
    cq = select(func.count()).where(OrderRcaEvalDump.eval_status.in_(_QUEUE_STATUSES))
    rows = list((await db.execute(q)).scalars().all())
    total = int((await db.scalar(cq)) or 0)
    return {
        "items": [
            {
                "chat_id": r.chat_id,
                "order_id": r.order_id,
                "run_id": r.run_id,
                "eval_status": r.eval_status,
                "retry_count": int(r.retry_count or 0),
                "last_error": r.last_error,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
    }
