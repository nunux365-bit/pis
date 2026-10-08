#!/usr/bin/env python3
"""
**Deprecated** — use the three-tier master data pipeline instead:

1. ``python scripts/sync_pr_po_reference_from_sap.py --mode full``  (SAP catalogue)
2. ``python scripts/load_pr_po_reference_fixed_master.py``  (JSON + SQL in repo)
3. ``python scripts/validate_pr_po_reference_master.py``

This script remains for one-off Excel recovery via ``--legacy-excel`` (repository workbook only).
Material / service / vendor / plant / tax / sloc overlap SAP sync and are skipped by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import OrderedDict
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from openpyxl import load_workbook  # noqa: E402

DEFAULT_DOWNLOADS = Path.home() / "Downloads"


def _norm_scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        s = str(v).strip()
        if s.endswith(".0"):
            return s[:-2]
        return s
    return str(v).strip()


def _truncate_label(s: str, max_len: int = 500) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


REF_CODE_MAX_LEN = 256


def _truncate_code(code: str, *, context: str) -> str:
    """DB column ``code`` is VARCHAR(256); guard against bad sheet cells."""
    c = (code or "").strip()
    if len(c) <= REF_CODE_MAX_LEN:
        return c
    short = c[: REF_CODE_MAX_LEN - 1] + "…"
    print(f"warning: truncated code ({context}): len={len(c)} head={c[:80]!r}", file=sys.stderr)
    return short


def _codes_from_cell(raw: str) -> list[str]:
    """Split a cell that mistakenly contains multiple codes (one per line)."""
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in s.split("\n")]
    return [p for p in parts if p]


def _dedupe_ref_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per DB unique key. Later rows override earlier (e.g. HANA over shallow repo ``cost_center``)."""
    by_key: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()
    for r in rows:
        k = (r["domain"], r["document_type"], r["code"], r.get("applies_to_kind") or "")
        by_key[k] = r
    return list(by_key.values())


async def _upsert_batch(rows: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, int]:
    from sqlalchemy import select

    from app.db.models import PrPoReferenceValue
    from app.db.session import AsyncSessionLocal

    ins, upd = 0, 0
    async with AsyncSessionLocal() as session:
        for row in rows:
            atk = row.get("applies_to_kind") or ""
            q = select(PrPoReferenceValue).where(
                PrPoReferenceValue.domain == row["domain"],
                PrPoReferenceValue.document_type == row["document_type"],
                PrPoReferenceValue.code == row["code"],
                PrPoReferenceValue.applies_to_kind == atk,
            )
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing is None:
                if not dry_run:
                    session.add(PrPoReferenceValue(**row))
                ins += 1
            else:
                if not dry_run:
                    existing.label = row["label"]
                    existing.sort_order = row["sort_order"]
                    existing.extra = row.get("extra")
                    existing.applies_to_kind = atk
                upd += 1
        if not dry_run:
            await session.commit()
    return ins, upd


def _read_two_col_sheet(path: Path, sheet: str, *, domain: str, sort_base: int = 0) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows_out: list[dict[str, Any]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            a = row[0] if len(row) > 0 else None
            b = row[1] if len(row) > 1 else None
            code_cell = _norm_scalar(a)
            label = _norm_scalar(b) if b is not None else ""
            if not code_cell:
                continue
            codes = _codes_from_cell(code_cell) if "\n" in code_cell else [code_cell]
            for code in codes:
                code = _truncate_code(code, context=f"{domain}/{sheet}")
                rows_out.append(
                    {
                        "domain": domain,
                        "document_type": "",
                        "applies_to_kind": "",
                        "code": code,
                        "label": _truncate_label(label or code),
                        "sort_order": sort_base + len(rows_out),
                        "extra": None,
                    }
                )
        return rows_out
    finally:
        wb.close()


def _read_material_group_sheet(path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``material_group`` upsert rows + code → label map for resolving service group names."""
    rows = _read_two_col_sheet(path, "Material Group", domain="material_group")
    by_code: dict[str, str] = {}
    for r in rows:
        by_code[str(r["code"]).strip()] = str(r.get("label") or "").strip() or str(r["code"]).strip()
    return rows, by_code


def _append_service_group_rows_from_service_master(
    all_rows: list[dict[str, Any]], *, material_group_labels: dict[str, str]
) -> int:
    """
    One ``service_group`` reference row per distinct ``Material Group`` value on imported ``service`` rows.
    Label comes from ``material_group_labels`` (PR PO **Material Group** tab); otherwise the code.
    """
    by_trunc: dict[str, str] = {}
    for r in all_rows:
        if r.get("domain") != "service":
            continue
        ex = r.get("extra")
        if not isinstance(ex, dict):
            continue
        raw = (ex.get("Material Group") or "").strip()
        if not raw:
            continue
        c = _truncate_code(raw, context="service_group/from service")
        if c not in by_trunc:
            by_trunc[c] = raw
    if not by_trunc:
        return 0
    n = 0
    for c in sorted(by_trunc.keys()):
        raw = by_trunc[c]
        name = (material_group_labels.get(raw) or material_group_labels.get(c) or "").strip() or c
        all_rows.append(
            {
                "domain": "service_group",
                "document_type": "",
                "applies_to_kind": "",
                "code": c,
                "label": _truncate_label(name),
                "sort_order": n,
                "extra": None,
            }
        )
        n += 1
    return n


def _col_index(headers: list[str], *needles: str) -> int | None:
    """First column index whose header matches one of ``needles`` (exact, then substring)."""
    heads = [(_norm_scalar(h) or "").strip() for h in headers]
    lows = [h.lower() for h in heads]
    for nd in needles:
        n = nd.lower().strip()
        for i, h in enumerate(lows):
            if h == n:
                return i
    for nd in needles:
        n = nd.lower().strip()
        for i, h in enumerate(lows):
            if n and n in h:
                return i
    return None


def _read_hana_provision_cost_centers(path: Path) -> list[dict[str, Any]]:
    """
    Read **Provision Sheet SAP HANA.xlsx** (or same layout) *Cost Center* tab.

    - First row = headers; one column must look like **Cost Center** (the SAP code).
    - All columns are copied into ``extra`` under their sheet header names.
    - Canonical keys ``Entity``, ``Profit Center``, ``Department`` are set when matching columns exist
      (so facet search + dropdowns work).
    """
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = None
        for sn in wb.sheetnames:
            sl = sn.strip().lower()
            if sl == "cost center" or (sl.replace("_", " ") == "cost center"):
                ws = wb[sn]
                break
        if ws is None:
            for sn in wb.sheetnames:
                sl = sn.strip().lower()
                if "cost" in sl and "center" in sl:
                    ws = wb[sn]
                    break
        if ws is None:
            print(f"warning: HANA workbook has no Cost Center sheet: {path}", file=sys.stderr)
            return []

        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            return []
        headers = [_norm_scalar(c) for c in header_row]
        cci = _col_index(
            headers,
            "Cost Center",
            "Cost Centre",
            "Cost Ctr",
            "CostCtr",
            "CostCenter",
            "Cst Ctr",
            "CCtr",
        )
        if cci is None:
            print(f"warning: HANA sheet {path!s}: no Cost Center column in header row", file=sys.stderr)
            return []

        ei = _col_index(headers, "Entity", "Company", "Controlling Area", "Company Code", "CoCd")
        pci = _col_index(headers, "Profit Center", "Profit centre", "Profit Ctr", "PC", "Profit Ctr.")
        di = _col_index(headers, "Department", "Dept", "Functional Area", "Bus Area")
        ni = _col_index(headers, "Description", "Name", "Short Text", "Cost Center Name", "Cost centre text")

        out: list[dict[str, Any]] = []
        for row in rows_iter:
            if not row or len(row) <= cci:
                continue
            code = _truncate_code(_norm_scalar(row[cci]), context="cost_center/hana")
            if not code:
                continue
            extra: dict[str, Any] = {}
            for j, h in enumerate(headers):
                if not h or j >= len(row):
                    continue
                extra[h] = _norm_scalar(row[j])
            if ei is not None and ei < len(row):
                extra["Entity"] = _norm_scalar(row[ei])
            if pci is not None and pci < len(row):
                extra["Profit Center"] = _norm_scalar(row[pci])
            if di is not None and di < len(row):
                extra["Department"] = _norm_scalar(row[di])
            label = ""
            if ni is not None and ni < len(row):
                label = _norm_scalar(row[ni])
            if not label:
                label = _truncate_label(extra.get(headers[cci], "") or code)
            out.append(
                {
                    "domain": "cost_center",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": code,
                    "label": _truncate_label(label or code),
                    "sort_order": len(out),
                    "extra": extra,
                }
            )
        return out
    finally:
        wb.close()


def _read_doc_type_pairs(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Doc. Type"]
        pairs: list[tuple[str, str]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            code = _norm_scalar(row[0] if len(row) > 0 else None)
            label = _norm_scalar(row[1] if len(row) > 1 else None)
            if not code:
                continue
            pairs.append((code, label or code))
        out: list[dict[str, Any]] = []
        i = 0
        sort = 0
        while i < len(pairs):
            c0, l0 = pairs[i]
            if i + 1 < len(pairs) and pairs[i + 1][0] == c0:
                out.append(
                    {
                        "domain": "purchasing_doc_type",
                        "document_type": "",
                        "applies_to_kind": "PR",
                        "code": c0,
                        "label": _truncate_label(l0),
                        "sort_order": sort,
                        "extra": None,
                    }
                )
                sort += 1
                c1, l1 = pairs[i + 1]
                out.append(
                    {
                        "domain": "purchasing_doc_type",
                        "document_type": "",
                        "applies_to_kind": "PO",
                        "code": c1,
                        "label": _truncate_label(l1),
                        "sort_order": sort,
                        "extra": None,
                    }
                )
                sort += 1
                i += 2
            else:
                out.append(
                    {
                        "domain": "purchasing_doc_type",
                        "document_type": "",
                        "applies_to_kind": "",
                        "code": c0,
                        "label": _truncate_label(l0),
                        "sort_order": sort,
                        "extra": None,
                    }
                )
                sort += 1
                i += 1
        return out
    finally:
        wb.close()


def _read_storage_locations(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Storage Location"]
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            code = _norm_scalar(row[0] if len(row) > 0 else None)
            label = _norm_scalar(row[1] if len(row) > 1 else None)
            if not code:
                continue
            key = (code, label or code)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "domain": "storage_location",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": code,
                    "label": _truncate_label(label or code),
                    "sort_order": len(out),
                    "extra": None,
                }
            )
        return out
    finally:
        wb.close()


def _read_item_categories(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Item Category"]
        out: list[dict[str, Any]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            code = _norm_scalar(row[0] if len(row) > 0 else None)
            label = _norm_scalar(row[1] if len(row) > 1 else None)
            if not label and not code:
                continue
            out.append(
                {
                    "domain": "item_category",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": code,
                    "label": _truncate_label(label or code or "Standard"),
                    "sort_order": len(out),
                    "extra": None,
                }
            )
        return out
    finally:
        wb.close()


def _read_tax_codes(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Tax Code"]
        out: list[dict[str, Any]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            code = _norm_scalar(row[0] if len(row) > 0 else None)
            label = _norm_scalar(row[1] if len(row) > 1 else None)
            if not code or "and so on" in code.lower():
                continue
            out.append(
                {
                    "domain": "tax_code",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": code,
                    "label": _truncate_label(label or code),
                    "sort_order": len(out),
                    "extra": None,
                }
            )
        return out
    finally:
        wb.close()


def _read_material_master(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"]
        headers = [str(c or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        idx = {h: i for i, h in enumerate(headers)}
        need = ["Material", "Material description", "Material Group", "Base Unit of Measure"]
        for k in need:
            if k not in idx:
                raise ValueError(f"Material Master: missing column {k!r}")
        out: list[dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            mat = _norm_scalar(row[idx["Material"]] if idx["Material"] < len(row) else None)
            if not mat:
                continue
            desc = _norm_scalar(row[idx["Material description"]] if idx["Material description"] < len(row) else "")
            mg = _norm_scalar(row[idx["Material Group"]] if idx["Material Group"] < len(row) else "")
            bu = _norm_scalar(row[idx["Base Unit of Measure"]] if idx["Base Unit of Measure"] < len(row) else "")
            extra: dict[str, str] = {"material_group": mg, "base_unit": bu}
            for opt_k in ("Material type", "Industry Sector", "Order Unit", "Weight unit", "Old material number"):
                if opt_k in idx:
                    extra[opt_k.lower().replace(" ", "_")] = _norm_scalar(row[idx[opt_k]] if idx[opt_k] < len(row) else "")
            out.append(
                {
                    "domain": "material",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": mat,
                    "label": _truncate_label(desc or mat),
                    "sort_order": len(out),
                    "extra": extra,
                }
            )
        return out
    finally:
        wb.close()


def _read_service_master(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"]
        headers = [str(c or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        idx = {h: i for i, h in enumerate(headers)}
        for k in ("Activity number", "Service Short Text"):
            if k not in idx:
                raise ValueError(f"Service Master: missing column {k!r}")
        out: list[dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            act = _norm_scalar(row[idx["Activity number"]] if idx["Activity number"] < len(row) else None)
            if not act:
                continue
            st = _norm_scalar(row[idx["Service Short Text"]] if idx["Service Short Text"] < len(row) else "")
            extra = {h: _norm_scalar(row[idx[h]]) for h in headers if h in idx and h != "Activity number"}
            # Mirror ``material.extra.material_group`` — stable key for search/filter (Excel uses "Material Group").
            mg = _norm_scalar(row[idx["Material Group"]] if idx.get("Material Group") is not None and idx["Material Group"] < len(row) else "")
            if mg:
                extra["service_group"] = mg
            for uom_col in ("Base Unit of Measure", "Order Unit", "Unit", "Base unit"):
                if uom_col in idx:
                    bu = _norm_scalar(row[idx[uom_col]] if idx[uom_col] < len(row) else "")
                    if bu:
                        extra["base_unit"] = bu
                    break
            out.append(
                {
                    "domain": "service",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": act,
                    "label": _truncate_label(st or act),
                    "sort_order": len(out),
                    "extra": extra or None,
                }
            )
        return out
    finally:
        wb.close()


def _read_vendor_master(path: Path) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"]
        headers = [str(c or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        # Resolve duplicate "PayT" by first occurrence index only.
        seen_h: set[str] = set()
        idx: dict[str, int] = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            if h in seen_h:
                continue
            seen_h.add(h)
            idx[h] = i
        required = ["Vendor", "Name 1", "CoCd"]
        for k in required:
            if k not in idx:
                raise ValueError(f"Vendor Master: missing column {k!r}")
        pay_i = idx.get("PayT")
        out: list[dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            def cell(name: str) -> str:
                j = idx.get(name)
                if j is None or j >= len(row):
                    return ""
                return _norm_scalar(row[j])

            vendor = cell("Vendor")
            cocd = cell("CoCd")
            if not vendor or not cocd:
                continue
            name1 = cell("Name 1")
            city = cell("City")
            label = name1
            if city:
                label = f"{name1} — {city}"
            extra_keys = [
                "Name 1",
                "Name 2",
                "City",
                "PostalCode",
                "Street",
                "SearchTerm",
                "Permanent account number",
                "Tax Number 3",
                "CoCd",
                "Type",
                "Rg",
            ]
            extra: dict[str, Any] = {k.lower().replace(" ", "_"): cell(k) for k in extra_keys}
            extra["vendor"] = vendor
            extra["cocd"] = cocd
            if pay_i is not None and pay_i < len(row):
                extra["payt"] = _norm_scalar(row[pay_i])
            composite = f"{vendor}|{cocd}"
            out.append(
                {
                    "domain": "vendor",
                    "document_type": "",
                    "applies_to_kind": "",
                    "code": composite,
                    "label": _truncate_label(label),
                    "sort_order": len(out),
                    "extra": extra,
                }
            )
        return out
    finally:
        wb.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Import procurement reference values from Excel masters.")
    p.add_argument(
        "--legacy-excel",
        action="store_true",
        help="Run legacy Excel import (repository-only by default; not recommended for prod).",
    )
    p.add_argument("--repository", type=Path, default=DEFAULT_DOWNLOADS / "PR PO Fields Repository.xlsx")
    p.add_argument("--material", type=Path, default=DEFAULT_DOWNLOADS / "Material Master.xlsx")
    p.add_argument("--service", type=Path, default=DEFAULT_DOWNLOADS / "Service Master.xlsx")
    p.add_argument("--vendor", type=Path, default=DEFAULT_DOWNLOADS / "Vendor Master - Non Pharma.xlsx")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-vendor", action="store_true", help="Also load Vendor Master.xlsx (SAP sync preferred).")
    p.add_argument("--include-material", action="store_true", help="Also load Material Master.xlsx (SAP sync preferred).")
    p.add_argument("--include-service", action="store_true", help="Also load Service Master.xlsx (SAP sync preferred).")
    p.add_argument(
        "--hana-provision",
        type=Path,
        nargs="?",
        const=DEFAULT_DOWNLOADS / "Provision Sheet SAP HANA.xlsx",
        default=None,
        help="Optional HANA cost-center enrich (SAP CC sync is preferred).",
    )
    args = p.parse_args()

    if not args.legacy_excel:
        print(
            "Use the standard pipeline:\n"
            "  1. python scripts/sync_pr_po_reference_from_sap.py --mode full\n"
            "  2. python scripts/load_pr_po_reference_fixed_master.py\n"
            "  3. python scripts/validate_pr_po_reference_master.py\n"
            "\nPass --legacy-excel to run this Excel importer (repository tabs only by default).",
            file=sys.stderr,
        )
        return 2

    all_rows: list[dict[str, Any]] = []

    def add_from(path: Path, fn, label: str) -> None:
        nonlocal all_rows
        if not path.is_file():
            print(f"warning: skip {label}: not found: {path}", file=sys.stderr)
            return
        chunk = fn(path)
        print(f"{label}: {len(chunk)} row(s) from {path}")
        all_rows.extend(chunk)

    repo = args.repository.expanduser()
    material_group_labels: dict[str, str] = {}
    add_from(repo, lambda p: _read_two_col_sheet(p, "Company Code", domain="company_code"), "company_code")
    add_from(repo, lambda p: _read_two_col_sheet(p, "Purch. Organization", domain="purchasing_org"), "purchasing_org")
    add_from(repo, lambda p: _read_two_col_sheet(p, "Currency", domain="currency"), "currency")
    add_from(repo, lambda p: _read_two_col_sheet(p, "Payment terms", domain="payment_terms"), "payment_terms")
    add_from(repo, lambda p: _read_two_col_sheet(p, "PO Unit", domain="order_unit"), "order_unit")
    add_from(repo, lambda p: _read_two_col_sheet(p, "Account Assignment Category", domain="account_assignment_category"), "account_assignment_category")
    if args.hana_provision is not None:
        hp = args.hana_provision.expanduser()
        add_from(hp, _read_hana_provision_cost_centers, "hana_cost_center")
    add_from(repo, _read_doc_type_pairs, "purchasing_doc_type")
    add_from(repo, _read_item_categories, "item_category")

    if args.include_material:
        mat = args.material.expanduser()
        add_from(mat, _read_material_master, "material")
    if args.include_service:
        svc = args.service.expanduser()
        add_from(svc, _read_service_master, "service")
        if repo.is_file():
            _, material_group_labels = _read_material_group_sheet(repo)
        n_sg = _append_service_group_rows_from_service_master(
            all_rows, material_group_labels=material_group_labels
        )
        if n_sg:
            print(f"service_group: {n_sg} row(s) from Service Master (legacy)")
    if args.include_vendor:
        ven = args.vendor.expanduser()
        add_from(ven, _read_vendor_master, "vendor")

    if not all_rows:
        print("error: no rows to import", file=sys.stderr)
        return 2

    before = len(all_rows)
    all_rows = _dedupe_ref_rows(all_rows)
    if len(all_rows) < before:
        print(f"note: de-duplicated {before - len(all_rows)} row(s) with same domain/code/kind", file=sys.stderr)

    ins, upd = asyncio.run(_upsert_batch(all_rows, dry_run=args.dry_run))
    if args.dry_run:
        print(f"dry-run: would insert {ins}, update {upd} ({len(all_rows)} row(s) total)")
    else:
        print(f"committed: inserted {ins}, updated {upd} ({len(all_rows)} row(s) total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
