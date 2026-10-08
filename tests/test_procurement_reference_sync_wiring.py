"""Verify procurement reference sync cron is registered when enabled."""

from unittest.mock import MagicMock, patch


def test_procurement_reference_sync_job_registered_when_enabled():
    from app.config.settings import settings

    scheduler_mock = MagicMock()

    with patch.object(settings, "procurement_reference_sync_enabled", True):
        with patch("app.kernel.scheduler.scheduler", scheduler_mock):
            from app.kernel.scheduler import _register_builtin_jobs

            _register_builtin_jobs()

    calls = scheduler_mock.add_job.call_args_list
    sync_calls = [c for c in calls if c.kwargs.get("id") == "procurement_reference_sync"]
    assert len(sync_calls) == 1
    call = sync_calls[0]
    assert call.args[0].__name__ == "procurement_reference_sync_job"
    assert call.kwargs["hour"] == 2
    assert call.kwargs["minute"] == 30
    assert call.kwargs["misfire_grace_time"] == 43_200


def test_procurement_reference_sync_job_not_registered_when_disabled():
    from app.config.settings import settings

    scheduler_mock = MagicMock()

    with patch.object(settings, "procurement_reference_sync_enabled", False):
        with patch("app.kernel.scheduler.scheduler", scheduler_mock):
            from app.kernel.scheduler import _register_builtin_jobs

            _register_builtin_jobs()

    ids = [c.kwargs.get("id") for c in scheduler_mock.add_job.call_args_list]
    assert "procurement_reference_sync" not in ids


def test_scheduler_uses_utc_timezone():
    from app.kernel.scheduler import scheduler

    assert str(scheduler.timezone) == "UTC"
