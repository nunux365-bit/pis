#!/usr/bin/env python3
"""
sbi_mis_clean.py — Wipe all SBI MIS data for a clean-slate test or staging reset.

What it clears
--------------
Pipeline / billing data:
  sbi_jobs            background job queue / status log
  sbi_run_files       uploaded file registry (raw, ahc, wallet_checker, pf_summary)
  sbi_pf_historicals  per-PF monthly totals used for D/E/F/G historicals columns
  sbi_current_run     pointer to the currently active month
  sbi_runs            upload history shown on Dashboard

Config tables (re-seeded by bootstrap on next server startup):
  sbi_rules           column computation rules
  sbi_column_formats  number-format overrides
  sbi_sheet_columns   user-added columns
  sbi_lookup_tables   key/value lookup tables (e.g. pf_wallet_limits)

Disk files:
  {SBI_MIS_DATA_DIR}/uploads/        uploaded xlsx / csv files
  {SBI_MIS_DATA_DIR}/pipeline_cache/ pickled pipeline results

After running, restart the server.  Bootstrap will re-seed sbi_rules (and
other config tables) from the current format_spec.py automatically.

Usage
-----
    # from project root with the virtualenv active:
    python scripts/sbi_mis_clean.py

    # skip the confirmation prompt (e.g. in CI):
    python scripts/sbi_mis_clean.py --yes
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
    except ImportError:
        pass  # rely on shell env

# ── App imports (after sys.path and env are set) ─────────────────────────────
from app.config.settings import settings  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
import app.db.models  # noqa: F401, E402
import app.sbi_mis.models  # noqa: F401, E402

from sqlalchemy import delete  # noqa: E402
from app.sbi_mis.models import (  # noqa: E402
    SbiJob,
    SbiRunFile,
    SbiPfHistorical,
    SbiCurrentRun,
    SbiRun,
    SbiRule,
    SbiColumnFormat,
    SbiSheetColumn,
    SbiLookupTable,
)
from app.sbi_mis import db as sbi_db  # noqa: E402


async def main(skip_confirm: bool = False) -> None:
    data_dir = sbi_db.data_dir()
    print(f"Database : {settings.database_url[:60]}…")
    print(f"Data dir : {data_dir}")
    print()

    if not skip_confirm:
        answer = input("This will permanently delete all SBI MIS data. Continue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    # ── Database ─────────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        pipeline_models = [
            ("sbi_jobs",           SbiJob),
            ("sbi_run_files",      SbiRunFile),
            ("sbi_pf_historicals", SbiPfHistorical),
            ("sbi_current_run",    SbiCurrentRun),
            ("sbi_runs",           SbiRun),
        ]
        config_models = [
            ("sbi_rules",          SbiRule),
            ("sbi_column_formats", SbiColumnFormat),
            ("sbi_sheet_columns",  SbiSheetColumn),
            ("sbi_lookup_tables",  SbiLookupTable),
        ]

        print("Clearing pipeline tables:")
        for name, model in pipeline_models:
            result = await session.execute(delete(model))
            print(f"  {name}: {result.rowcount} rows deleted")

        print("Clearing config tables (re-seeded by bootstrap on next startup):")
        for name, model in config_models:
            result = await session.execute(delete(model))
            print(f"  {name}: {result.rowcount} rows deleted")

        await session.commit()

    print()

    # ── Disk files ────────────────────────────────────────────────────────────
    print("Clearing disk files:")
    for subdir in ("uploads", "pipeline_cache"):
        d = data_dir / subdir
        if d.exists():
            shutil.rmtree(d)
            print(f"  Deleted  {d}")
        d.mkdir(parents=True, exist_ok=True)
        print(f"  Created  {d}  (empty)")

    print()
    print("Done.  Restart the server — bootstrap will re-seed rules automatically.")


if __name__ == "__main__":
    skip = "--yes" in sys.argv
    asyncio.run(main(skip_confirm=skip))
