"""Regression tests for SmartQnA clarification flows (async migration safety)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.optimus.smartqna.agent import (
    handle_clarification,
    handle_clarification_followup,
)


@pytest.mark.asyncio
@patch("app.agents.optimus.smartqna.agent._semantic_document_search", new_callable=AsyncMock)
async def test_handle_clarification_awaits_semantic_search(mock_search):
    mock_search.return_value = [
        {"filename": "leave_policy.pdf", "score": 0.85},
        {"filename": "noise.pdf", "score": 0.1},
    ]

    result = await handle_clarification("leave stuff", doc_catalog=[])

    mock_search.assert_awaited_once_with(
        "leave stuff", top_k=5, conversation_id=None, user_id=None
    )
    assert result.needs_clarification is True
    assert result.query_type == "clarification_needed"
    assert "leave policy" in result.answer.lower()
    assert result.suggestions == ["leave policy"]


@pytest.mark.asyncio
@patch("app.agents.optimus.smartqna.agent._semantic_document_search", new_callable=AsyncMock)
async def test_handle_clarification_no_relevant_docs(mock_search):
    mock_search.return_value = [{"filename": "x.pdf", "score": 0.1}]

    result = await handle_clarification("???", doc_catalog=[])

    assert result.needs_clarification is True
    assert result.suggestions == []
    assert "rephrase" in result.answer.lower()


@pytest.mark.asyncio
@patch("app.agents.optimus.smartqna.agent.chat_complete_text", new_callable=AsyncMock)
async def test_handle_clarification_followup_reconstructs_question(mock_chat):
    mock_chat.return_value = "What is the leave policy?"

    history = [
        {
            "role": "assistant",
            "content": "Could you clarify?",
            "needs_clarification": True,
            "suggestions": ["Leave Policy"],
        },
        {"role": "user", "content": "yes leave"},
    ]

    result = await handle_clarification_followup("yes leave", history)

    assert result == "What is the leave policy?"
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_clarification_followup_skips_non_clarification_history():
    history = [
        {"role": "assistant", "content": "Here is the leave policy...", "needs_clarification": False},
        {"role": "user", "content": "thanks"},
    ]

    result = await handle_clarification_followup("thanks", history)

    assert result is None
