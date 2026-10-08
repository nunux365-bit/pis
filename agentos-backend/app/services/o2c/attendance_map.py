"""OHC attendance workbook → site map (blocking OpenPyXL path via threadpool)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.infra.sync_bridge import run_blocking
from app.o2c.agent_tools import tool_load_ohc_attendance_map


async def load_ohc_attendance_map_api(
    *,
    xlsx_path: str | None,
    sheet_name: str | None,
    include_rows: bool,
) -> dict[str, Any]:
    def _impl() -> dict[str, Any]:
        try:
            m = tool_load_ohc_attendance_map(xlsx_path, sheet_name=sheet_name)
        except (OSError, ValueError) as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        out: dict[str, Any] = {
            "sites": list(m.keys()),
            "row_counts": {k: len(v) for k, v in m.items()},
        }
        if include_rows:
            out["data"] = m
        return out

    return await run_blocking(_impl)
