#!/usr/bin/env python3
"""SAP workshop matrix: 3 PR types × 3 orgs × (create|modify|delete) × (simple|complex) = 54 cases.

Uses real values from ``pr_po_reference_values`` (see ``integration_reference_defaults``).
Failures are flagged **SAP_TEAM** when SAP sync or master data is the likely cause.

Does not replace ``run_sap_test.py`` — orchestrates existing form builders + API helpers.

Usage (API on :8000, SAP configured):

  python scripts/integration/run_pr_workshop_matrix.py
  python scripts/integration/run_pr_workshop_matrix.py --doc-type YUNB --org 1MGH
  python scripts/integration/run_pr_workshop_matrix.py --scenario create --dry-run
  python scripts/integration/run_pr_workshop_matrix.py --output reports/pr_workshop.json

Env: ``SAP_MATERIAL`` / ``PROCUREMENT_INTEGRATION_SAP_MATERIAL`` overrides DB material when
QAS master differs from imported catalogue.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import (
    OrgTestContext,
    WORKSHOP_ORGS,
    load_workshop_org_contexts,
)
from scripts.integration.procurement_api_common import (
    classify_sap_ticket,
    create_pr_via_api_result,
    load_env,
    patch_resync_via_api_result,
    resolve_access_token,
)
from scripts.integration.run_pr_create_yast import complex_yast_form, simple_yast_form
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form
from scripts.integration.run_pr_create_yunb import complex_yunb_form, simple_yunb_form

DocumentType = Literal["YSER", "YUNB", "YAST"]
Scenario = Literal["create", "modify", "delete"]
Shape = Literal["simple", "complex"]

FORM_BUILDERS: dict[str, dict[str, Callable[..., dict]]] = {
    "YUNB": {"simple": simple_yunb_form, "complex": complex_yunb_form},
    "YAST": {"simple": simple_yast_form, "complex": complex_yast_form},
    "YSER": {"simple": simple_yser_form, "complex": complex_yser_form},
}

PR_TYPES: tuple[str, ...] = ("YSER", "YUNB", "YAST")
SCENARIOS: tuple[str, ...] = ("create", "modify", "delete")
SHAPES: tuple[str, ...] = ("simple", "complex")


@dataclass
class CaseResult:
    case_id: str
    document_type: str
    org: str
    scenario: str
    shape: str
    status: str
    detail: str
    flag_sap_team: bool
    ticket_id: str | None = None
    sap_id: str | None = None


def _case_id(*, doc: str, org: str, scenario: str, shape: str) -> str:
    return f"{doc}|{org}|{scenario}|{shape}"


def _header_note(*, doc: str, org: str, scenario: str, shape: str, phase: str) -> str:
    return f"Workshop {doc} {org} {scenario} {shape} {phase}"


def _build_form(
    *,
    doc: str,
    org: str,
    scenario: str,
    shape: str,
    phase: str,
) -> dict[str, Any]:
    builder = FORM_BUILDERS[doc][shape]
    return builder(header_note=_header_note(doc=doc, org=org, scenario=scenario, shape=shape, phase=phase))


def _modify_form(base: dict[str, Any], *, doc: str, org: str, scenario: str, shape: str) -> dict[str, Any]:
    form = copy.deepcopy(base)
    hdr = form.setdefault("header", {})
    hdr["header_note"] = _header_note(doc=doc, org=org, scenario=scenario, shape=shape, phase="modify")
    lines = form.get("lines")
    if isinstance(lines, list) and lines and isinstance(lines[0], dict):
        row = lines[0]
        row["unit_price"] = "199"
        row["valuation_price"] = "199"
        row["delivery_date"] = "2026-09-01"
        if doc != "YSER":
            row["short_text"] = (str(row.get("short_text") or "Line") + " [modified]")[:40]
        else:
            row["short_text"] = (str(row.get("short_text") or "Service") + " [modified]")[:40]
    return form


def _delete_form(base_complex: dict[str, Any], *, doc: str, org: str, shape: str) -> dict[str, Any]:
    """Drop to first line only (SAP item 20 marked deleted on resync)."""
    form = copy.deepcopy(base_complex)
    form["header"]["header_note"] = _header_note(
        doc=doc, org=org, scenario="delete", shape=shape, phase="resync"
    )
    form["lines"] = [copy.deepcopy(base_complex["lines"][0])]
    return form


def _validate_context(ctx: OrgTestContext, doc: str) -> str | None:
    if not ctx.plant:
        return (
            f"missing plant for {ctx.purchasing_org} "
            "(set SAP_PLANT_{org} / SAP_PLANT env or import catalogue)"
        )
    if not ctx.cost_center:
        return (
            f"no cost centre in DB for entity {ctx.purchasing_org} "
            "(import HANA cost centres with matching Entity)"
        )
    if doc in ("YUNB", "YAST") and not ctx.material:
        return "missing SAP_MATERIAL (set env or import catalogue)"
    if doc == "YSER" and not ctx.service:
        return "missing SAP_SERVICE (set env or import catalogue)"
    if doc == "YSER" and ctx.service == ctx.service_2:
        return "need two distinct SAP_SERVICE codes for complex YSER"
    if doc in ("YUNB", "YAST") and ctx.material == ctx.material_2:
        return "need two distinct SAP_MATERIAL codes for complex YUNB/YAST"
    return None


def run_create(
    *,
    token: str,
    ctx: OrgTestContext,
    doc: str,
    shape: str,
    dry_run: bool,
) -> CaseResult:
    cid = _case_id(doc=doc, org=ctx.purchasing_org, scenario="create", shape=shape)
    err_ctx = _validate_context(ctx, doc)
    if err_ctx:
        return CaseResult(cid, doc, ctx.purchasing_org, "create", shape, "fail", err_ctx, False)
    form = _build_form(doc=doc, org=ctx.purchasing_org, scenario="create", shape=shape, phase="create")
    if dry_run:
        return CaseResult(cid, doc, ctx.purchasing_org, "create", shape, "pass", "dry-run", False)
    ticket, err = create_pr_via_api_result(access_token=token, document_type=doc, form=form)
    if err:
        return CaseResult(cid, doc, ctx.purchasing_org, "create", shape, "fail", err, True)
    assert ticket is not None
    status, detail, flag = classify_sap_ticket(ticket)
    return CaseResult(
        cid,
        doc,
        ctx.purchasing_org,
        "create",
        shape,
        status,
        detail,
        flag,
        str(ticket.get("id")),
        str(ticket.get("sap_id") or "") or None,
    )


def run_modify(
    *,
    token: str,
    ctx: OrgTestContext,
    doc: str,
    shape: str,
    dry_run: bool,
) -> CaseResult:
    cid = _case_id(doc=doc, org=ctx.purchasing_org, scenario="modify", shape=shape)
    err_ctx = _validate_context(ctx, doc)
    if err_ctx:
        return CaseResult(cid, doc, ctx.purchasing_org, "modify", shape, "fail", err_ctx, False)
    base = _build_form(doc=doc, org=ctx.purchasing_org, scenario="modify", shape=shape, phase="create")
    if dry_run:
        return CaseResult(cid, doc, ctx.purchasing_org, "modify", shape, "pass", "dry-run", False)
    created, err = create_pr_via_api_result(access_token=token, document_type=doc, form=base)
    if err:
        return CaseResult(cid, doc, ctx.purchasing_org, "modify", shape, "fail", f"create step: {err}", True)
    assert created is not None
    st0, d0, f0 = classify_sap_ticket(created)
    if st0 == "fail":
        return CaseResult(
            cid, doc, ctx.purchasing_org, "modify", shape, "fail", f"create step: {d0}", f0,
            str(created.get("id")), None,
        )
    modified = _modify_form(
        base, doc=doc, org=ctx.purchasing_org, scenario="modify", shape=shape
    )
    updated, err = patch_resync_via_api_result(
        access_token=token,
        ticket_id=str(created["id"]),
        form=modified,
        version=int(created["version"]),
    )
    if err:
        return CaseResult(
            cid, doc, ctx.purchasing_org, "modify", shape, "fail", f"resync: {err}", True,
            str(created.get("id")), str(created.get("sap_id") or "") or None,
        )
    assert updated is not None
    status, detail, flag = classify_sap_ticket(updated)
    return CaseResult(
        cid, doc, ctx.purchasing_org, "modify", shape, status, detail, flag,
        str(updated.get("id")), str(updated.get("sap_id") or "") or None,
    )


def run_delete(
    *,
    token: str,
    ctx: OrgTestContext,
    doc: str,
    shape: str,
    dry_run: bool,
) -> CaseResult:
    cid = _case_id(doc=doc, org=ctx.purchasing_org, scenario="delete", shape=shape)
    err_ctx = _validate_context(ctx, doc)
    if err_ctx:
        return CaseResult(cid, doc, ctx.purchasing_org, "delete", shape, "fail", err_ctx, False)
    # Always seed with 2 lines, then resync to 1 (exercises item delete / IsDeleted).
    complex_form = _build_form(
        doc=doc, org=ctx.purchasing_org, scenario="delete", shape="complex", phase="create"
    )
    if dry_run:
        return CaseResult(cid, doc, ctx.purchasing_org, "delete", shape, "pass", "dry-run", False)
    created, err = create_pr_via_api_result(access_token=token, document_type=doc, form=complex_form)
    if err:
        return CaseResult(cid, doc, ctx.purchasing_org, "delete", shape, "fail", f"create 2-line: {err}", True)
    assert created is not None
    st0, d0, f0 = classify_sap_ticket(created)
    if st0 == "fail":
        return CaseResult(
            cid, doc, ctx.purchasing_org, "delete", shape, "fail", f"create 2-line: {d0}", f0,
            str(created.get("id")), None,
        )
    one_line = _delete_form(complex_form, doc=doc, org=ctx.purchasing_org, shape=shape)
    updated, err = patch_resync_via_api_result(
        access_token=token,
        ticket_id=str(created["id"]),
        form=one_line,
        version=int(created["version"]),
    )
    if err:
        return CaseResult(
            cid, doc, ctx.purchasing_org, "delete", shape, "fail", f"resync delete: {err}", True,
            str(created.get("id")), str(created.get("sap_id") or "") or None,
        )
    assert updated is not None
    status, detail, flag = classify_sap_ticket(updated)
    return CaseResult(
        cid, doc, ctx.purchasing_org, "delete", shape, status, detail, flag,
        str(updated.get("id")), str(updated.get("sap_id") or "") or None,
    )


def iter_cases(
    *,
    doc_filter: str | None,
    org_filter: str | None,
    scenario_filter: str | None,
    shape_filter: str | None,
) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for doc in PR_TYPES:
        if doc_filter and doc.upper() != doc_filter.upper():
            continue
        for org in WORKSHOP_ORGS:
            if org_filter and org != org_filter:
                continue
            for scenario in SCENARIOS:
                if scenario_filter and scenario != scenario_filter:
                    continue
                for shape in SHAPES:
                    if shape_filter and shape != shape_filter:
                        continue
                    out.append((doc, org, scenario, shape))
    return out


def print_report(results: list[CaseResult]) -> None:
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    warned = sum(1 for r in results if r.status == "warn")
    sap_flags = [r for r in results if r.flag_sap_team and r.status != "pass"]

    print("\n" + "=" * 72)
    print("PR WORKSHOP MATRIX SUMMARY")
    print("=" * 72)
    print(f"Total: {len(results)}  pass: {passed}  fail: {failed}  warn: {warned}")
    print(f"Flag for SAP team: {len(sap_flags)}")
    if sap_flags:
        print("\n--- SAP TEAM (master data / OData / verify) ---")
        for r in sap_flags:
            print(f"  {r.case_id}: [{r.status}] {r.detail[:200]}")
    print("\n--- ALL FAILURES ---")
    for r in results:
        if r.status == "fail":
            flag = " [SAP]" if r.flag_sap_team else ""
            print(f"  {r.case_id}{flag}: {r.detail[:240]}")


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="Run 54-case SAP PR workshop matrix")
    p.add_argument("--doc-type", choices=["YSER", "YUNB", "YAST"])
    p.add_argument("--org", choices=list(WORKSHOP_ORGS))
    p.add_argument("--scenario", choices=list(SCENARIOS))
    p.add_argument("--shape", choices=list(SHAPES))
    p.add_argument("--dry-run", action="store_true", help="Validate forms only, no API")
    p.add_argument("--output", default="", help="Write JSON report path")
    p.add_argument("--token", help="Bearer token")
    p.add_argument("--email", help="Login email")
    p.add_argument("--password", help="Login password")
    args = p.parse_args()

    mat_override = (
        os.environ.get("SAP_MATERIAL", "").strip()
        or os.environ.get("PROCUREMENT_INTEGRATION_SAP_MATERIAL", "").strip()
    )
    contexts = load_workshop_org_contexts(material_override=mat_override)
    by_org = {c.purchasing_org: c for c in contexts}

    token = ""
    if not args.dry_run:
        token = resolve_access_token(
            email=args.email, password=args.password, token=args.token
        )

    results: list[CaseResult] = []
    runners = {
        "create": run_create,
        "modify": run_modify,
        "delete": run_delete,
    }

    for doc, org, scenario, shape in iter_cases(
        doc_filter=args.doc_type,
        org_filter=args.org,
        scenario_filter=args.scenario,
        shape_filter=args.shape,
    ):
        ctx = by_org[org]
        ctx.apply_to_environ()
        print(f"\n>>> {doc} | {org} | {scenario} | {shape}")
        result = runners[scenario](
            token=token, ctx=ctx, doc=doc, shape=shape, dry_run=args.dry_run
        )
        results.append(result)
        sap_tag = " [SAP]" if result.flag_sap_team and result.status != "pass" else ""
        print(f"    {result.status.upper()}{sap_tag}: {result.detail[:120]}")

    print_report(results)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "orgs": list(WORKSHOP_ORGS),
            "material_override": mat_override or None,
            "results": [asdict(r) for r in results],
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {out_path}")

    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
