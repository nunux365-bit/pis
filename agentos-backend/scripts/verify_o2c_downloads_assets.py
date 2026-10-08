#!/usr/bin/env python3
"""Verify O2C assets under ~/Downloads/agentos (MIS template + attendance workbook)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.o2c_ohc.mis_template_audit import audit_mis_template
from app.agents.o2c_ohc.pivot_slicer_attendance import try_parse_attendance_via_pivot_slicers
from app.agents.o2c_ohc.summary_grid_attendance import (
    detect_summary_dropdown_workbook,
    iter_client_sites_from_manpower,
    parse_summary_formula_grid_snapshot,
)


def main() -> int:
    base = Path.home() / "Downloads" / "agentos"
    att = base / "OHC Attendance (Feb-26) (1-28).xlsx"
    mis = base / "Standardized MIS Format - OHC.xlsx"
    if not att.is_file() or not mis.is_file():
        print("Expected:", att, mis, file=sys.stderr)
        return 2

    # Fast checks (zip-only pivot probe returns None for this workbook)
    pivot_none = try_parse_attendance_via_pivot_slicers(att) is None
    dd = detect_summary_dropdown_workbook(att)

    # One full workbook read for attendance snapshot + site list (expensive ~30–60s on 9MB file)
    snap = parse_summary_formula_grid_snapshot(att)
    sites = iter_client_sites_from_manpower(att, max_row=2500)

    report = {
        "attendance_path": str(att),
        "mis_path": str(mis),
        "pivot_xml_parser_returns_none": pivot_none,
        "summary_dropdown_layout_detected": dd,
        "manpower_ab_site_count": len(sites),
        "snapshot_client_site_keys": list(snap.keys()) if snap else [],
        "snapshot_employee_count": len(next(iter(snap.values()))) if snap else 0,
        "mis_audit": audit_mis_template(mis).to_dict(),
    }
    if snap:
        k = next(iter(snap))
        report["snapshot_sample_roles"] = list({r.get("role_code") for r in snap[k]})[:10]

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
