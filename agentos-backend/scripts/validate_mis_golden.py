#!/usr/bin/env python3
"""
Validate MIS golden JSON fixtures (shape + optional totals). Exit 1 on any failure.

Usage:
  python scripts/validate_mis_golden.py
  python scripts/validate_mis_golden.py --fixtures-dir /path/to/mis_golden
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description="Validate MIS golden fixture JSON files.")
    p.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_repo_root() / "tests" / "fixtures" / "mis_golden",
        help="Directory containing manifest.json and *.json fixtures",
    )
    p.add_argument(
        "--no-totals",
        action="store_true",
        help="Skip mis_totals_consistent check when manifest requests it",
    )
    args = p.parse_args()
    root = args.fixtures_dir.expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(_repo_root()))

    from app.agents.o2c_ohc.mis_golden_validate import (
        mis_totals_consistent,
        summary_rows_include_role_code,
        validate_mis_summary_shape,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed = False
    for entry in manifest.get("fixtures") or []:
        name = entry.get("file")
        if not name:
            continue
        path = root / name
        if not path.is_file():
            print(f"MISSING_FILE {path}", file=sys.stderr)
            failed = True
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        errs = validate_mis_summary_shape(data)
        if errs:
            print(f"FAIL {name} shape: {errs}")
            failed = True
            continue
        exp = entry.get("expect") or {}
        rows = data.get("summary_rows") or []
        min_rows = int(exp.get("min_rows") or 0)
        bad = False
        if len(rows) < min_rows:
            print(f"FAIL {name} min_rows want>={min_rows} got={len(rows)}")
            bad = True
        roles_any = exp.get("roles_any")
        if roles_any:
            if not any(summary_rows_include_role_code(data, str(rc)) for rc in roles_any):
                print(f"FAIL {name} roles_any missing any of {roles_any}")
                bad = True
        must = exp.get("must_have_role")
        if must and not summary_rows_include_role_code(data, str(must)):
            print(f"FAIL {name} must_have_role {must!r}")
            bad = True
        if exp.get("totals_consistent") and not args.no_totals:
            terr = mis_totals_consistent(data)
            if terr:
                print(f"FAIL {name} totals: {terr}")
                bad = True
        if bad:
            failed = True
        else:
            print(f"OK {name}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
