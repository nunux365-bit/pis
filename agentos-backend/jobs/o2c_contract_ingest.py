"""Daily O2C contract PDF ingest from Google Drive — ``run_o2c_pipeline`` only (no MIS)."""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import settings
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)


def validate_drive_contract_ingest_config() -> str | None:
    """
    Return an error message when Drive contract ingest is not runnable; ``None`` when OK.

    Requires parent folder + ingestion DB key, plus either all-sites mode or a site folder name.
    """
    gdrive_parent = (settings.o2c_gdrive_contracts_parent_folder_id or "").strip()
    ingestion_db = (settings.o2c_contracts_ingestion_root_db or "").strip()
    if not gdrive_parent:
        return "O2C_GDRIVE_CONTRACTS_PARENT_FOLDER_ID is not set"
    if not ingestion_db:
        return "O2C_CONTRACTS_INGESTION_ROOT_DB is not set"
    root = (settings.o2c_contracts_root or "").strip()
    if bool(settings.o2c_gdrive_contracts_all_sites) and not root:
        return None
    if root:
        return None
    return (
        "set O2C_GDRIVE_CONTRACTS_ALL_SITES=true (leave O2C_CONTRACTS_ROOT blank) "
        "or set O2C_CONTRACTS_ROOT to a child folder name (e.g. taco)"
    )


def _run_pipeline_sync() -> dict[str, Any]:
    from app.agents.o2c_ohc.pipeline import run_o2c_pipeline

    return run_o2c_pipeline(
        contracts_root=None,
        ignore_mtime_watermark=False,
    )


async def o2c_contract_ingest_job() -> None:
    config_err = validate_drive_contract_ingest_config()
    if config_err:
        log.info("o2c_contract_ingest: skipped (%s)", config_err)
        return

    try:
        result = await run_blocking(_run_pipeline_sync)
        top_error = (result.get("error") or "").strip() if isinstance(result, dict) else ""
        if top_error:
            log.error(
                "o2c_contract_ingest: pipeline error root=%s error=%s",
                result.get("contracts_root") or result.get("ingestion_root_db") or "",
                top_error,
            )
            return

        failed = int(result.get("failed") or 0)
        ok_n = int(result.get("ok") or 0)
        candidates = int(result.get("candidates") or 0)
        root = result.get("contracts_root") or result.get("ingestion_root_db") or ""
        log.info(
            "o2c_contract_ingest done root=%s candidates=%s ok=%s failed=%s",
            root,
            candidates,
            ok_n,
            failed,
        )
        if failed:
            for row in result.get("results") or []:
                if row.get("status") != "failed":
                    continue
                log.warning(
                    "o2c_contract_ingest failed pdf=%s reason=%s error=%s",
                    row.get("relative_path"),
                    row.get("reason"),
                    row.get("error"),
                )
    except ValueError as e:
        log.warning("o2c_contract_ingest: configuration error: %s", e)
    except Exception:
        log.exception("o2c_contract_ingest_job failed")
