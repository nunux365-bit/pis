"""DB integration tests for responder eval dashboard aggregates."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.agents.responder_eval.constants import EVAL_VERSION, RUN_STATUS_COMPLETED, RUN_STATUS_NOT_GRADED
from app.agents.responder_eval.dashboard import (
    eval_grade_distribution,
    eval_summary,
    eval_timeseries,
    eval_top_issues,
)
from app.agents.responder_eval.models import ResponderEvalRun
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT
from app.agents.responder_eval.denormalized import resolution_row_fields
from app.agents.responder_eval.issue_tags import compute_issue_tags, eval_dict_for_issue_tags
from app.services.responder_eval_dashboard import list_responder_eval_runs


def _chat_id() -> str:
    return f"dash-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def pg_or_skip():
    from app.db.session import AsyncSessionLocal

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            async with AsyncSessionLocal() as s:
                await s.execute(select(ResponderEvalRun).limit(1))
            return
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.15)
    pytest.skip(f"Postgres / responder_eval schema not available: {last_exc}")


async def _scrub(chat_ids: list[str]) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        async with s.begin():
            for chat_id in chat_ids:
                await s.execute(delete(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id))


async def _insert_run(
    chat_id: str,
    *,
    letter_grade: str,
    bot_grade: str,
    human_grade: str,
    bot_score: int | None,
    human_score: int | None,
    composite_score: int | None,
    eval_json: dict,
    eval_status: str = RUN_STATUS_COMPLETED,
    order_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    from app.db.session import AsyncSessionLocal

    merged = eval_dict_for_issue_tags(
        eval_json,
        letter_grade=letter_grade,
        bot_grade=bot_grade,
        human_grade=human_grade,
        bot_score=bot_score,
        human_score=human_score,
    )
    denorm = {
        **resolution_row_fields(merged),
        **compute_issue_tags(merged),
    }
    now = created_at or datetime.now(UTC)
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(
                ResponderEvalRun(
                    chat_id=chat_id,
                    order_id=order_id or f"DASH-TEST-{uuid.uuid4().hex[:8]}",
                    rca_run_id="run-test",
                    eval_version=EVAL_VERSION,
                    composite_score=composite_score,
                    letter_grade=letter_grade,
                    bot_score=bot_score,
                    human_score=human_score,
                    bot_grade=bot_grade,
                    human_grade=human_grade,
                    eval_status=eval_status,
                    eval_json=eval_json,
                    created_at=now,
                    updated_at=now,
                    **denorm,
                )
            )


def _grade_counts(out: dict, segment: str) -> dict[str, int]:
    return {s["grade"]: s["count"] for s in out[segment]["slices"]}


def _issue_counts(out: dict, segment: str, grade: str | None = None) -> dict[str, int]:
    block = out[segment]
    if grade is not None:
        return {i["issue"]: i["count"] for i in block.get(grade, [])}
    merged: dict[str, int] = {}
    for grade_key in ("E", "D", "C"):
        for item in block.get(grade_key, []):
            merged[item["issue"]] = merged.get(item["issue"], 0) + int(item["count"])
    return merged


@pytest.mark.asyncio
async def test_eval_top_issues_db_counts_by_segment(pg_or_skip):
    c1, c2 = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_top_issues(s, days=30, limit=20)
            before_composite = _issue_counts(before, "composite")
            before_bot = _issue_counts(before, "bot")

        await _insert_run(
            c1,
            letter_grade="C",
            bot_grade="B",
            human_grade="A",
            bot_score=80,
            human_score=95,
            composite_score=55,
            created_at=now - timedelta(minutes=5),
            eval_json={
                "violations": [{"rule": "no_pii", "turns": [1]}],
                "resolution": "no",
                "segment_evals": {
                    SEGMENT_BOT: {"graded": True, "turn_indexes": [1], "resolution": "partial"},
                    SEGMENT_HUMAN_AGENT: {"graded": True, "turn_indexes": [2], "resolution": "yes"},
                },
            },
        )
        await _insert_run(
            c2,
            letter_grade="B",
            bot_grade="D",
            human_grade="-",
            bot_score=40,
            human_score=None,
            composite_score=75,
            created_at=now - timedelta(minutes=4),
            eval_json={
                "violations": [{"rule": "no_links", "turns": [3]}],
                "segment_evals": {
                    SEGMENT_BOT: {
                        "graded": True,
                        "turn_indexes": [3],
                        "guardrail_violations": 1,
                        "resolution": "no",
                    },
                },
            },
        )

        async with AsyncSessionLocal() as s:
            after = await eval_top_issues(s, days=30, limit=20)
            after_composite = _issue_counts(after, "composite")
            after_bot = _issue_counts(after, "bot")

        assert after_composite.get("resolution:no", 0) - before_composite.get("resolution:no", 0) == 1
        assert after_bot.get("guardrail:no_links", 0) - before_bot.get("guardrail:no_links", 0) == 1
        assert after_bot.get("resolution:no", 0) - before_bot.get("resolution:no", 0) == 1
        assert (
            _issue_counts(after, "composite", "C").get("resolution:no", 0)
            - _issue_counts(before, "composite", "C").get("resolution:no", 0)
            == 1
        )
        assert (
            _issue_counts(after, "bot_closed", "D").get("guardrail:no_links", 0)
            - _issue_counts(before, "bot_closed", "D").get("guardrail:no_links", 0)
            == 1
        )
    finally:
        await _scrub([c1, c2])


@pytest.mark.asyncio
async def test_eval_top_issues_ignores_eval_json_without_denormalized_tags(pg_or_skip):
    """Charts read issue arrays only; rubric JSON is not a fallback source."""
    chat_id = _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            async with s.begin():
                s.add(
                    ResponderEvalRun(
                        chat_id=chat_id,
                        order_id="NO-FALLBACK",
                        rca_run_id="run-test",
                        eval_version=EVAL_VERSION,
                        composite_score=45,
                        letter_grade="D",
                        bot_grade="-",
                        human_grade="-",
                        bot_score=None,
                        human_score=None,
                        eval_status=RUN_STATUS_COMPLETED,
                        eval_json={
                            "resolution": "no",
                            "violations": [{"rule": "no_pii", "turns": [1]}],
                        },
                        composite_issues=[],
                        bot_issues=[],
                        human_issues=[],
                        created_at=now,
                        updated_at=now,
                    )
                )
            out = await eval_top_issues(s, days=30, limit=20)
        assert _issue_counts(out, "composite").get("guardrail:no_pii", 0) == 0
    finally:
        await _scrub([chat_id])


@pytest.mark.asyncio
async def test_eval_grade_distribution_db_segments(pg_or_skip):
    c1, c2 = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_grade_distribution(s, days=7)
            before_composite = _grade_counts(before, "composite")
            before_bot = _grade_counts(before, "bot")
            before_human = _grade_counts(before, "human")

        await _insert_run(
            c1,
            letter_grade="C",
            bot_grade="B",
            human_grade="D",
            bot_score=82,
            human_score=45,
            composite_score=60,
            created_at=now - timedelta(minutes=3),
            eval_json={"resolution": "partial"},
        )
        await _insert_run(
            c2,
            letter_grade="A",
            bot_grade="-",
            human_grade="A",
            bot_score=None,
            human_score=88,
            composite_score=90,
            created_at=now - timedelta(minutes=2),
            eval_json={"resolution": "yes"},
        )

        async with AsyncSessionLocal() as s:
            after = await eval_grade_distribution(s, days=7)
            after_composite = _grade_counts(after, "composite")
            after_bot = _grade_counts(after, "bot")
            after_human = _grade_counts(after, "human")

        assert after_composite.get("C", 0) - before_composite.get("C", 0) == 1
        assert after_composite.get("A", 0) - before_composite.get("A", 0) == 1
        assert after_bot.get("B", 0) - before_bot.get("B", 0) == 1
        assert after_human.get("D", 0) - before_human.get("D", 0) == 1
        assert after_human.get("A", 0) - before_human.get("A", 0) == 1
    finally:
        await _scrub([c1, c2])


@pytest.mark.asyncio
async def test_eval_grade_distribution_na_count_matches_list_filter(pg_or_skip):
    c1 = _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_grade_distribution(s, days=7)
            _, before_list_na = await list_responder_eval_runs(
                s, limit=200, offset=0, days=7, grade="-", segment="bot_closed"
            )

        await _insert_run(
            c1,
            letter_grade="B",
            bot_grade="-",
            human_grade="-",
            bot_score=55,
            human_score=None,
            composite_score=70,
            eval_status=RUN_STATUS_COMPLETED,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "yes", "segment_evals": {"bot": {"graded": False}}},
        )

        async with AsyncSessionLocal() as s:
            after = await eval_grade_distribution(s, days=7)
            _, after_list_na = await list_responder_eval_runs(
                s, limit=200, offset=0, days=7, grade="-", segment="bot_closed"
            )

        assert after["bot_closed"]["na_count"] - before["bot_closed"]["na_count"] == 1
        assert after_list_na - before_list_na == 1
    finally:
        await _scrub([c1])


@pytest.mark.asyncio
async def test_eval_grade_distribution_na_ignores_not_graded_status(pg_or_skip):
    """na_count is graded-only — not_graded runs with '-' do not count."""
    c1 = _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_grade_distribution(s, days=7)

        await _insert_run(
            c1,
            letter_grade="-",
            bot_grade="-",
            human_grade="A",
            bot_score=None,
            human_score=90,
            composite_score=None,
            eval_status=RUN_STATUS_NOT_GRADED,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "not_applicable", "graded": False},
        )

        async with AsyncSessionLocal() as s:
            after = await eval_grade_distribution(s, days=7)

        assert after["composite"]["na_count"] == before["composite"]["na_count"]
        assert after["bot"]["na_count"] == before["bot"]["na_count"]
    finally:
        await _scrub([c1])


@pytest.mark.asyncio
async def test_eval_bot_cohort_grade_distribution(pg_or_skip):
    c_closed, c_pre = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_grade_distribution(s, days=7)
            before_closed = _grade_counts(before, "bot_closed")
            before_pre = _grade_counts(before, "bot_pre_handoff")

        await _insert_run(
            c_closed,
            letter_grade="B",
            bot_grade="C",
            human_grade="-",
            bot_score=60,
            human_score=None,
            composite_score=70,
            created_at=now - timedelta(minutes=2),
            eval_json={"resolution": "yes"},
        )
        await _insert_run(
            c_pre,
            letter_grade="B",
            bot_grade="D",
            human_grade="A",
            bot_score=40,
            human_score=90,
            composite_score=65,
            created_at=now - timedelta(minutes=1),
            eval_json={
                "resolution": "partial",
                "segment_evals": {
                    SEGMENT_BOT: {"graded": True, "resolution": "no"},
                    SEGMENT_HUMAN_AGENT: {"graded": True, "resolution": "yes"},
                },
            },
        )

        async with AsyncSessionLocal() as s:
            after = await eval_grade_distribution(s, days=7)
            after_closed = _grade_counts(after, "bot_closed")
            after_pre = _grade_counts(after, "bot_pre_handoff")
            rows_closed, _ = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, grade="C", segment="bot_closed"
            )
            rows_pre, _ = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, grade="D", segment="bot_pre_handoff"
            )

        assert after_closed.get("C", 0) - before_closed.get("C", 0) == 1
        assert after_pre.get("D", 0) - before_pre.get("D", 0) == 1
        assert after_closed.get("D", 0) - before_closed.get("D", 0) == 0
        assert after_pre.get("C", 0) - before_pre.get("C", 0) == 0
        assert any(r.chat_id == c_closed for r in rows_closed)
        assert any(r.chat_id == c_pre for r in rows_pre)
        assert all(r.chat_id != c_pre for r in rows_closed)
        assert all(r.chat_id != c_closed for r in rows_pre)

        async with AsyncSessionLocal() as s:
            summary = await eval_summary(s, days=7)
        # Partition: every graded bot chat is exactly closed or pre–hand-off.
        assert (
            summary["graded_bot_closed_runs"] + summary["graded_bot_pre_handoff_runs"]
            == summary["graded_bot_runs"]
        )
    finally:
        await _scrub([c_closed, c_pre])


@pytest.mark.asyncio
async def test_list_human_segment_scopes_to_human_cohort(pg_or_skip):
    c_human, c_bot_only = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        await _insert_run(
            c_human,
            letter_grade="B",
            bot_grade="C",
            human_grade="A",
            bot_score=55,
            human_score=90,
            composite_score=70,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "yes"},
        )
        await _insert_run(
            c_bot_only,
            letter_grade="B",
            bot_grade="B",
            human_grade="-",
            bot_score=80,
            human_score=None,
            composite_score=80,
            created_at=now,
            eval_json={"resolution": "yes"},
        )
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            rows, _ = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, segment="human"
            )
        assert any(r.chat_id == c_human for r in rows)
        assert all(r.chat_id != c_bot_only for r in rows)
    finally:
        await _scrub([c_human, c_bot_only])


@pytest.mark.asyncio
async def test_eval_timeseries_db_segment_volumes(pg_or_skip):
    c1, c2 = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before = await eval_timeseries(s, days=7, granularity="day")
            before_bot = sum(p["total_bot"] for p in before["points"])
            before_human = sum(p["total_human"] for p in before["points"])

        await _insert_run(
            c1,
            letter_grade="B",
            bot_grade="B",
            human_grade="-",
            bot_score=80,
            human_score=None,
            composite_score=80,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "yes"},
        )
        await _insert_run(
            c2,
            letter_grade="B",
            bot_grade="-",
            human_grade="C",
            bot_score=None,
            human_score=55,
            composite_score=70,
            created_at=now,
            eval_json={"resolution": "partial"},
        )

        async with AsyncSessionLocal() as s:
            after = await eval_timeseries(s, days=7, granularity="day")
            after_bot = sum(p["total_bot"] for p in after["points"])
            after_human = sum(p["total_human"] for p in after["points"])

        assert after_bot - before_bot >= 1
        assert after_human - before_human >= 1
    finally:
        await _scrub([c1, c2])


@pytest.mark.asyncio
async def test_list_runs_segment_grade_filter_db(pg_or_skip):
    c1, c2 = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        await _insert_run(
            c1,
            letter_grade="C",
            bot_grade="D",
            human_grade="A",
            bot_score=42,
            human_score=90,
            composite_score=50,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "no"},
        )
        await _insert_run(
            c2,
            letter_grade="C",
            bot_grade="B",
            human_grade="C",
            bot_score=85,
            human_score=60,
            composite_score=55,
            created_at=now - timedelta(minutes=2),
            eval_json={"resolution": "partial"},
        )

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            rows, total = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, grade="D", segment="bot"
            )

        assert total >= 1
        assert any(r.chat_id == c1 for r in rows)
        assert all(r.bot_grade == "D" for r in rows)
    finally:
        await _scrub([c1, c2])


@pytest.mark.asyncio
async def test_list_date_window_excludes_old_runs(pg_or_skip):
    c1 = _chat_id()
    try:
        await _insert_run(
            c1,
            letter_grade="C",
            bot_grade="C",
            human_grade="C",
            bot_score=50,
            human_score=50,
            composite_score=50,
            created_at=datetime.now(UTC) - timedelta(days=40),
            eval_json={"resolution": "no"},
        )
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            rows, total = await list_responder_eval_runs(s, limit=50, offset=0, days=7)
        assert all(r.chat_id != c1 for r in rows)
        assert not any(r.chat_id == c1 for r in rows)

        async with AsyncSessionLocal() as s:
            _, total_90 = await list_responder_eval_runs(s, limit=50, offset=0, days=90)
        async with AsyncSessionLocal() as s:
            rows_90, _ = await list_responder_eval_runs(
                s, limit=200, offset=0, days=90, grade="C", segment="composite"
            )
        assert any(r.chat_id == c1 for r in rows_90)
        assert total_90 >= 1
    finally:
        await _scrub([c1])


@pytest.mark.asyncio
async def test_list_eval_status_and_search_filters(pg_or_skip):
    c1, c2 = _chat_id(), _chat_id()
    order_prefix = f"PO-FILTER-{uuid.uuid4().hex[:6]}"
    now = datetime.now(UTC)
    try:
        await _insert_run(
            c1,
            letter_grade="-",
            bot_grade="-",
            human_grade="-",
            bot_score=None,
            human_score=None,
            composite_score=None,
            eval_status=RUN_STATUS_NOT_GRADED,
            order_id=f"{order_prefix}-A",
            created_at=now,
            eval_json={"graded": False},
        )
        await _insert_run(
            c2,
            letter_grade="B",
            bot_grade="B",
            human_grade="B",
            bot_score=80,
            human_score=80,
            composite_score=80,
            order_id=f"{order_prefix}-B",
            created_at=now,
            eval_json={"resolution": "yes"},
        )
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            rows, total = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, eval_status=RUN_STATUS_NOT_GRADED
            )
        assert any(r.chat_id == c1 for r in rows)
        assert all(r.eval_status == RUN_STATUS_NOT_GRADED for r in rows)

        async with AsyncSessionLocal() as s:
            rows, total = await list_responder_eval_runs(
                s, limit=50, offset=0, days=7, search=order_prefix
            )
        assert total >= 2
        assert {r.chat_id for r in rows} >= {c1, c2}
    finally:
        await _scrub([c1, c2])


@pytest.mark.asyncio
async def test_grade_slice_count_matches_list_for_composite(pg_or_skip):
    c1 = _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            before_dist = await eval_grade_distribution(s, days=7)
            before_slice = _grade_counts(before_dist, "composite").get("B", 0)
            _, before_list = await list_responder_eval_runs(
                s, limit=500, offset=0, days=7, grade="B", segment="composite"
            )

        await _insert_run(
            c1,
            letter_grade="B",
            bot_grade="B",
            human_grade="B",
            bot_score=82,
            human_score=82,
            composite_score=82,
            created_at=now,
            eval_json={"resolution": "yes"},
        )

        async with AsyncSessionLocal() as s:
            after_dist = await eval_grade_distribution(s, days=7)
            after_slice = _grade_counts(after_dist, "composite").get("B", 0)
            _, after_list = await list_responder_eval_runs(
                s, limit=500, offset=0, days=7, grade="B", segment="composite"
            )

        assert after_slice - before_slice == 1
        assert after_list - before_list == 1
    finally:
        await _scrub([c1])


@pytest.mark.asyncio
async def test_count_issue_tags_sql_matches_row_aggregation(pg_or_skip):
    """SQL unnest aggregation must match legacy per-row bump (chart accuracy)."""
    from sqlalchemy import and_, or_, select

    from app.agents.responder_eval.dashboard import (
        _count_issue_tags,
        _count_issue_tags_sql,
        _graded_filters,
        _window_bounds,
        _window_filters,
    )
    from app.db.session import AsyncSessionLocal

    since, until = _window_bounds(30)
    low_grade_filter = or_(
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
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                select(
                    ResponderEvalRun.composite_issues,
                    ResponderEvalRun.bot_issues,
                    ResponderEvalRun.human_issues,
                    ResponderEvalRun.letter_grade,
                    ResponderEvalRun.bot_grade,
                    ResponderEvalRun.human_grade,
                    ResponderEvalRun.bot_score,
                    ResponderEvalRun.human_score,
                ).where(*_window_filters(since, until), *_graded_filters(), low_grade_filter)
            )
        ).all()
        row_counts = _count_issue_tags(rows, limit=10)
        sql_counts = await _count_issue_tags_sql(s, since, until, limit=10)
    assert sql_counts == row_counts


@pytest.mark.asyncio
async def test_list_reason_filter_matches_issue_tag(pg_or_skip):
    c_match, c_other = _chat_id(), _chat_id()
    now = datetime.now(UTC)
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            _, before = await list_responder_eval_runs(
                s,
                limit=50,
                offset=0,
                days=7,
                segment="composite",
                reason="traceability:unsupported_refund",
            )

        await _insert_run(
            c_match,
            letter_grade="E",
            bot_grade="E",
            human_grade="-",
            bot_score=10,
            human_score=None,
            composite_score=10,
            created_at=now,
            eval_json={
                "resolution": "no",
                "traceability_pass": False,
                "traceability_failure_reasons": ["unsupported_refund"],
            },
        )
        await _insert_run(
            c_other,
            letter_grade="E",
            bot_grade="E",
            human_grade="-",
            bot_score=12,
            human_score=None,
            composite_score=12,
            created_at=now - timedelta(minutes=1),
            eval_json={"resolution": "partial"},
        )

        async with AsyncSessionLocal() as s:
            rows, total = await list_responder_eval_runs(
                s,
                limit=50,
                offset=0,
                days=7,
                segment="composite",
                reason="traceability:unsupported_refund",
            )

        assert total - before == 1
        assert {r.chat_id for r in rows} >= {c_match}
        assert c_other not in {r.chat_id for r in rows}
    finally:
        await _scrub([c_match, c_other])
