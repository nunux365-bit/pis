"""Fire-and-forget tasks keep a strong ref until done (Python 3.12+)."""

from __future__ import annotations

import asyncio

import pytest

from app.infra.task_tracker import inflight_count, shutdown_spawned_tasks, spawn


@pytest.mark.asyncio
async def test_spawn_tracks_until_done():
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    before = inflight_count()
    task = spawn(work(), name="test-track")
    await started.wait()
    assert inflight_count() == before + 1
    release.set()
    await task
    assert inflight_count() == before


@pytest.mark.asyncio
async def test_spawn_discards_on_failure():
    before = inflight_count()

    async def boom():
        raise RuntimeError("task failed")

    task = spawn(boom(), name="test-fail")
    with pytest.raises(RuntimeError, match="task failed"):
        await task
    assert inflight_count() == before


@pytest.mark.asyncio
async def test_shutdown_cancels_leftovers():
    async def hang():
        await asyncio.Event().wait()

    spawn(hang(), name="test-hang")
    await shutdown_spawned_tasks(grace_sec=0.05)
    assert inflight_count() == 0


@pytest.mark.asyncio
async def test_shutdown_noop_when_empty():
    # Drain anything left by prior tests in this process.
    await shutdown_spawned_tasks(grace_sec=0.01)
    await shutdown_spawned_tasks(grace_sec=0.01)
