"""Persist contract parsing failures to ``failed_contract_parsing`` (agenos, asyncpg)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async
from app.config.settings import settings


_UNDEFINED_TABLE = "42P01"  # asyncpg / PostgreSQL undefined_table


async def log_failed_contract_parsing_async(
    *,
    folder_root: str,
    relative_path: str,
    original_filename: str,
    file_sha256: str | None,
    source_file_modified_at: datetime | None,
    attempt_count: int,
    last_error: str,
    last_validation_errors: list[str] | None = None,
    last_llm_json: dict[str, Any] | None = None,
    markdown_snapshot: str | None = None,
) -> None:
    root = str(Path(folder_root).resolve())
    mtime = source_file_modified_at
    if mtime is not None and mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=UTC)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            try:
                await session.execute(
                    text("""
                    INSERT INTO failed_contract_parsing (
                        folder_root, relative_path, original_filename, file_sha256,
                        source_file_modified_at, attempt_count, last_error,
                        last_validation_errors, last_llm_json, markdown_snapshot
                    ) VALUES (
                        :root, :rel, :ofn, :sha, :mtime, :acnt, :lerr,
                        CAST(:lval AS jsonb), CAST(:llm AS jsonb), :mds
                    )
                    """),
                    {
                        "root": root,
                        "rel": relative_path,
                        "ofn": original_filename,
                        "sha": file_sha256,
                        "mtime": mtime,
                        "acnt": attempt_count,
                        "lerr": (last_error or "")[:20000],
                        "lval": json.dumps(last_validation_errors or []),
                        "llm": json.dumps(last_llm_json) if last_llm_json is not None else None,
                        "mds": ((markdown_snapshot or "")[:500_000] or None),
                    },
                )
            except ProgrammingError as e:
                orig = getattr(e, "orig", None)
                sqlstate = getattr(orig, "sqlstate", None) if orig is not None else None
                if sqlstate == _UNDEFINED_TABLE or (
                    orig is not None and "undefined_table" in str(orig).lower()
                ):
                    raise RuntimeError(
                        "failed_contract_parsing table missing. Apply docs/agenos/DDL_PATCH_O2C.sql to agenos."
                    ) from e
                raise


def log_failed_contract_parsing(
    *,
    folder_root: str,
    relative_path: str,
    original_filename: str,
    file_sha256: str | None,
    source_file_modified_at: datetime | None,
    attempt_count: int,
    last_error: str,
    last_validation_errors: list[str] | None = None,
    last_llm_json: dict[str, Any] | None = None,
    markdown_snapshot: str | None = None,
) -> None:
    run_agenos_async(
        log_failed_contract_parsing_async(
            folder_root=folder_root,
            relative_path=relative_path,
            original_filename=original_filename,
            file_sha256=file_sha256,
            source_file_modified_at=source_file_modified_at,
            attempt_count=attempt_count,
            last_error=last_error,
            last_validation_errors=last_validation_errors,
            last_llm_json=last_llm_json,
            markdown_snapshot=markdown_snapshot,
        )
    )


async def clear_failed_contract_parsing_async(*, folder_root: str, relative_path: str) -> None:
    root = str(Path(folder_root).resolve())
    rel = relative_path.replace("\\", "/")
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            try:
                await session.execute(
                    text("""
                    DELETE FROM failed_contract_parsing
                    WHERE folder_root = :root AND relative_path = :rel
                    """),
                    {"root": root, "rel": rel},
                )
            except ProgrammingError as e:
                orig = getattr(e, "orig", None)
                sqlstate = getattr(orig, "sqlstate", None) if orig is not None else None
                if sqlstate == _UNDEFINED_TABLE or (
                    orig is not None and "undefined_table" in str(orig).lower()
                ):
                    return
                raise


def clear_failed_contract_parsing(*, folder_root: str, relative_path: str) -> None:
    run_agenos_async(
        clear_failed_contract_parsing_async(folder_root=folder_root, relative_path=relative_path)
    )
