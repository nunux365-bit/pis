#!/usr/bin/env python3
"""
April 2026: compare attendance LWD/DOJ handover prep vs stored MIS runs (read-only).

Uses REMINDER_SENDS_DATABASE_URL when set (prod), else DATABASE_URL.
Downloads/parses the configured OHC attendance workbook from Drive (or local path).

Usage (from agentos-backend, venv active)::

    set -a && source .env && set +a
    python scripts/test_april_handover_mis_attendance.py
    python scripts/test_april_handover_mis_attendance.py --json /tmp/april_handover.json
    python scripts/test_april_handover_mis_attendance.py --site "TACO TTR" --rerun-one
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.o2c_ohc.manpower_clinical_hint import append_manpower_period_churn_rows  # noqa: E402
from app.agents.o2c_ohc.mis_drafts import (  # noqa: E402
    _apply_fmo_mo_attendance_hints,
    _attendance_records_for_period,
    _rate_lines_for_site_async,
)
from app.agents.o2c_ohc.mis_handover_attendance import (  # noqa: E402
    apply_handover_attendance_prep,
    parse_sheet_date,
)
from app.agents.o2c_ohc.mis_gdrive_finalize import build_mis_drive_svc  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.services.o2c.attendance_workbook_cache import load_parsed_ohc_attendance_workbook_cached  # noqa: E402

APRIL_START = date(2026, 4, 1)
APRIL_END = date(2026, 4, 30)


def _db_url() -> str:
    raw = (os.environ.get("REMINDER_SENDS_DATABASE_URL") or settings.database_url or "").strip()
    if raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


def _summary_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _staff_churn(records: list[dict[str, Any]], *, ps: date, pe: date) -> dict[str, int]:
    leavers = joiners = both = 0
    for rec in records:
        lwd = parse_sheet_date(rec.get("lwd"), period_start=ps)
        doj = parse_sheet_date(rec.get("doj"), period_start=ps)
        in_lwd = lwd is not None and ps <= lwd <= pe
        in_doj = doj is not None and ps <= doj <= pe
        if in_lwd and in_doj:
            both += 1
        elif in_lwd:
            leavers += 1
        elif in_doj:
            joiners += 1
    return {"leavers": leavers, "joiners": joiners, "both_in_month": both}


def _mis_row_snapshot(session_rows: list[dict[str, Any]]) -> dict[str, Any]:
    active = [r for r in session_rows if not r.get("is_omitted")]
    omitted = [r for r in session_rows if r.get("is_omitted")]
    return {
        "active_employee_rows": len(active),
        "omitted_rows": len(omitted),
        "names_with_comma": [
            {
                "eid": r.get("employee_external_id"),
                "name": r.get("employee_name"),
                "crl": r.get("contract_rate_line_id"),
            }
            for r in active
            if isinstance(r.get("employee_name"), str) and "," in r["employee_name"]
        ],
    }


async def _fetch_april_runs(session: AsyncSession) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT mr.id::text AS mis_run_id,
               mr.client_site_key,
               mr.status,
               mr.service_site_id::text AS service_site_id,
               mr.contract_terms_version_id::text AS contract_terms_version_id,
               mr.summary_json
        FROM o2c_mis_run mr
        WHERE mr.billing_period_start = CAST(:ps AS date)
          AND mr.billing_period_end = CAST(:pe AS date)
        ORDER BY mr.client_site_key
        """),
        {"ps": APRIL_START, "pe": APRIL_END},
    )
    return [dict(row) for row in r.mappings().all()]


async def _fetch_summary_rows(session: AsyncSession, mis_run_id: str) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT contract_rate_line_id::text AS contract_rate_line_id,
               employee_external_id,
               employee_name,
               is_omitted,
               omit_reason,
               final_amount
        FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid)
        ORDER BY contract_rate_line_id, employee_external_id
        """),
        {"mid": mis_run_id},
    )
    return [dict(row) for row in r.mappings().all()]


async def analyze_site(
    session: AsyncSession,
    *,
    run: dict[str, Any],
    amap: dict[str, list[dict[str, Any]]],
    workbook_path: Path | None,
) -> dict[str, Any]:
    key = str(run["client_site_key"] or "").strip()
    records = list(amap.get(key) or [])
    # Cache enrich: hints + DOJ/LWD backfill only; period churn stubs match MIS draft path.
    if workbook_path and workbook_path.is_file():
        records = append_manpower_period_churn_rows(
            records,
            workbook_path,
            key,
            period_start=APRIL_START,
            period_end=APRIL_END,
        )
    att = _attendance_records_for_period(
        records, period_start=APRIL_START, period_end=APRIL_END
    )
    lines = await _rate_lines_for_site_async(
        session,
        str(run["contract_terms_version_id"]),
        str(run["service_site_id"]),
    )
    _apply_fmo_mo_attendance_hints(att, lines)
    prep = apply_handover_attendance_prep(
        att, lines, period_start=APRIL_START, period_end=APRIL_END
    )

    sj = _summary_json(run.get("summary_json"))
    stored_handover = sj.get("handover") if isinstance(sj.get("handover"), dict) else {}
    db_rows = await _fetch_summary_rows(session, str(run["mis_run_id"]))
    suppressed = set(stored_handover.get("suppressed_employee_ids") or prep.handover.get("suppressed_employee_ids") or [])

    db_leaver_omitted = [
        r["employee_external_id"]
        for r in db_rows
        if r.get("employee_external_id") in suppressed and r.get("is_omitted")
    ]
    db_leaver_active = [
        r["employee_external_id"]
        for r in db_rows
        if r.get("employee_external_id") in suppressed and not r.get("is_omitted")
    ]

    churn = _staff_churn(records, ps=APRIL_START, pe=APRIL_END)
    would_pair = bool(prep.handover.get("applied"))
    stored_applied = bool(stored_handover.get("applied"))

    if churn["leavers"] and churn["joiners"]:
        churn_reason = "leavers_and_joiners_in_attendance"
    elif churn["leavers"]:
        churn_reason = "leavers_only_no_april_joiner_on_roll"
    elif churn["joiners"]:
        churn_reason = "joiners_only_no_april_leaver_on_roll"
    else:
        churn_reason = "none"

    issues: list[str] = []
    if churn["leavers"] or churn["joiners"]:
        if would_pair and not stored_applied and run.get("status") in ("approved", "pending_human"):
            issues.append("attendance_would_handover_but_stored_mis_has_no_handover_flag")
        if would_pair and db_leaver_active:
            issues.append("leaver_still_active_in_db_rows_after_handover_expected")
        if stored_applied and not would_pair:
            issues.append("stored_handover_but_prep_would_not_pair_now")
        if would_pair and not db_leaver_omitted and run.get("status") != "skipped":
            issues.append("handover_prep_ok_but_leaver_not_omitted_in_db")

    return {
        "client_site_key": key,
        "mis_run_id": run["mis_run_id"],
        "status": run["status"],
        "attendance_staff": len(records),
        "churn": churn,
        "churn_reason": churn_reason,
        "prep_handover": prep.handover,
        "stored_handover": stored_handover,
        "att_after_prep": len(prep.records),
        "db_rows": _mis_row_snapshot(db_rows),
        "db_leaver_omitted": db_leaver_omitted,
        "db_leaver_still_active": db_leaver_active,
        "issues": issues,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Write full report JSON")
    parser.add_argument("--site", action="append", help="Filter client_site_key (repeatable)")
    parser.add_argument(
        "--rerun-one",
        action="store_true",
        help="Re-run MIS draft for --site only (writes DB; requires template path)",
    )
    args = parser.parse_args()

    url = _db_url()
    if not url:
        print("No DATABASE_URL / REMINDER_SENDS_DATABASE_URL", file=sys.stderr)
        return 1

    engine = create_async_engine(url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    cleanup: list[Path] = []
    drive_svc = build_mis_drive_svc()
    wb = load_parsed_ohc_attendance_workbook_cached(
        explicit=None, cleanup_paths=cleanup, drive_svc=drive_svc, engine="auto"
    )
    amap = wb.amap
    if not amap:
        print("Failed to load attendance workbook", file=sys.stderr)
        await engine.dispose()
        return 1

    report: dict[str, Any] = {
        "period": f"{APRIL_START}..{APRIL_END}",
        "db_url_host": url.split("@")[-1].split("/")[0] if "@" in url else "local",
        "workbook_sites": len(amap),
        "sites": [],
        "summary": {},
    }

    async with Session() as session:
        runs = await _fetch_april_runs(session)
        if args.site:
            filt = {s.strip() for s in args.site}
            runs = [r for r in runs if r["client_site_key"] in filt]

        wb_path = wb.resolved_path if wb.resolved_path and wb.resolved_path.is_file() else None
        for run in runs:
            site_report = await analyze_site(session, run=run, amap=amap, workbook_path=wb_path)
            report["sites"].append(site_report)

        if args.rerun_one:
            if not args.site or len(args.site) != 1:
                print("--rerun-one requires exactly one --site", file=sys.stderr)
                await engine.dispose()
                return 1
            from app.agents.o2c_ohc.mis_drafts import create_or_refresh_mis_draft_for_site_async
            from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records

            key = args.site[0].strip()
            records = amap.get(key) or []
            tpl = Path(settings.o2c_invoice_mis_template_path or "")
            out_dir = Path(settings.o2c_mis_out_dir or settings.o2c_invoice_out_dir or "/tmp/o2c_mis")
            out_dir.mkdir(parents=True, exist_ok=True)
            det = build_detailed_json_from_records(
                key, records, period_start=APRIL_START, period_end=APRIL_END
            )
            print(f"Re-running MIS draft for {key!r} …")
            res = await create_or_refresh_mis_draft_for_site_async(
                client_site_key=key,
                attendance_records=records,
                detailed_json=det,
                period_start=APRIL_START,
                period_end=APRIL_END,
                out_dir=out_dir,
                template_path=tpl,
                attendance_workbook_path=wb.resolved_path,
                allow_expired_contract_terms=bool(settings.o2c_mis_allow_expired_contract_terms),
            )
            print(res)
            run = next((r for r in runs if r["client_site_key"] == key), None)
            if run:
                run["summary_json"] = (
                    await session.execute(
                        text("SELECT summary_json FROM o2c_mis_run WHERE id = CAST(:mid AS uuid)"),
                        {"mid": run["mis_run_id"]},
                    )
                ).mappings().first()["summary_json"]
                report["sites"] = [
                    await analyze_site(session, run=run, amap=amap, workbook_path=wb_path)
                ]

    churn_sites = [s for s in report["sites"] if s["churn"]["leavers"] or s["churn"]["joiners"]]
    would = [s for s in report["sites"] if s["prep_handover"].get("applied")]
    issues = [s for s in report["sites"] if s["issues"]]

    report["summary"] = {
        "april_mis_runs": len(report["sites"]),
        "sites_with_lwd_or_doj_churn": len(churn_sites),
        "sites_prep_would_apply_handover": len(would),
        "sites_with_issues": len(issues),
    }

    print(f"April MIS runs: {report['summary']['april_mis_runs']} ({report['db_url_host']})")
    print(
        f"Churn sites: {report['summary']['sites_with_lwd_or_doj_churn']} | "
        f"Would handover: {report['summary']['sites_prep_would_apply_handover']} | "
        f"Issues: {report['summary']['sites_with_issues']}"
    )
    for s in issues[:25]:
        print(f"  ISSUE {s['client_site_key']}: {', '.join(s['issues'])}")
    for s in would[:15]:
        pairs = s["prep_handover"].get("pairs") or []
        print(
            f"  HANDOVER {s['client_site_key']} ({s['status']}): "
            f"{len(pairs)} pair(s) -> {[p.get('joiner_display_name') for p in pairs]}"
        )
    churn_only = [
        s for s in report["sites"] if s["churn"]["leavers"] or s["churn"]["joiners"]
    ]
    if churn_only and not would:
        print("Churn sites (handover not applied — pairing needs leaver+joiner on same roll):")
        for s in churn_only:
            print(
                f"  {s['client_site_key']} ({s['status']}): "
                f"leavers={s['churn']['leavers']} joiners={s['churn']['joiners']} — {s['churn_reason']}"
            )

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {args.json}")

    await engine.dispose()
    return 1 if issues and not args.rerun_one else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
