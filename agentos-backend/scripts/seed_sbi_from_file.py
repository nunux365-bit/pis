#!/usr/bin/env python3
"""
Seed the SBI MIS database from a local spreadsheet — no server, no network.

Reads the "Dump" sheet from the provided xlsx/csv, runs the full SBI MIS
pipeline (applies rules, computes derived columns, populates pf_historicals),
and writes the run record to PostgreSQL.

Usage
-----
    # from project root with the virtualenv active:
    python scripts/seed_sbi_from_file.py /path/to/SBI_Dump_2026-05.xlsx 2026-05

    # If you need to override the database URL for this run:
    DATABASE_URL=postgresql+asyncpg://... python scripts/seed_sbi_from_file.py ...

Requirements
------------
- The backend .env (or environment) must have DATABASE_URL set.
- SBI_MIS_ENABLED=true is NOT required — this script bypasses the feature flag.
- The DB migrations must be applied (alembic upgrade head).
- Bootstrap must have been run at least once (to seed rules / sheet_columns).
  If the DB is empty, the pipeline still runs but produces no derived columns.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

# ── Allow running from project root without installing the package ───────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── Load .env if present (same as uvicorn does) ──────────────────────────────
_env_file = _PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
        print(f"[seed] Loaded env from {_env_file}")
    except ImportError:
        pass  # python-dotenv not installed; rely on shell env

# ── App imports (after sys.path and env are set) ─────────────────────────────
from app.config.settings import settings  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402 — initialises engine
import app.db.models  # noqa: F401, E402 — register ORM mappers
import app.sbi_mis.models  # noqa: F401, E402 — register SBI MIS ORM models

from app.sbi_mis import db as sbi_db
from app.sbi_mis.engine import ingest, pipeline


# ── Helpers ──────────────────────────────────────────────────────────────────

def _copy_to_data_dir(src: Path, month: str) -> Path:
    """Copy the source file into SBI_MIS_DATA_DIR/uploads/<month>/."""
    dest_dir = sbi_db.upload_dir() / month
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
        print(f"[seed] Copied to {dest}")
    else:
        print(f"[seed] File already in data dir: {dest}")
    return dest


async def _store_run(file_path: Path, month: str, row_count: int) -> None:
    """Write sbi_runs + sbi_current_run + sbi_run_files rows."""
    async with AsyncSessionLocal() as session:
        await sbi_db.set_current_run(session, str(file_path), month, row_count)
        await sbi_db.upsert_run_file(
            session, month, "raw", str(file_path),
            original_filename=file_path.name, row_count=row_count,
        )


async def _store_pf_historicals(records: list[dict]) -> None:
    if not records:
        return
    async with AsyncSessionLocal() as session:
        await sbi_db.bulk_upsert_pf_historicals(session, records)
    print(f"[seed] Stored {len(records)} PF historical rows for this month.")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(file_path: Path, month: str) -> None:
    print(f"[seed] Source file : {file_path}")
    print(f"[seed] Month       : {month}")
    print(f"[seed] Data dir    : {sbi_db.data_dir()}")
    print(f"[seed] Database    : {settings.database_url[:40]}…")
    print()

    # 1. Validate headers (fast peek — raises SchemaError on mismatch)
    print("[seed] Validating Dump sheet headers…")
    _, _, sheet_hint = ingest.validate_headers(file_path)
    print(f"       Sheet: {sheet_hint!r} ✓")

    # 2. Load the Dump into a DataFrame
    print("[seed] Loading dump rows…")
    df = ingest.load_raw(file_path, sheet_hint=sheet_hint if sheet_hint != "csv" else None)
    row_count = len(df)
    print(f"       {row_count:,} rows loaded ✓")

    # 3. Copy to data dir
    dest = _copy_to_data_dir(file_path, month)

    # 4. Store run record in DB
    print("[seed] Writing run record to DB…")
    await _store_run(dest, month, row_count)
    print("       sbi_runs + sbi_current_run written ✓")

    # 5. Run the pipeline (sync — calls db sync bridges internally)
    print("[seed] Running pipeline (may take a moment for large files)…")
    loop = asyncio.get_event_loop()
    import functools
    result = await loop.run_in_executor(
        None,
        functools.partial(pipeline.run, df, active_month=month),
    )

    warnings = result.get("warnings", [])
    sheets = result.get("sheets", {})

    for w in warnings:
        print(f"       ⚠  {w}")

    sheet_summary = {
        name: (len(df_or_dict) if hasattr(df_or_dict, "__len__") else "dict")
        for name, df_or_dict in sheets.items()
    }
    print(f"       Pipeline done ✓  sheets: {sheet_summary}")

    # 6. PF historicals are written inside pipeline.run() automatically when
    #    active_month is passed. Nothing extra needed.

    print()
    print(f"[seed] ✅  Done. Active month is {month!r}.")
    print(f"[seed]    Start the server and visit /o2c/pharma-mis/sbi/dashboard")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        print("\nUsage: python scripts/seed_sbi_from_file.py <file.xlsx> <YYYY-MM>")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"Error: file not found: {src}")
        sys.exit(1)

    month_arg = sys.argv[2]
    if len(month_arg) != 7 or month_arg[4] != "-":
        print(f"Error: month must be YYYY-MM, got: {month_arg!r}")
        sys.exit(1)

    asyncio.run(main(src, month_arg))
