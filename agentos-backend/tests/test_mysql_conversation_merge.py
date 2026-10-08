"""Conversation-level MySQL transcript merge helpers."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.compliance_call.batch_item import ComplianceBatchItem
from app.agents.compliance_call.fingerprint import (
    make_ingest_fingerprint_mysql,
    make_ingest_fingerprint_mysql_conversation,
)
from app.agents.compliance_call.mysql_conversation import (
    collapse_mysql_items_by_conversation,
    merge_transcript_parts,
    mysql_conversation_skip_reason,
)
from app.agents.compliance_call.fingerprint import make_ingest_fingerprint_mysql_conversation
from app.agents.compliance_call.run import _mysql_ingest_fingerprint
from app.config.settings import settings


def test_conversation_fingerprint_differs_from_call() -> None:
    a = make_ingest_fingerprint_mysql(mysql_call_id=100)
    b = make_ingest_fingerprint_mysql_conversation(conversation_id=724540)
    c = make_ingest_fingerprint_mysql_conversation(conversation_id=724540)
    assert a != b
    assert b == c


def test_merge_transcript_parts_ordered() -> None:
    canon, grad = merge_transcript_parts(
        [
            (10, "first", "first g"),
            (20, "second", "second g"),
        ]
    )
    assert "--- call 10 ---" in canon
    assert "--- call 20 ---" in canon
    assert canon.index("10") < canon.index("20")
    assert grad.index("second g") > grad.index("first g")


def test_mysql_ingest_fingerprint_uses_conversation_when_legs_present() -> None:
    item = ComplianceBatchItem(
        source="mysql_call",
        mysql_call_id=12968,
        mysql_second_opinion_conversation_id=724540,
        mysql_call_legs=[{"mysql_call_id": 12967}, {"mysql_call_id": 12968}],
        filename="x",
    )
    fp = _mysql_ingest_fingerprint(item)
    assert fp == make_ingest_fingerprint_mysql_conversation(conversation_id=724540)
    assert fp != make_ingest_fingerprint_mysql(mysql_call_id=12968)


@pytest.mark.asyncio
async def test_collapse_mysql_items_groups_by_conversation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "compliance_mysql_merge_conversation_transcripts", True)
    a = ComplianceBatchItem(
        source="mysql_call",
        mysql_call_id=12967,
        mysql_second_opinion_conversation_id=724540,
        mysql_call_updated_at="2026-05-01 10:00:00",
        doctor_slug="d",
        doctor_name="Dr",
    )
    b = ComplianceBatchItem(
        source="mysql_call",
        mysql_call_id=12968,
        mysql_second_opinion_conversation_id=724540,
        mysql_call_updated_at="2026-05-01 11:00:00",
        doctor_slug="d",
        doctor_name="Dr",
    )
    fake_legs = [
        {"mysql_call_id": 12967, "mysql_room_name": None, "mysql_metadata": {}, "mysql_call_updated_at": "t1"},
        {"mysql_call_id": 12968, "mysql_room_name": None, "mysql_metadata": {}, "mysql_call_updated_at": "t2"},
    ]
    with patch(
        "app.agents.compliance_call.mysql_conversation.fetch_mysql_legs_for_conversation",
        new_callable=AsyncMock,
        return_value=fake_legs,
    ):
        out = await collapse_mysql_items_by_conversation([a, b])
    assert len(out) == 1
    assert out[0].mysql_second_opinion_conversation_id == 724540
    assert out[0].mysql_call_id == 12968
    assert len(out[0].mysql_call_legs) == 2


@pytest.mark.asyncio
async def test_mysql_conversation_skip_reason_conv_fingerprint_done() -> None:
    conv_id = 724540
    conv_fp = make_ingest_fingerprint_mysql_conversation(conversation_id=conv_id)
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    conv_done_result = MagicMock()
    conv_done_result.scalar_one_or_none.return_value = uuid.uuid4()
    legacy_result = MagicMock()
    legacy_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[conv_done_result, legacy_result])

    reason = await mysql_conversation_skip_reason(
        session, conversation_id=conv_id, leg_call_ids=[1, 2]
    )
    assert reason == "conversation_already_completed"


@pytest.mark.asyncio
async def test_mysql_conversation_skip_reason_legacy_conv_in_input_data() -> None:
    conv_id = 724540
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    conv_done_result = MagicMock()
    conv_done_result.scalar_one_or_none.return_value = None
    legacy_result = MagicMock()
    legacy_result.scalar_one_or_none.return_value = uuid.uuid4()
    session.execute = AsyncMock(side_effect=[conv_done_result, legacy_result])

    reason = await mysql_conversation_skip_reason(
        session, conversation_id=conv_id, leg_call_ids=[12967, 12968]
    )
    assert reason == "conversation_already_scored"


@pytest.mark.asyncio
async def test_mysql_conversation_skip_reason_all_legs_done() -> None:
    conv_id = 724540
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=2)
    conv_done_result = MagicMock()
    conv_done_result.scalar_one_or_none.return_value = None
    legacy_result = MagicMock()
    legacy_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[conv_done_result, legacy_result])

    reason = await mysql_conversation_skip_reason(
        session, conversation_id=conv_id, leg_call_ids=[12967, 12968]
    )
    assert reason == "all_calls_already_completed"


@pytest.mark.asyncio
async def test_mysql_conversation_skip_reason_none_when_fresh() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    empty = MagicMock()
    empty.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[empty, empty])

    reason = await mysql_conversation_skip_reason(
        session, conversation_id=999, leg_call_ids=[1]
    )
    assert reason is None


@pytest.mark.asyncio
async def test_ingest_merged_legs_delegates_to_transcribe_mysql_legs_merged() -> None:
    import uuid

    from app.agents.compliance_call.graph import _node_ingest

    with patch(
        "app.agents.compliance_call.graph.transcribe_mysql_legs_merged",
        new_callable=AsyncMock,
        return_value={
            "transcript_text": "merged canon",
            "grading_transcript": "merged grad",
            "deepgram_summary": {"duration": 10.0},
            "mysql_merged_call_ids": [1, 2],
            "mysql_skipped_call_ids": [],
        },
    ) as mock_merge:
        out = await _node_ingest(
            {
                "ingest_source": "mysql_call",
                "mysql_call_legs": [{"mysql_call_id": 1}, {"mysql_call_id": 2}],
                "workflow_run_id": str(uuid.uuid4()),
                "doctor_slug": "x",
                "doctor_name": "Dr X",
                "mysql_call_id": 2,
                "deepgram_client": MagicMock(),
                "openai_client": MagicMock(),
            }
        )
    mock_merge.assert_awaited_once()
    assert out.get("mysql_transcript_merged") is True
    assert out.get("transcript_text") == "merged canon"


@pytest.mark.asyncio
async def test_graph_normalize_skips_when_mysql_transcript_merged() -> None:
    from app.agents.compliance_call.graph import _node_normalize, _node_transcribe

    assert await _node_normalize({"mysql_transcript_merged": True, "source_media_path": ""}) == {}
    assert await _node_transcribe({"mysql_transcript_merged": True, "normalized_wav_path": "/x"}) == {}
