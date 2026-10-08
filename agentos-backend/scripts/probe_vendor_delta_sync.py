#!/usr/bin/env python3
"""Probe vendor delta: ``A_Supplier`` day filter → ``A_SupplierCompany`` by Supplier id.

Usage (from ``agentos-backend/``)::

  python scripts/probe_vendor_delta_sync.py
  python scripts/probe_vendor_delta_sync.py --day 2024-06-01
  python scripts/probe_vendor_delta_sync.py --supplier 1000005974
  python scripts/probe_vendor_delta_sync.py --supplier 1000008083 --upsert
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from app.config.settings import settings
    from app.procurement.reference_sync.dates import vendor_daily_filter
    from app.procurement.reference_sync.odata_client import ODataClient, sap_master_configured
    from app.procurement.reference_sync.vendor_fetch import (
        fetch_delta_supplier_ids,
        fetch_vendor_company_for_suppliers,
        upsert_vendors_for_supplier_ids,
    )
    from app.procurement.sap_odata_utils import odata_text

    p = argparse.ArgumentParser(description="Probe SAP vendor delta + company enrich.")
    p.add_argument("--day", type=date.fromisoformat, default=None, help="UTC day (default: yesterday)")
    p.add_argument("--supplier", action="append", dest="suppliers", help="Test one Supplier id (repeatable)")
    p.add_argument(
        "--upsert",
        action="store_true",
        help="Upsert probed supplier(s) into pr_po_reference_values (requires --supplier).",
    )
    args = p.parse_args()

    if args.upsert and not args.suppliers:
        print("--upsert requires at least one --supplier", file=sys.stderr)
        return 2

    if not sap_master_configured():
        print("PROCUREMENT_SAP_* not configured", file=sys.stderr)
        return 2

    async def _run() -> int:
        from app.procurement.reference_sync import dates as sync_dates

        day = args.day or sync_dates.sync_yesterday_utc()
        print(f"SAP: {settings.procurement_sap_base_url}")
        print(f"Day filter: {vendor_daily_filter(day)}")

        async with ODataClient.open() as odata:
            if args.suppliers:
                ids = [s.strip() for s in args.suppliers if s.strip()]
            else:
                ids = await fetch_delta_supplier_ids(odata, day=day)
            print(f"Supplier ids: {len(ids)}")
            for sid in ids[:10]:
                print(f"  {sid}")
            if len(ids) > 10:
                print(f"  ... +{len(ids) - 10} more")

            probe_ids = ids if args.suppliers else ids[:5]
            props = await fetch_vendor_company_for_suppliers(odata, probe_ids)
            print(f"Company rows ({len(probe_ids)} supplier id(s) probed): {len(props)}")
            for pr in props[:8]:
                print(
                    f"  {odata_text(pr.get('Supplier'))}|{odata_text(pr.get('CompanyCode'))}"
                    f" payt={odata_text(pr.get('PaymentTerms'))!r}"
                )

            if args.upsert:
                ids = [s.strip() for s in (args.suppliers or []) if s.strip()]
                mapped, inserted, updated = await upsert_vendors_for_supplier_ids(ids)
                print(f"Upserted: mapped={mapped} inserted={inserted} updated={updated}")
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
