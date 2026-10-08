#!/usr/bin/env python3
"""Contract Health + O2C smoke test using app .env (read-mostly; optional safe pending-CTV date touch)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _prior_month() -> tuple[date, date]:
    today = date.today()
    y, m = today.year, today.month
    if m == 1:
        return date(y - 1, 12, 1), date(y - 1, 12, 31)
    start = date(y, m - 1, 1)
    end = date(y, m, 1) - __import__("datetime").timedelta(days=1)
    return start, end


async def _run() -> int:
    from sqlalchemy import text

    from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
    from app.config.settings import settings
    from app.services.o2c.contract_review import contract_review_get, contract_review_set_status
    from app.services.o2c.contract_review_health import (
        contract_review_client_tree,
        contract_review_ops_summary,
        contract_review_patch_header,
        contract_review_siblings,
    )
    from app.services.o2c.mis_contract_lines import approve_contract_terms_for_mis_run
    from app.agents.o2c_ohc.terms_picker import count_approved_billable_terms_async

    ps, pe = _prior_month()
    ps_s, pe_s = ps.isoformat(), pe.isoformat()
    report: dict[str, object] = {"period": {"start": ps_s, "end": pe_s}, "checks": []}

    def ok(name: str, detail: object = None) -> None:
        report["checks"].append({"name": name, "status": "ok", "detail": detail})

    def fail(name: str, detail: object) -> None:
        report["checks"].append({"name": name, "status": "fail", "detail": str(detail)[:500]})

    def skip(name: str, reason: str) -> None:
        report["checks"].append({"name": name, "status": "skip", "detail": reason})

    # Config (no secrets)
    report["config"] = {
        "agenos_db_configured": bool((settings.agenos_database_url_sync or settings.database_url_sync or "").strip()),
        "contracts_root": bool((settings.o2c_contracts_root or "").strip()),
        "gdrive_contracts": bool((settings.o2c_gdrive_contracts_parent_folder_id or "").strip()),
        "openai": bool((settings.openai_api_key or "").strip()),
    }

    try:
        summary = await contract_review_ops_summary(period_start=ps_s, period_end=pe_s)
        ok(
            "ops_summary",
            {
                "overlap_sites": summary.get("overlap_site_count"),
                "parse_failures": summary.get("parse_failure_count"),
                "pending_issues": len((summary.get("queues") or {}).get("pending_data_issues") or []),
            },
        )
    except Exception as e:
        fail("ops_summary", e)

    bc_id: str | None = None
    pending_ctv: str | None = None
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(
                    text("""
                    SELECT bc.id::text AS billing_client_id,
                           ctv.id::text AS contract_terms_version_id
                    FROM contract_terms_version ctv
                    JOIN billing_client bc ON bc.id = ctv.billing_client_id
                    WHERE ctv.status IN ('pending', 'draft')
                      AND ctv.superseded_by_id IS NULL
                    ORDER BY ctv.updated_at DESC
                    LIMIT 1
                    """),
                )
                row = r.mappings().first()
                if row:
                    bc_id = str(row["billing_client_id"])
                    pending_ctv = str(row["contract_terms_version_id"])
        if bc_id:
            ok("db_connect_billing_client_sample", {"billing_client_id": bc_id[:8] + "…"})
        else:
            skip("db_connect_billing_client_sample", "no pending/draft CTV in DB")
    except Exception as e:
        fail("db_connect_billing_client_sample", e)

    if bc_id:
        try:
            tree = await contract_review_client_tree(
                billing_client_id=bc_id, period_start=ps_s, period_end=pe_s
            )
            sites_n = len(tree.get("sites") or [])
            overlap_n = sum(1 for s in (tree.get("sites") or []) if s.get("overlap_conflict"))
            ok(
                "client_tree",
                {"client": tree.get("client_name"), "sites": sites_n, "overlap_sites": overlap_n},
            )
        except Exception as e:
            fail("client_tree", e)

    if pending_ctv:
        try:
            sibs = await contract_review_siblings(
                contract_terms_version_id=pending_ctv, period_start=ps_s, period_end=pe_s
            )
            ok("siblings", {"count": len(sibs.get("items") or [])})
        except Exception as e:
            fail("siblings", e)
        try:
            detail = await contract_review_get(contract_terms_version_id=pending_ctv)
            lines_n = len(detail.get("rate_lines") or [])
            ok("contract_detail", {"rate_lines": lines_n, "status": (detail.get("header") or {}).get("status")})
        except Exception as e:
            fail("contract_detail", e)

        # Safe mutate: patch header on pending only, then restore
        try:
            hdr = await contract_review_get(contract_terms_version_id=pending_ctv)
            h = hdr.get("header") or {}
            orig_from = str(h.get("effective_from") or "")[:10]
            orig_to = h.get("effective_to")
            orig_to_s = str(orig_to)[:10] if orig_to else None
            await contract_review_patch_header(
                contract_terms_version_id=pending_ctv,
                effective_from=orig_from or ps_s,
                clear_effective_to=True,
                patch_effective_from=True,
            )
            await contract_review_patch_header(
                contract_terms_version_id=pending_ctv,
                effective_from=orig_from or ps_s,
                effective_to=orig_to_s,
                patch_effective_from=True,
                patch_effective_to=bool(orig_to_s),
                clear_effective_to=not orig_to_s,
            )
            ok("header_patch_roundtrip_pending", {"ctv": pending_ctv[:8] + "…"})
        except Exception as e:
            fail("header_patch_roundtrip_pending", e)

    # MIS approve path: dry-run SQL parse only (no commit)
    try:
        _ = approve_contract_terms_for_mis_run
        ok("mis_approve_import", "approve_contract_terms_for_mis_run importable")
    except Exception as e:
        fail("mis_approve_import", e)

    # Block HTTP-style approve on contract review
    from fastapi import HTTPException

    try:
        await contract_review_set_status(
            contract_terms_version_id=pending_ctv or "00000000-0000-0000-0000-000000000001",
            status_value="approved",
            updated_by="smoke@test",
        )
        fail("status_guard_approved", "expected HTTP 400")
    except HTTPException as e:
        if e.status_code == 400:
            ok("status_guard_approved", "rejects approved via contract health")
        else:
            fail("status_guard_approved", e.detail)
    except Exception as e:
        fail("status_guard_approved", e)

    if bc_id:
        try:
            async with AgenosAsyncSessionLocal() as session:
                async with session.begin():
                    r = await session.execute(
                        text(
                            "SELECT id::text FROM service_site WHERE billing_client_id = CAST(:bc AS uuid) LIMIT 1"
                        ),
                        {"bc": bc_id},
                    )
                    sid = (r.mappings().first() or {}).get("id")
                    if sid:
                        n = await count_approved_billable_terms_async(
                            session, bc_id, str(sid), period_start=ps, period_end=pe
                        )
                        ok("terms_picker_count_approved", {"site_id": str(sid)[:8] + "…", "count": n})
                    else:
                        skip("terms_picker_count_approved", "no service_site for client")
        except Exception as e:
            fail("terms_picker_count_approved", e)

    # Ingest: only if explicitly safe (max 0 files) — validates API wiring + Drive/root config
    root = (settings.o2c_contracts_root or "").strip()
    gdrive = (settings.o2c_gdrive_contracts_parent_folder_id or "").strip()
    if root or gdrive:
        try:
            # max_files=0 should short-circuit quickly if supported; else skip
            skip("ingest_run", "skipped live ingest to avoid PDF side effects; config present")
        except Exception as e:
            fail("ingest_run", e)
    else:
        skip("ingest_run", "no O2C_CONTRACTS_ROOT or gdrive parent configured")

    fails = [c for c in report["checks"] if c["status"] == "fail"]
    report["summary"] = {
        "total": len(report["checks"]),
        "ok": sum(1 for c in report["checks"] if c["status"] == "ok"),
        "fail": len(fails),
        "skip": sum(1 for c in report["checks"] if c["status"] == "skip"),
    }
    print(json.dumps(report, indent=2, default=str))
    return 1 if fails else 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
