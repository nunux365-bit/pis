#!/usr/bin/env python3
"""Re-export ``reference_fixed_master.json`` from the current DB (fixed domains only).

Usage (from ``agentos-backend/``)::

  python scripts/export_pr_po_reference_fixed_master.py
  python scripts/export_pr_po_reference_fixed_master.py --out path/to.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import OrderedDict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.reference_domains import FIXED_MASTER_DOMAINS
from app.procurement.reference_fixed_loader import DEFAULT_JSON_PATH


def main() -> int:
    from sqlalchemy import select

    from app.db.models import PrPoReferenceValue
    from app.db.session import AsyncSessionLocal

    p = argparse.ArgumentParser(description="Export fixed procurement reference master to JSON.")
    p.add_argument("--out", type=Path, default=DEFAULT_JSON_PATH)
    args = p.parse_args()

    async def _run() -> int:
        by_key: OrderedDict[tuple[str, str, str, str], dict] = OrderedDict()
        async with AsyncSessionLocal() as session:
            for dom in sorted(FIXED_MASTER_DOMAINS):
                res = await session.execute(
                    select(PrPoReferenceValue)
                    .where(PrPoReferenceValue.domain == dom)
                    .order_by(PrPoReferenceValue.sort_order, PrPoReferenceValue.code)
                )
                for r in res.scalars():
                    k = (r.domain, r.document_type or "", r.code, r.applies_to_kind or "")
                    by_key[k] = {
                        "domain": r.domain,
                        "document_type": r.document_type or "",
                        "applies_to_kind": r.applies_to_kind or "",
                        "code": r.code,
                        "label": r.label,
                        "sort_order": r.sort_order,
                        "extra": r.extra,
                    }
        rows = [
            r
            for r in by_key.values()
            if "and so on" not in str(r.get("code") or "").lower()
        ]
        payload = {
            "version": 1,
            "description": "Non-SAP procurement reference master. Load after SAP sync.",
            "rows": rows,
        }
        out = args.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(rows)} row(s) to {out}")
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
