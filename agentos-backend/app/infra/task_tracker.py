"""Strong refs for fire-and-forget tasks (Python 3.12+ GC can drop unreferenced tasks)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

_inflight: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Schedule ``coro`` and keep a strong reference until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


def inflight_count() -> int:
    return len(_inflight)


async def shutdown_spawned_tasks(*, grace_sec: float = 5.0) -> None:
    """Wait briefly for in-flight work, then cancel leftovers."""
    pending = list(_inflight)
    if not pending:
        return
    _done, still = await asyncio.wait(pending, timeout=max(0.0, grace_sec))
    for task in still:
        task.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)
    log.debug("spawned tasks shutdown remaining=%s", len(_inflight))
