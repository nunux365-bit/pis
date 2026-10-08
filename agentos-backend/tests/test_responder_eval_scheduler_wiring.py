"""Verify responder eval scheduler job registration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.settings import settings


def test_responder_eval_tick_registered_when_enabled():
    scheduler_mock = MagicMock()

    with patch.object(settings, "responder_eval_enabled", True):
        with patch.object(settings, "responder_eval_tick_interval_minutes", 10):
            with patch("app.kernel.scheduler.scheduler", scheduler_mock):
                from app.kernel.scheduler import _register_builtin_jobs

                _register_builtin_jobs()

    calls = [
        c for c in scheduler_mock.add_job.call_args_list if c.kwargs.get("id") == "responder_eval_tick"
    ]
    assert len(calls) == 1
    call = calls[0]
    assert call.args[0].__name__ == "responder_eval_tick_job"
    assert call.kwargs["minutes"] == 10
    assert call.kwargs["max_instances"] == 1


def test_responder_eval_tick_not_registered_when_disabled():
    scheduler_mock = MagicMock()

    with patch.object(settings, "responder_eval_enabled", False):
        with patch("app.kernel.scheduler.scheduler", scheduler_mock):
            from app.kernel.scheduler import _register_builtin_jobs

            _register_builtin_jobs()

    ids = [c.kwargs.get("id") for c in scheduler_mock.add_job.call_args_list]
    assert "responder_eval_tick" not in ids


@pytest.mark.asyncio
async def test_responder_eval_tick_job_logs_skipped_lock(monkeypatch):
    from jobs import responder_eval_tick as job

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(
        job, "run_responder_eval_batch", AsyncMock(return_value={"skipped_lock": 1})
    )
    with patch.object(job.log, "info") as info:
        await job.responder_eval_tick_job()
    info.assert_called_once()
    assert "skipped_lock" in str(info.call_args)
