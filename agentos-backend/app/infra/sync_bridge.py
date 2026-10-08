"""Offload blocking callables to a thread pool so ``async`` FastAPI routes do not stall the event loop.

Use when there is **no** mature async API, or legacy sync orchestration must stay intact:

- **OpenPyXL** (workbook read/write)
- **Google Drive / Sheets** official sync clients and uploads
- **Contract vector indexing** when using sync clients (prefer ``AsyncOpenAI`` + ``AsyncQdrantClient`` in async paths)
- **Heavy sync file I/O** or CPU-bound pure-Python that would block the loop

Prefer **native async** (SQLAlchemy async + asyncpg, ``AsyncOpenAI``, ``await`` agenos coroutines)
in ``async def`` handlers instead of wrapping whole flows in ``run_blocking``.

For **new** code, isolate only the blocking slice::

    async def endpoint(...):
        def _openpyxl_or_gdrive() -> str:
            ...
        path = await run_blocking(_openpyxl_or_gdrive)
        result = await my_async_service(path)

``starlette.concurrency.run_in_threadpool`` propagates exceptions (including ``HTTPException``)
back to the caller's task.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from starlette.concurrency import run_in_threadpool

T = TypeVar("T")


async def run_blocking(factory: Callable[[], T]) -> T:
    """Run ``factory()`` in a worker thread and await the result."""
    return await run_in_threadpool(factory)
