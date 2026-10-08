"""Cron job wrappers route through the unified LangGraph (not the pipeline directly).

We had a quiet split-brain earlier: the cron ticked ``pipeline.scan_and_process``
/ ``pipeline.dispatch_approved`` while the workflow-catalog UI + HTTP triggers
ran the LangGraph. Behavior was identical (both ended up in the same helpers)
but observability and future node-level hooks (tracing, retries, LLM-backed
nodes) would silently skip cron traffic.

These tests pin the contract: the only thing the cron does on a healthy tick
is call the graph's ``run_scan_async`` / ``run_dispatch_async``. Duplicate ingest
is uniqueness-constrained; dispatch claims with ``SKIP LOCKED``. No process-wide
advisory lock, no direct pipeline imports, no hidden branches.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def test_scan_job_delegates_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import settings
    from jobs import email_automation_scan as job_mod

    monkeypatch.setattr(settings, "email_automation_enabled", True, raising=False)

    calls: list[dict[str, Any]] = []

    async def _fake_run_scan_async() -> dict[str, Any]:
        calls.append({"kind": "scan"})
        return {
            "disabled": False,
            "ingested": ["m1", "m2"],
            "result": {"message_count": 2, "send_count": 3, "per_message": []},
        }

    monkeypatch.setattr(job_mod, "run_scan_async", _fake_run_scan_async)

    asyncio.run(job_mod.email_automation_scan_job())

    assert calls == [{"kind": "scan"}], "scan job must invoke run_scan_async exactly once"


def test_dispatch_job_delegates_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import settings
    from jobs import email_automation_scan as job_mod

    monkeypatch.setattr(settings, "email_automation_enabled", True, raising=False)

    calls: list[dict[str, Any]] = []

    async def _fake_run_dispatch_async() -> dict[str, Any]:
        calls.append({"kind": "dispatch"})
        return {
            "disabled": False,
            "reclaim": {"requeued": 0, "completed": 0},
            "sent_ids": ["s1"],
            "failed": [],
        }

    monkeypatch.setattr(job_mod, "run_dispatch_async", _fake_run_dispatch_async)

    asyncio.run(job_mod.email_automation_dispatch_job())

    assert calls == [{"kind": "dispatch"}], (
        "dispatch job must invoke run_dispatch_async exactly once"
    )


def test_scan_job_noops_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import settings
    from jobs import email_automation_scan as job_mod

    monkeypatch.setattr(settings, "email_automation_enabled", False, raising=False)

    async def _boom(*_a, **_k):  # pragma: no cover — must never fire
        raise AssertionError("graph should not be invoked when kill switch is off")

    monkeypatch.setattr(job_mod, "run_scan_async", _boom)
    monkeypatch.setattr(job_mod, "run_dispatch_async", _boom)

    asyncio.run(job_mod.email_automation_scan_job())
    asyncio.run(job_mod.email_automation_dispatch_job())


def test_scan_job_swallows_graph_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node-level crash must not propagate — next tick retries."""

    from app.config.settings import settings
    from jobs import email_automation_scan as job_mod

    monkeypatch.setattr(settings, "email_automation_enabled", True, raising=False)

    async def _boom() -> dict[str, Any]:
        raise RuntimeError("gmail 500 on this tick")

    monkeypatch.setattr(job_mod, "run_scan_async", _boom)

    asyncio.run(job_mod.email_automation_scan_job())  # must not raise


@pytest.mark.parametrize(
    "exc_factory,expected_reason_prefix",
    [
        pytest.param(
            lambda: __import__(
                "google.auth.exceptions", fromlist=["RefreshError"]
            ).RefreshError("unauthorized_client: DWD not set up"),
            "google_auth_refresh_error",
            id="refresh_error",
        ),
        pytest.param(
            lambda: __import__("ssl").SSLEOFError(
                "UNEXPECTED_EOF_WHILE_READING during JWT grant"
            ),
            "google_transport_error",
            id="ssl_eof",
        ),
        pytest.param(
            lambda: TimeoutError("oauth2.googleapis.com timed out"),
            "google_transport_error",
            id="timeout",
        ),
        pytest.param(
            lambda: BrokenPipeError(32, "Broken pipe"),
            "google_transport_error",
            id="broken_pipe",
        ),
        pytest.param(
            lambda: ConnectionResetError(54, "Connection reset by peer"),
            "google_transport_error",
            id="connection_reset",
        ),
    ],
)
def test_scan_job_demotes_operational_google_errors_to_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc_factory,
    expected_reason_prefix: str,
) -> None:
    """Operational Google failures → single-line WARNING, no traceback.

    Covers both DWD / auth misconfig (``RefreshError``) and transient network
    blips (raw ``SSLError``, ``TimeoutError``) that leak past google-auth's
    own ``TransportError`` wrapping. The scheduler's next tick is the retry.
    """

    import logging

    from app.config.settings import settings
    from jobs import email_automation_scan as job_mod

    pytest.importorskip("google.auth.exceptions")

    monkeypatch.setattr(settings, "email_automation_enabled", True, raising=False)

    async def _fail() -> dict[str, Any]:
        raise exc_factory()

    monkeypatch.setattr(job_mod, "run_scan_async", _fail)

    with caplog.at_level(logging.WARNING, logger="jobs.email_automation_scan"):
        asyncio.run(job_mod.email_automation_scan_job())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(expected_reason_prefix in r.getMessage() for r in warnings), (
        f"expected WARNING containing {expected_reason_prefix!r}, "
        f"got: {[r.getMessage() for r in warnings]}"
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, (
        f"operational Google errors must not emit ERROR / exception trace, got: "
        f"{[r.getMessage() for r in errors]}"
    )
