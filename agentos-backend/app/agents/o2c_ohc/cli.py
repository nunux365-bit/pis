"""
Operational CLI for O2C_OHC (local debugging and batch jobs).

**Why a CLI?** Fast, dependency-light entrypoints for engineers: parse-only probes
(``attendance-map``), one-off PDF folders, MIS template audit, roll export — without
starting the API or creating a ``WorkflowRun``.

**Platform agent:** end-to-end Phase A + B for product flows is ``run_o2c_ohc_agent`` in
``app.agents.o2c_ohc.agent``, wired to ``POST /api/workflows/trigger`` via workflow keys
``o2c_ohc``, ``o2c.ohc``, ``demo_o2c_ohc`` (see ``app.services.workflow_runner.O2C_OHC_KEYS``).

Run: ``python -m app.agents.o2c_ohc.cli ...`` from the ``agentos-backend`` directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description="O2C_OHC agent — contracts, attendance map, MIS")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("contracts", help="Scan contracts folder → LLM → agenos")
    pc.add_argument(
        "--root",
        default="",
        help="Override O2C_CONTRACTS_ROOT",
    )
    pc.add_argument(
        "--ignore-mtime-watermark",
        action="store_true",
        help="Scan all PDFs under root; skip only by sha256 (for copy/unzip with old file mtimes)",
    )

    pa = sub.add_parser("attendance-map", help="Parse Summary / pivot source → JSON map (stdout)")
    pa.add_argument("--path", default="", help="Override O2C_ATTENDANCE_XLSX_PATH")
    pa.add_argument(
        "--engine",
        choices=("auto", "roll_full", "flat", "xlwings", "google"),
        default="auto",
        help="google: A1/E1=All + iterate B1 (xlwings+Excel), else error; xlwings: same without snapshot fallback",
    )

    pm = sub.add_parser(
        "mis-audit",
        help='Check {{placeholders}} in MIS template vs canonical keys (e.g. "Standardized MIS Format - OHC.xlsx")',
    )
    pm.add_argument(
        "--template",
        required=True,
        help="Path to Standardized MIS Format - OHC.xlsx",
    )

    pe = sub.add_parser(
        "export-roll-summary",
        help="On Roll + Off Roll wide tabs → new xlsx, one Summary-style sheet per client-site",
    )
    pe.add_argument("--src", required=True, help="Source OHC attendance .xlsx")
    pe.add_argument("--out", required=True, help="Output .xlsx path")
    pe.add_argument(
        "--year-month",
        default="",
        help="Optional YYYY-MM to limit date columns (e.g. 2026-02). Empty = all header dates.",
    )
    pe.add_argument(
        "--validate",
        action="store_true",
        help="If Summary!B1 matches a built sheet, print validation JSON for that site",
    )
    pe.add_argument(
        "--no-color",
        action="store_true",
        help="Disable present/absent/week-off/leave/locum cell fills (plain values only)",
    )

    pg = sub.add_parser("graph", help="Run LangGraph pipeline (contracts → shared-resource resolve → MIS)")
    pg.add_argument("--root", default="", help="Contracts root override")
    pg.add_argument("--attendance", default="", help="Attendance xlsx override")
    pg.add_argument("--from", dest="period_start", default="", help="YYYY-MM-DD")
    pg.add_argument("--to", dest="period_end", default="", help="YYYY-MM-DD")
    pg.add_argument(
        "--invoice-out",
        default="",
        help="MIS xlsx output directory (alias; same as mis_out_dir in graph state)",
    )

    args = p.parse_args(argv)

    if args.cmd == "contracts":
        from app.agents.o2c_ohc.pipeline import run_o2c_pipeline

        r = run_o2c_pipeline(
            contracts_root=args.root or None,
            ignore_mtime_watermark=args.ignore_mtime_watermark,
        )
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("failed", 0) == 0 else 2

    if args.cmd == "attendance-map":
        from app.agents.o2c_ohc.attendance_summary import parse_ohc_summary_workbook
        from app.config.settings import settings

        raw = (
            args.path
            or settings.o2c_attendance_xlsx_path
            or settings.o2c_ohc_attendance_xlsx_path
            or ""
        ).strip()
        if not raw:
            print("Set --path or O2C_ATTENDANCE_XLSX_PATH", file=sys.stderr)
            return 2
        path = Path(raw).expanduser()
        m = parse_ohc_summary_workbook(path, engine=args.engine)
        print(json.dumps({k: len(v) for k, v in m.items()}, indent=2))
        return 0

    if args.cmd == "mis-audit":
        from app.agents.o2c_ohc.mis_template_audit import audit_mis_template

        r = audit_mis_template(Path(args.template))
        print(json.dumps(r.to_dict(), indent=2))
        return 0 if r.can_fill_all_placeholders else 3

    if args.cmd == "export-roll-summary":
        from app.agents.o2c_ohc.roll_to_summary_export import (
            export_roll_tabs_to_summary_workbook,
            validate_site_against_summary_tab,
        )
        from openpyxl import load_workbook

        src = Path(args.src).expanduser()
        out = Path(args.out).expanduser()
        ym: tuple[int, int] | None = None
        raw_ym = (args.year_month or "").strip()
        if raw_ym:
            parts = raw_ym.replace("/", "-").split("-")
            if len(parts) != 2:
                print("--year-month must be YYYY-MM", file=sys.stderr)
                return 2
            ym = (int(parts[0]), int(parts[1]))
        info = export_roll_tabs_to_summary_workbook(
            src, out, year_month=ym, apply_colors=not args.no_color
        )
        print(json.dumps(info, indent=2))
        if args.validate:
            wb = load_workbook(out, read_only=False, data_only=True)
            try:
                src_wb = load_workbook(src, read_only=False, data_only=True)
                try:
                    ref_b1 = src_wb["Summary"].cell(1, 2).value
                finally:
                    src_wb.close()
                ref = str(ref_b1).strip() if ref_b1 else ""
                for sn in wb.sheetnames:
                    w = wb[sn]
                    if str(w.cell(1, 2).value or "").strip() == ref:
                        v = validate_site_against_summary_tab(src, w)
                        print(json.dumps(v, indent=2, default=str))
                        break
                else:
                    print(
                        json.dumps(
                            {"validate": "skipped", "reason": f"no sheet with B1={ref!r}"},
                            indent=2,
                        )
                    )
            finally:
                wb.close()
        return 0

    if args.cmd == "graph":
        from app.agents.o2c_ohc.graph import run_o2c_full_graph

        state: dict = {}
        if args.root:
            state["contracts_root"] = args.root
        if args.attendance:
            state["attendance_xlsx"] = args.attendance
        if args.period_start:
            state["period_start"] = args.period_start
        if args.period_end:
            state["period_end"] = args.period_end
        if args.invoice_out:
            state["invoice_out_dir"] = args.invoice_out
        out = run_o2c_full_graph(state)
        print(json.dumps(out, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
