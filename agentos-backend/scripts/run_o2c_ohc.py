#!/usr/bin/env python3
"""CLI: run O2C contract folder ingest (uses O2C_CONTRACTS_ROOT / DATABASE_URL from env)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Run from repo: cd agentos-backend && python scripts/run_o2c_ohc.py
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def _main() -> None:
    from app.o2c.runner import run_o2c_folder_ingest

    p = argparse.ArgumentParser(description="O2C_OHC PDF ingest")
    p.add_argument("--root", type=str, default=None, help="Override O2C_CONTRACTS_ROOT")
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument(
        "--ignore-mtime-watermark",
        action="store_true",
        help="Scan all PDFs; skip only duplicates by sha256 (copy/unzip may preserve old mtimes)",
    )
    args = p.parse_args()
    r = await run_o2c_folder_ingest(
        contracts_root=args.root,
        max_files=args.max_files,
        ignore_mtime_watermark=args.ignore_mtime_watermark,
    )
    print(json.dumps(r, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
