"""AgentOS main-DB contract folder ingest — async runner executed off the FastAPI event loop."""

from __future__ import annotations

import asyncio
from typing import Any

from app.infra.sync_bridge import run_blocking
from app.o2c.runner import run_o2c_folder_ingest


async def run_contract_folder_ingest(
    *,
    contracts_root: str | None,
    max_files: int | None,
    ignore_mtime_watermark: bool,
) -> dict[str, Any]:
    """
    Run incremental PDF ingest without blocking the app event loop.

    ``run_o2c_folder_ingest`` is async but uses sync PDF text extraction and filesystem scans;
    those run inside ``asyncio.run(...)`` in a worker thread (same isolation pattern as
    ``run_o2c_full_graph`` for the O2C LangGraph path).
    """

    def _run() -> dict[str, Any]:
        return asyncio.run(
            run_o2c_folder_ingest(
                contracts_root=contracts_root,
                max_files=max_files,
                ignore_mtime_watermark=ignore_mtime_watermark,
            )
        )

    return await run_blocking(_run)
