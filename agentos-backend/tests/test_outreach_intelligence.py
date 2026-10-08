"""Tests for outreach reply intelligence pipeline."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@patch("app.email_automation.outreach.outreach_llm.classify_outreach_thread_transcript")
async def test_classify_returns_expected_keys(mock_classify):
    mock_classify.return_value = {
        "category": "interested",
        "confidence": "high",
        "justification": "Prospect asked for a demo call.",
        "prompt_version": "chw_outreach_reply_v1",
    }
    from app.email_automation.outreach.outreach_llm import classify_outreach_thread_transcript
    result = await classify_outreach_thread_transcript("transcript text", prompt_path="chw/categorizer_prompt.txt")
    assert result["category"] == "interested"
    assert "confidence" in result
    assert "justification" in result


def test_prompt_version_constant_defined():
    from app.email_automation.outreach.engine import CHW_OUTREACH_PROMPT_VERSION
    assert CHW_OUTREACH_PROMPT_VERSION == "chw_outreach_reply_v1"
