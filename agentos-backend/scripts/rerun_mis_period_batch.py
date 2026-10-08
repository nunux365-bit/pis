#!/usr/bin/env python3
"""
Batch MIS rerun for a billing period (fresh attendance → LLM draft → optional auto-approve).

Use after you reopen runs (e.g. approved → pending_human) and updated the attendance workbook.

Prerequisites:
  - ``agenos_database_url`` / ``AGENOS_DATABASE_URL`` (or sync URL) configured in ``.env``
  - Attendance: ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID`` or ``O2C_ATTENDANCE_XLSX_PATH`` / ``O2C_OHC_ATTENDANCE_XLSX_PATH``
  - MIS template: ``O2C_INVOICE_MIS_TEMPLATE_PATH``
  - Auto-approve: ``O2C_MIS_AUTO_APPROVE_ENABLED=true`` (and rules must pass per site)

Typical flow::

    # 1) Reopen approved runs for the period (SQL example — review before running):
    # UPDATE o2c_mis_run
    # SET status = 'pending_human', updated_at = now()
    # WHERE billing_period_start = '2026-05-01'
    #   AND billing_period_end   = '2026-05-31'
    #   AND status = 'approved';

    # 2) Batch rerun + auto-approve attempt
    cd agentos-backend && set -a && source .env && set +a
    python scripts/rerun_mis_period_batch.py \\
        --period-start 2026-05-01 --period-end 2026-05-31 \\
        --status pending_human

Dry-run (list only)::

    python scripts/rerun_mis_period_batch.py --period-start 2026-05-01 --period-end 2026-05-31 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.services.o2c import mis_workflow as mw  # noqa: E402


async def _fetch_runs(
    *,
    period_start: date,
    period_end: date,
    status: str | None,
    client_site_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses = [
        "mr.billing_period_start = :ps",
        "mr.billing_period_end = :pe",
    ]
    params: dict[str, Any] = {"ps": period_start, "pe": period_end, "lim": limit}
    if status:
        clauses.append("mr.status = :st")
        params["st"] = status
    if client_site_key:
        clauses.append("trim(mr.client_site_key) = :csk")
        params["csk"] = client_site_key.strip()
    where = " AND ".join(clauses)
    q = f"""
        SELECT mr.id::text AS id,
               mr.client_site_key,
               mr.status,
               mr.approved_by,
               (SELECT COUNT(*)::int FROM o2c_mis_summary_row sr WHERE sr.mis_run_id = mr.id) AS line_count
        FROM o2c_mis_run mr
        WHERE {where}
        ORDER BY mr.client_site_key
        LIMIT :lim
    """
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(text(q), params)
            return [dict(row) for row in r.mappings().all()]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch MIS rerun for a billing period.")
    p.add_argument("--period-start", required=True, help="Billing period start YYYY-MM-DD")
    p.add_argument("--period-end", required=True, help="Billing period end YYYY-MM-DD")
    p.add_argument(
        "--status",
        default="pending_human",
        help="Only runs with this status (default: pending_human). Use '' for any status.",
    )
    p.add_argument("--client-site-key", default="", help="Optional single site alias filter")
    p.add_argument("--limit", type=int, default=500, help="Max runs to process (default 500)")
    p.add_argument("--dry-run", action="store_true", help="List matching runs only; no rerun")
    p.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="Rerun only; do not call auto-approve after each ok draft",
    )
    p.add_argument(
        "--skip-gdrive",
        action="store_true",
        help="Do not upload MIS xlsx to Google Drive after rerun",
    )
    p.add_argument(
        "--allow-expired-contract-terms",
        action="store_true",
        help="Pass allow_expired_contract_terms=True to draft refresh",
    )
    p.add_argument(
        "--json-out",
        default="",
        help="Write per-site results JSON to this path",
    )
    return p.parse_args()


async def _run_batch(args: argparse.Namespace) -> int:
    ps = date.fromisoformat(args.period_start)
    pe = date.fromisoformat(args.period_end)
    status_filter: str | None = (args.status or "").strip() or None

    runs = await _fetch_runs(
        period_start=ps,
        period_end=pe,
        status=status_filter,
        client_site_key=(args.client_site_key or "").strip() or None,
        limit=max(1, args.limit),
    )
    if not runs:
        print(f"No MIS runs for {ps}..{pe} status={status_filter!r}.")
        return 1

    print(f"Found {len(runs)} run(s) for {ps}..{pe} status={status_filter!r}")
    for row in runs:
        print(
            f"  {row['client_site_key']!r}  id={row['id']}  status={row['status']}  lines={row['line_count']}"
        )

    if args.dry_run:
        print("Dry-run: no reruns executed.")
        return 0

    auto_on = bool(getattr(settings, "o2c_mis_auto_approve_enabled", False))
    if not args.no_auto_approve and not auto_on:
        print(
            "Warning: O2C_MIS_AUTO_APPROVE_ENABLED is false — auto-approve will not approve anything.",
            file=sys.stderr,
        )

    results: list[dict[str, Any]] = []
    ok_n = fail_n = skip_n = aa_ok = 0

    for i, row in enumerate(runs, start=1):
        mid = UUID(str(row["id"]))
        csk = row.get("client_site_key") or ""
        print(f"\n[{i}/{len(runs)}] {csk} ({mid}) …", flush=True)
        entry: dict[str, Any] = {
            "mis_run_id": str(mid),
            "client_site_key": csk,
            "status_before": row.get("status"),
        }
        try:
            out = await mw.rerun_mis_for_existing_run(
                mis_run_id=mid,
                allow_expired_contract_terms=bool(args.allow_expired_contract_terms),
                finalize_gdrive=not args.skip_gdrive,
                attempt_auto_approve=not args.no_auto_approve,
                preserved_by="batch_rerun_script",
            )
            rerun = out.get("rerun") if isinstance(out.get("rerun"), dict) else {}
            entry["rerun_status"] = rerun.get("status")
            entry["rerun_reason"] = rerun.get("reason") or rerun.get("error")
            entry["final_amount_locks"] = out.get("final_amount_locks")
            entry["auto_approve"] = out.get("auto_approve")
            if str(rerun.get("status") or "") == "ok":
                ok_n += 1
                aa = out.get("auto_approve") if isinstance(out.get("auto_approve"), dict) else {}
                if aa.get("approved"):
                    aa_ok += 1
                    print(f"  ok — auto-approved (delta={aa.get('pct_delta')})")
                else:
                    reasons = aa.get("reasons") or [aa.get("reason") or rerun.get("reason")]
                    print(f"  ok — auto-approve not applied: {reasons}")
            else:
                skip_n += 1
                print(f"  skipped/failed: {entry['rerun_reason']}")
        except Exception as e:
            fail_n += 1
            entry["error"] = str(e)
            print(f"  ERROR: {e}", file=sys.stderr)
        results.append(entry)

    summary = {
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "processed": len(runs),
        "rerun_ok": ok_n,
        "rerun_not_ok": skip_n,
        "errors": fail_n,
        "auto_approved": aa_ok,
        "results": results,
    }
    print("\n--- summary ---")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {args.json_out}")

    return 0 if fail_n == 0 else 2


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run_batch(args))


if __name__ == "__main__":
    raise SystemExit(main())
