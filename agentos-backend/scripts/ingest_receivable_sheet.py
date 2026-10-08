"""One-shot script: ingest a receivables workbook (.xlsx or .xlsb) into the DB snapshot table.

Usage:
    python scripts/ingest_receivable_sheet.py /path/to/Receivables-new.xlsx
    python scripts/ingest_receivable_sheet.py /path/to/Receivable-08-07.2026.xlsb
"""

import asyncio
import sys
from pathlib import Path


async def main(xlsx_path: Path) -> None:
    from app.db.session import AsyncSessionLocal
    from app.services.receivable_dashboard import try_ingest_receivable_snapshot

    print(f"Ingesting: {xlsx_path}")
    async with AsyncSessionLocal() as db:
        await try_ingest_receivable_snapshot(db, xlsx_path, source_message_id=None)
        await db.commit()
    print("Done — snapshot saved.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/ingest_receivable_sheet.py <path-to-xlsx-or-xlsb>")
        sys.exit(1)
    asyncio.run(main(Path(sys.argv[1])))
