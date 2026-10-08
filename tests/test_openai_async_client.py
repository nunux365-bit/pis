"""Unit tests for shared AsyncOpenAI / httpx client lifecycle."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.infra import openai_async_client as oai


@pytest.fixture(autouse=True)
async def _reset_shared_client():
    await oai.close_shared_openai_client()
    yield
    await oai.close_shared_openai_client()


@pytest.mark.asyncio
async def test_get_returns_same_instance(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")
    a = oai.get_shared_openai_client()
    b = oai.get_shared_openai_client()
    assert a is b
    assert oai._http_client is not None
    assert not oai._http_client.is_closed


@pytest.mark.asyncio
async def test_get_requires_api_key(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "  ")
    with pytest.raises(RuntimeError, match="openai_api_key"):
        oai.get_shared_openai_client()
    assert oai._openai_client is None
    assert oai._http_client is None


@pytest.mark.asyncio
async def test_close_is_idempotent(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")
    oai.get_shared_openai_client()
    await oai.close_shared_openai_client()
    await oai.close_shared_openai_client()
    assert oai._openai_client is None
    assert oai._http_client is None


@pytest.mark.asyncio
async def test_get_after_close_creates_new_client(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")
    first = oai.get_shared_openai_client()
    first_http = oai._http_client
    await oai.close_shared_openai_client()
    assert first_http is not None and first_http.is_closed
    second = oai.get_shared_openai_client()
    assert second is not first
    assert oai._http_client is not None
    assert not oai._http_client.is_closed


@pytest.mark.asyncio
async def test_recreate_when_http_closed_externally(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")
    first = oai.get_shared_openai_client()
    http = oai._http_client
    assert http is not None
    await http.aclose()
    second = oai.get_shared_openai_client()
    # Allow deferred close task to run.
    await asyncio.sleep(0)
    assert second is not first
    assert oai._http_client is not None
    assert not oai._http_client.is_closed


@pytest.mark.asyncio
async def test_failed_openai_init_closes_httpx(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")

    created: list[httpx.AsyncClient] = []
    real_async_client = httpx.AsyncClient

    def tracking_async_client(*args, **kwargs):
        c = real_async_client(*args, **kwargs)
        created.append(c)
        return c

    monkeypatch.setattr(oai.httpx, "AsyncClient", tracking_async_client)

    def boom(*_a, **_k):
        raise RuntimeError("sdk init failed")

    monkeypatch.setattr(oai, "AsyncOpenAI", boom)

    with pytest.raises(RuntimeError, match="sdk init failed"):
        oai.get_shared_openai_client()

    await asyncio.sleep(0)
    assert oai._openai_client is None
    assert oai._http_client is None
    assert len(created) == 1
    assert created[0].is_closed


@pytest.mark.asyncio
async def test_close_then_concurrent_gets_share_one(monkeypatch):
    monkeypatch.setattr(oai.settings, "openai_api_key", "sk-test-shared")
    await oai.close_shared_openai_client()

    clients = [oai.get_shared_openai_client() for _ in range(20)]
    assert all(c is clients[0] for c in clients)


@pytest.mark.asyncio
async def test_judge_missing_key_before_shared_client(monkeypatch):
    from app.agents.responder_eval.judge import JudgeError, run_judge

    monkeypatch.setattr(oai.settings, "openai_api_key", "")
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.settings.openai_api_key",
        "",
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.settings.responder_eval_mock_judge",
        False,
    )
    with pytest.raises(JudgeError, match="OPENAI_API_KEY"):
        await run_judge({"messages": []}, {}, {})


@pytest.mark.asyncio
async def test_synthesize_missing_key_uses_mock(monkeypatch):
    from app.agents.order_rca import synthesize

    monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", False)
    monkeypatch.setattr(synthesize.settings, "openai_api_key", "")
    called = False

    def _should_not_run():
        nonlocal called
        called = True
        raise AssertionError("shared client must not be used")

    monkeypatch.setattr(synthesize, "get_shared_openai_client", _should_not_run)
    syn, source = await synthesize.synthesize_rca(
        {
            "preflight": {"allocation_badge": "IDEAL"},
            "perfect_order": {
                "overall": "imperfect",
                "overall_pass": False,
                "pillars": [
                    {
                        "id": "delivery",
                        "pass": False,
                        "status": "fail",
                        "label": "Late",
                        "detail": "late",
                    }
                ],
            },
            "operations": {"status_transitions": [], "groot_empty": True},
            "p1": {"status": "not_configured"},
            "p4": {"rows": []},
            "skus": [],
            "order": {"order_id": "PO1"},
        }
    )
    assert source == "mock"
    assert not called
    assert isinstance(syn, dict)


@pytest.mark.asyncio
async def test_synthesize_openai_error_falls_back(monkeypatch):
    from app.agents.order_rca import synthesize

    monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", False)
    monkeypatch.setattr(synthesize.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(synthesize.settings, "order_rca_openai_model", "gpt-test")
    monkeypatch.setattr(synthesize.settings, "order_rca_synthesis_temperature", 0)

    class _Broken:
        class responses:
            @staticmethod
            async def create(**_kwargs):
                raise RuntimeError("openai down")

    monkeypatch.setattr(synthesize, "get_shared_openai_client", lambda: _Broken())
    syn, source = await synthesize.synthesize_rca(
        {
            "preflight": {"allocation_badge": "IDEAL"},
            "perfect_order": {
                "overall": "imperfect",
                "overall_pass": False,
                "pillars": [
                    {
                        "id": "delivery",
                        "pass": False,
                        "status": "fail",
                        "label": "Late",
                        "detail": "late",
                    }
                ],
            },
            "operations": {"status_transitions": [], "groot_empty": True},
            "p1": {"status": "not_configured"},
            "p4": {"rows": []},
            "skus": [],
            "order": {"order_id": "PO1"},
        }
    )
    assert source == "fallback"
    assert isinstance(syn, dict)
    assert "verdict" in syn


@pytest.mark.asyncio
async def test_judge_shared_client_error_wrapped(monkeypatch):
    from app.agents.responder_eval.judge import JudgeError, run_judge

    monkeypatch.setattr(
        "app.agents.responder_eval.judge.settings.responder_eval_mock_judge",
        False,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.judge.settings.openai_api_key",
        "sk-test",
    )

    def _boom():
        raise RuntimeError("pool dead")

    monkeypatch.setattr(
        "app.agents.responder_eval.judge.get_shared_openai_client",
        _boom,
    )
    with pytest.raises(JudgeError, match="pool dead"):
        await run_judge({"messages": [{"role": "assistant", "content": "x"}]}, {}, {})
