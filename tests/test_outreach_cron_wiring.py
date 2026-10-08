"""Verify outreach cron jobs are registered when outreach_enabled=True."""
from unittest.mock import patch, MagicMock
import pytest


def test_outreach_jobs_registered_when_enabled():
    from app.config.settings import settings
    scheduler_mock = MagicMock()

    with patch.object(settings, "outreach_enabled", True):
        with patch("app.kernel.scheduler.scheduler", scheduler_mock):
            from app.kernel.scheduler import _register_builtin_jobs
            _register_builtin_jobs()

    assert "outreach_sync" in str(scheduler_mock.add_job.call_args_list)
    assert "outreach_send" in str(scheduler_mock.add_job.call_args_list)


def test_outreach_jobs_not_registered_when_disabled():
    from app.config.settings import settings
    scheduler_mock = MagicMock()

    with patch.object(settings, "outreach_enabled", False):
        with patch("app.kernel.scheduler.scheduler", scheduler_mock):
            from app.kernel.scheduler import _register_builtin_jobs
            _register_builtin_jobs()

    calls_str = str(scheduler_mock.add_job.call_args_list)
    assert "outreach_sync" not in calls_str
    assert "outreach_send" not in calls_str
