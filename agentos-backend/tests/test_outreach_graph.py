"""Tests for the outreach LangGraph agent — fine-grained 3-branch topology."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_graph_cache():
    from app.agents.outreach.graph import _compile_outreach_graph
    _compile_outreach_graph.cache_clear()


# ---------------------------------------------------------------------------
# mode_router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_router_disabled_sets_disabled_true():
    import app.config.settings as settings_mod

    _clear_graph_cache()
    fake_settings = MagicMock()
    fake_settings.outreach_enabled = False
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import _node_mode_router
        result = await _node_mode_router({"mode": "sync"})
        assert result == {"disabled": True}
    finally:
        settings_mod.settings = original
        _clear_graph_cache()


@pytest.mark.asyncio
async def test_mode_router_enabled_sets_disabled_false():
    import app.config.settings as settings_mod

    _clear_graph_cache()
    fake_settings = MagicMock()
    fake_settings.outreach_enabled = True
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import _node_mode_router
        result = await _node_mode_router({"mode": "sync"})
        assert result == {"disabled": False}
    finally:
        settings_mod.settings = original
        _clear_graph_cache()


# ---------------------------------------------------------------------------
# _route_by_mode
# ---------------------------------------------------------------------------


def test_route_by_mode_sync():
    from app.agents.outreach.graph import _route_by_mode
    assert _route_by_mode({"mode": "sync"}) == "sync_branch"


def test_route_by_mode_dispatch():
    from app.agents.outreach.graph import _route_by_mode
    assert _route_by_mode({"mode": "dispatch"}) == "dispatch_branch"


def test_route_by_mode_replies():
    from app.agents.outreach.graph import _route_by_mode
    assert _route_by_mode({"mode": "replies"}) == "replies_branch"


def test_route_by_mode_disabled_returns_end():
    from langgraph.graph import END
    from app.agents.outreach.graph import _route_by_mode
    assert _route_by_mode({"mode": "sync", "disabled": True}) == END


# ---------------------------------------------------------------------------
# Sync branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_sheet_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_ingest_sheet_all
    result = await _node_ingest_sheet_all({"mode": "sync", "disabled": True})
    assert result == {"ingest_results": []}


@pytest.mark.asyncio
async def test_reconcile_db_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_reconcile_db_all
    result = await _node_reconcile_db_all({"mode": "sync", "disabled": True})
    assert result == {"sync_results": []}


@pytest.mark.asyncio
async def test_reconcile_db_all_noops_on_empty_ingest():
    from app.agents.outreach.graph import _node_reconcile_db_all
    result = await _node_reconcile_db_all({"mode": "sync", "disabled": False, "ingest_results": []})
    assert result == {"sync_results": []}


# ---------------------------------------------------------------------------
# Dispatch branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_health_check_all
    result = await _node_health_check_all({"mode": "dispatch", "disabled": True})
    assert result == {"error": "disabled"}


@pytest.mark.asyncio
async def test_health_check_all_ok_when_gmail_reachable():
    from app.agents.outreach.graph import _node_health_check_all
    with patch("app.email_automation.gmail_sa.list_inbox_messages", return_value=[]):
        result = await _node_health_check_all({"mode": "dispatch", "disabled": False})
    assert result == {}


@pytest.mark.asyncio
async def test_health_check_all_sets_error_on_failure():
    from app.agents.outreach.graph import _node_health_check_all
    with patch(
        "app.email_automation.gmail_sa.list_inbox_messages",
        side_effect=Exception("connection refused"),
    ):
        result = await _node_health_check_all({"mode": "dispatch", "disabled": False})
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_quota_guard_unlimited_when_limit_zero():
    import app.config.settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.outreach_daily_send_limit = 0
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import _node_quota_guard
        result = await _node_quota_guard(
            {"mode": "dispatch", "disabled": False}
        )
        assert result == {"quota_remaining": -1}
    finally:
        settings_mod.settings = original


@pytest.mark.asyncio
async def test_quota_guard_aborts_when_health_failed():
    from app.agents.outreach.graph import _node_quota_guard
    result = await _node_quota_guard({"mode": "dispatch", "disabled": False, "error": "unreachable"})
    assert result == {"dispatch_results": [], "dispatch_writeback": []}


@pytest.mark.asyncio
async def test_send_batch_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_send_batch_all
    result = await _node_send_batch_all({"mode": "dispatch", "disabled": True})
    assert result == {"dispatch_results": [], "dispatch_writeback": []}


@pytest.mark.asyncio
async def test_send_batch_all_noops_when_quota_exhausted():
    from app.agents.outreach.graph import _node_send_batch_all
    result = await _node_send_batch_all(
        {"mode": "dispatch", "disabled": False, "quota_remaining": 0}
    )
    assert result == {"dispatch_results": [], "dispatch_writeback": []}


# ---------------------------------------------------------------------------
# Replies branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_replies_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_scan_replies_all
    result = await _node_scan_replies_all({"mode": "replies", "disabled": True})
    assert result == {"reply_scan_results": []}


@pytest.mark.asyncio
async def test_classify_replies_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_classify_replies_all
    result = await _node_classify_replies_all({"mode": "replies", "disabled": True})
    assert result == {"reply_classify_results": []}


@pytest.mark.asyncio
async def test_classify_replies_all_noops_on_empty_scan():
    from app.agents.outreach.graph import _node_classify_replies_all
    result = await _node_classify_replies_all(
        {"mode": "replies", "disabled": False, "reply_scan_results": []}
    )
    assert result == {"reply_classify_results": []}


@pytest.mark.asyncio
async def test_update_reply_db_all_noops_when_disabled():
    from app.agents.outreach.graph import _node_update_reply_db_all
    result = await _node_update_reply_db_all({"mode": "replies", "disabled": True})
    assert result == {"reply_db_results": []}


# ---------------------------------------------------------------------------
# Graph entry points — shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_outreach_replies_async_disabled_returns_shape():
    _clear_graph_cache()
    import app.config.settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.outreach_enabled = False
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import run_outreach_replies_async
        _clear_graph_cache()
        out = await run_outreach_replies_async()
        assert out["disabled"] is True
        assert out["reply_db_results"] == []
    finally:
        settings_mod.settings = original
        _clear_graph_cache()


@pytest.mark.asyncio
async def test_run_outreach_sync_async_disabled_returns_shape():
    _clear_graph_cache()
    import app.config.settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.outreach_enabled = False
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import run_outreach_sync_async
        _clear_graph_cache()
        out = await run_outreach_sync_async()
        assert out["disabled"] is True
        assert out["sync_results"] == []
    finally:
        settings_mod.settings = original
        _clear_graph_cache()


@pytest.mark.asyncio
async def test_run_outreach_dispatch_async_disabled_returns_shape():
    _clear_graph_cache()
    import app.config.settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.outreach_enabled = False
    original = settings_mod.settings
    settings_mod.settings = fake_settings
    try:
        from app.agents.outreach.graph import run_outreach_dispatch_async
        _clear_graph_cache()
        out = await run_outreach_dispatch_async()
        assert out["disabled"] is True
        assert out["dispatch_results"] == []
    finally:
        settings_mod.settings = original
        _clear_graph_cache()


# ---------------------------------------------------------------------------
# Catalog seed
# ---------------------------------------------------------------------------


def test_cold_outreach_agent_in_catalog_seed():
    from app.seed.catalog_seed import AGENT_SEED_ROWS
    names = [row[0] for row in AGENT_SEED_ROWS]
    assert "Cold Outreach Agent" in names
