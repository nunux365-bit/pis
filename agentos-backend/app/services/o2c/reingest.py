"""Re-ingest failed contract parsing entries (agenos lookup + main DB ingest)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.o2c.runner import run_o2c_folder_ingest


async def reingest_failed_contract_parsing(*, failure_id: str) -> dict[str, Any]:
    fid = (failure_id or "").strip()
    if not fid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "failure_id is required")

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            fr = await session.execute(
                text(
                    "SELECT folder_root, relative_path FROM failed_contract_parsing "
                    "WHERE id = CAST(:fid AS uuid)"
                ),
                {"fid": fid},
            )
            row = fr.mappings().first()
            if not row:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "failed_contract_parsing entry not found"
                )
            contracts_root = row["folder_root"]
    try:
        result = await run_o2c_folder_ingest(contracts_root=contracts_root, max_files=1)
        return {"status": "ok", "reingest_result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}
