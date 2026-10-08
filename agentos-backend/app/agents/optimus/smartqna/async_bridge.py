"""Run async coroutines from sync entrypoints (scripts/tests only)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[object, object, T]) -> T:
    """Run *coro* when no event loop is active; refuse inside async handlers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Use the async API from async code")
