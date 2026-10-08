"""Dedicated executors so pandas/O2C do not starve Gmail on the default pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_cpu: ThreadPoolExecutor | None = None


def cpu_executor() -> ThreadPoolExecutor:
    global _cpu
    if _cpu is None:
        _cpu = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agentos-cpu")
    return _cpu


async def run_cpu(func: Callable[[], T]) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(cpu_executor(), func)


async def shutdown_executors() -> None:
    global _cpu
    if _cpu is not None:
        _cpu.shutdown(wait=True, cancel_futures=False)
        _cpu = None
    try:
        loop = asyncio.get_running_loop()
        await loop.shutdown_default_executor()
    except Exception:
        log.debug("default executor shutdown failed", exc_info=True)
