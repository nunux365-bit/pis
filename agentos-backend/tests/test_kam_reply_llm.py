"""KAM accuracy classifier — output parsing / coercion with a mocked OpenAI client."""

from __future__ import annotations

import json

import pytest

from app.email_automation import kam_reply_llm


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    async def create(self, **kwargs):
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_openai(monkeypatch, payload: dict) -> None:
    import openai

    content = json.dumps(payload)
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: _FakeClient(content))
    monkeypatch.setattr(kam_reply_llm.settings, "openai_api_key", "sk-test")


@pytest.mark.asyncio
async def test_accurate_thread_clears_failure_reason(monkeypatch):
    _patch_openai(
        monkeypatch,
        {
            "score": 1,
            "client_responsive": True,
            "scenarios": ["recon_pending"],
            "open_asks": ["recon"],
            "asks_missed": [],
            "kam_actively_engaged": True,
            "clear_cta": True,
            # Even if the model wrongly emits a reason, score=1 forces "none".
            "failure_reason": "missed_open_ask",
            "confidence": "high",
            "justification": "++Bhawna please reconcile",
        },
    )

    out = await kam_reply_llm.classify_kam_reply_thread_transcript(
        "thread", kam_directory_block="HANA H1 -> KAM A <a@1mg.com>"
    )
    assert out["score"] == 1
    assert out["failure_reason"] == "none"
    assert out["scenarios"] == ["recon_pending"]
    assert out["prompt_version"] == kam_reply_llm.PROMPT_VERSION


@pytest.mark.asyncio
async def test_inaccurate_thread_keeps_reason_and_coerces(monkeypatch):
    _patch_openai(
        monkeypatch,
        {
            "score": 0,
            "client_responsive": True,
            "scenarios": ["request_for_invoice", "bogus_scenario"],
            "asks_missed": ["invoice copy"],
            "kam_actively_engaged": False,
            "clear_cta": False,
            "failure_reason": "missing_document",
            "confidence": "weird",  # coerced to "low"
            "justification": "no attachment",
        },
    )

    out = await kam_reply_llm.classify_kam_reply_thread_transcript(
        "thread", kam_directory_block="HANA H1 -> KAM A <a@1mg.com>"
    )
    assert out["score"] == 0
    assert out["failure_reason"] == "missing_document"
    assert out["confidence"] == "low"
    # Unknown scenario slug is filtered out.
    assert out["scenarios"] == ["request_for_invoice"]


@pytest.mark.asyncio
async def test_non_binary_score_coerced_to_zero(monkeypatch):
    _patch_openai(monkeypatch, {"score": 5, "confidence": "low", "justification": ""})
    out = await kam_reply_llm.classify_kam_reply_thread_transcript(
        "thread", kam_directory_block="x"
    )
    assert out["score"] == 0


@pytest.mark.asyncio
async def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(kam_reply_llm.settings, "openai_api_key", "")
    with pytest.raises(RuntimeError):
        await kam_reply_llm.classify_kam_reply_thread_transcript("t", kam_directory_block="x")
