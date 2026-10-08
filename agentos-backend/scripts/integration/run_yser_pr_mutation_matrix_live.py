#!/usr/bin/env python3
"""YSER PR incremental-update mutation matrix (UI hydrate → update_pr → SAP verify).

Exercises production paths:
  normalize_form → create_pr / update_pr
  load_form_from_sap (same as GET ticket hydrate)
  sap_pr_service_ref uniqueness per PR

Usage (from agentos-backend):
  python scripts/integration/run_yser_pr_mutation_matrix_live.py
  python scripts/integration/run_yser_pr_mutation_matrix_live.py --skip-delete
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_pr_client import create_pr, sap_pr_configured, update_pr
from app.procurement.sap_pr_z_client import delete_yser_pr, get_yser_pr
from app.procurement.sap_pr_z_payload import (
    count_z_pr_sap_items,
    form_from_z_pr_read,
    validate_yser_form_service_refs_unique,
    verify_yser_read_matches_form,
    yser_effective_line_blocks,
)
from app.procurement.sap_ticket_form_read import load_form_from_sap
from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import load_env
from scripts.integration.run_pr_create_yser import simple_yser_form
from scripts.integration.run_yser_pr_e2e_signoff_live import (
    yser_grouped_two_service_form,
    yser_two_groups_form,
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


@dataclass
class MatrixOut:
    matrix_id: str
    pr_number: str = ""
    steps: list[Step] = field(default_factory=list)

    def ok(self) -> bool:
        ran = [s for s in self.steps if s.status != "SKIP"]
        return bool(ran) and all(s.status == "OK" for s in ran)


def _prep(form: dict[str, Any]) -> dict[str, Any]:
    out = normalize_form("YSER", form)
    apply_procurement_defaults(out, document_type="YSER", kind="PR")
    return out


async def _hydrate(*, pr: str, seed: dict[str, Any], ticket_id: str) -> tuple[dict[str, Any] | None, str]:
    form, source, err = await load_form_from_sap(
        kind="PR",
        document_type="YSER",
        sap_id=pr,
        ticket_id=ticket_id,
        seed_form=seed,
    )
    if err or not form or source != "sap":
        return None, err or f"source={source}"
    return form, ""


def _refs_summary(form: dict[str, Any]) -> str:
    refs = [
        str(l.get("sap_pr_service_ref") or "")
        for l in yser_effective_line_blocks(form)
        if isinstance(l, dict)
    ]
    dup = validate_yser_form_service_refs_unique(form)
    return f"refs={refs} dup={dup or 'none'}"


async def _verify(
    *, pr: str, form: dict[str, Any], ticket_id: str, expect_lines: int
) -> tuple[bool, str]:
    body, err = await get_yser_pr(pr_number=pr, ticket_id=f"{ticket_id}-v")
    if err or not body:
        return False, err or "GET failed"
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
    mm = verify_yser_read_matches_form(body, form=form)
    n = len(ui.get("lines") or [])
    if n != expect_lines:
        mm.insert(0, f"lines {n} != {expect_lines}")
    ref_err = validate_yser_form_service_refs_unique(ui)
    if ref_err:
        mm.insert(0, ref_err)
    if mm:
        return False, "; ".join(mm[:3])
    return True, f"lines={n} items={count_z_pr_sap_items(body)} {_refs_summary(ui)}"


async def _update(pr: str, form: dict[str, Any], ticket_id: str) -> str | None:
    dup = validate_yser_form_service_refs_unique(form)
    if dup:
        return dup
    _, err = await update_pr(sap_id=pr, ticket_id=ticket_id, form=form, document_type="YSER")
    return err


async def run_grouped_matrix(*, skip_delete: bool) -> MatrixOut:
    """Single PR: add CC → delete CC → add service → delete service (grouped)."""
    out = MatrixOut(matrix_id="grouped_mutations")
    tid = str(uuid.uuid4())
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    svc2 = _env("SAP_SERVICE_2")

    form = _prep(simple_yser_form(header_note=f"mut-grp-{tid[:8]}"))
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    hydrated, herr = await _hydrate(pr=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("ui_hydrate", "FAIL", herr[:200]))
        return out
    out.steps.append(Step("ui_hydrate", "OK", _refs_summary(hydrated)))

    # Add CC
    upd = copy.deepcopy(hydrated)
    upd["lines"][0]["allocations"].append({"cost_center": cc2, "qty": "1"})
    err = await _update(pr, upd, f"{tid}-addcc")
    out.steps.append(Step("add_cc", "OK" if not err else "FAIL", (err or _refs_summary(upd))[:200]))
    if err:
        return out
    ok, det = await _verify(pr=pr, form=upd, ticket_id=tid, expect_lines=1)
    out.steps.append(Step("verify_add_cc", "OK" if ok else "FAIL", det))

    refreshed, _ = await _hydrate(pr=pr, seed=upd, ticket_id=f"{tid}-h1")
    if not refreshed:
        out.steps.append(Step("hydrate_after_add_cc", "FAIL", "hydrate failed"))
        return out
    out.steps.append(Step("hydrate_after_add_cc", "OK", _refs_summary(refreshed)))

    # Delete CC
    del_cc = copy.deepcopy(refreshed)
    del_cc["lines"][0]["allocations"] = [copy.deepcopy(del_cc["lines"][0]["allocations"][0])]
    err = await _update(pr, del_cc, f"{tid}-delcc")
    out.steps.append(Step("delete_cc", "OK" if not err else "FAIL", (err or "")[:200]))
    if err:
        return out
    ok, det = await _verify(pr=pr, form=del_cc, ticket_id=tid, expect_lines=1)
    out.steps.append(Step("verify_delete_cc", "OK" if ok else "FAIL", det))

    refreshed2, _ = await _hydrate(pr=pr, seed=del_cc, ticket_id=f"{tid}-h2")
    if not refreshed2:
        out.steps.append(Step("hydrate_after_del_cc", "FAIL", "hydrate failed"))
        return out
    out.steps.append(Step("hydrate_after_del_cc", "OK", _refs_summary(refreshed2)))

    # Add service on existing item
    add_svc = copy.deepcopy(refreshed2)
    add_svc["lines"].append(
        {
            "service": svc2,
            "short_text": "Second svc",
            "delivery_date": add_svc["lines"][0].get("delivery_date"),
            "unit_price": "50",
            "valuation_price": "50",
            "allocations": [{"cost_center": cc1, "qty": "1"}],
            "service_group": add_svc["lines"][0].get("service_group"),
        }
    )
    err = await _update(pr, add_svc, f"{tid}-addsvc")
    out.steps.append(Step("add_service_existing_item", "OK" if not err else "FAIL", (err or "")[:200]))
    if err:
        return out
    ok, det = await _verify(pr=pr, form=add_svc, ticket_id=tid, expect_lines=2)
    out.steps.append(Step("verify_add_service", "OK" if ok else "FAIL", det))

    refreshed3, _ = await _hydrate(pr=pr, seed=add_svc, ticket_id=f"{tid}-h3")
    if not refreshed3:
        out.steps.append(Step("hydrate_after_add_svc", "FAIL", "hydrate failed"))
        return out
    out.steps.append(Step("hydrate_after_add_svc", "OK", _refs_summary(refreshed3)))

    # Header note only
    note_form = copy.deepcopy(refreshed3)
    note_form.setdefault("header", {})["header_note"] = f"note-{tid[:8]}"
    err = await _update(pr, note_form, f"{tid}-note")
    out.steps.append(Step("update_header_note", "OK" if not err else "FAIL", (err or "")[:200]))

    # Delete one service (grouped) — keep first line only
    del_svc = copy.deepcopy(refreshed3)
    del_svc["lines"] = [copy.deepcopy(del_svc["lines"][0])]
    err = await _update(pr, del_svc, f"{tid}-delsvc")
    out.steps.append(Step("delete_service_grouped", "OK" if not err else "FAIL", (err or "")[:200]))
    if not err:
        ok, det = await _verify(pr=pr, form=del_svc, ticket_id=tid, expect_lines=1)
        out.steps.append(Step("verify_delete_service", "OK" if ok else "FAIL", det))

    if not skip_delete:
        ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
        out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


async def run_legacy_matrix(*, skip_delete: bool) -> MatrixOut:
    """Legacy two-item PR: delete line + add service on new item."""
    out = MatrixOut(matrix_id="legacy_mutations")
    tid = str(uuid.uuid4())
    cc = _env("SAP_COST_CENTER")

    form = _prep(yser_two_groups_form(header_note=f"mut-leg-{tid[:8]}"))
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    hydrated, herr = await _hydrate(pr=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("ui_hydrate", "FAIL", herr[:200]))
        return out
    out.steps.append(Step("ui_hydrate", "OK", _refs_summary(hydrated)))

    # Delete second line (legacy item delete)
    del_form = copy.deepcopy(hydrated)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    err = await _update(pr, del_form, f"{tid}-legdel")
    out.steps.append(Step("delete_legacy_line", "OK" if not err else "FAIL", (err or "")[:200]))
    if not err:
        ok, det = await _verify(pr=pr, form=del_form, ticket_id=tid, expect_lines=1)
        out.steps.append(Step("verify_delete_line", "OK" if ok else "FAIL", det))

    refreshed, _ = await _hydrate(pr=pr, seed=del_form, ticket_id=f"{tid}-h")
    if not refreshed:
        return out
    out.steps.append(Step("hydrate_after_del", "OK", _refs_summary(refreshed)))

    # Add service on new item (new service_group → new PRItem)
    g2 = _env("SAP_SERVICE_GROUP_2", "SD05-0001")
    svc2 = _env("SAP_SERVICE_2")
    add_form = copy.deepcopy(refreshed)
    add_form["lines"].append(
        {
            "service": svc2,
            "service_group": g2,
            "short_text": "New item svc",
            "delivery_date": add_form["lines"][0].get("delivery_date"),
            "unit_price": "80",
            "valuation_price": "80",
            "allocations": [{"cost_center": cc, "qty": "1"}],
        }
    )
    err = await _update(pr, add_form, f"{tid}-legadd")
    out.steps.append(Step("add_service_new_item", "OK" if not err else "FAIL", (err or "")[:200]))
    if not err:
        ok, det = await _verify(pr=pr, form=add_form, ticket_id=tid, expect_lines=2)
        out.steps.append(Step("verify_add_new_item", "OK" if ok else "FAIL", det))

    if not skip_delete:
        ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
        out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


async def run_grouped_create_matrix(*, skip_delete: bool) -> MatrixOut:
    """Create with 2 services grouped; hydrate refs on both lines."""
    out = MatrixOut(matrix_id="grouped_create_hydrate")
    tid = str(uuid.uuid4())
    form = _prep(yser_grouped_two_service_form(header_note=f"mut-2svc-{tid[:8]}"))
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    hydrated, herr = await _hydrate(pr=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("ui_hydrate", "FAIL", herr[:200]))
        return out
    refs = [
        l.get("sap_pr_service_ref")
        for l in yser_effective_line_blocks(hydrated)
        if isinstance(l, dict)
    ]
    dup = validate_yser_form_service_refs_unique(hydrated)
    ok = len(hydrated.get("lines") or []) == 2 and not dup and len(set(refs)) == len(refs)
    out.steps.append(
        Step(
            "ui_hydrate_refs",
            "OK" if ok else "FAIL",
            f"lines=2 refs={refs} dup={dup}",
        )
    )

    if not skip_delete:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
    return out


def _print(results: list[MatrixOut]) -> int:
    fails = 0
    print("\n" + "=" * 72)
    print("YSER PR MUTATION MATRIX (hydrate → update → verify)")
    print("=" * 72)
    for r in results:
        mark = "PASS" if r.ok() else "FAIL"
        if not r.ok():
            fails += 1
        print(f"\n[{mark}] {r.matrix_id} PR={r.pr_number or '-'}")
        for s in r.steps:
            print(f"  {s.status:4} {s.name:28} {s.detail[:110]}")
    print("\n" + "=" * 72)
    print(f"TOTAL: {len(results) - fails}/{len(results)} matrices passed")
    return 1 if fails else 0


async def main_async(args: argparse.Namespace) -> int:
    load_env()
    print(integration_defaults_summary(apply_integration_reference_defaults()))
    if not sap_pr_configured():
        print("SAP not configured.", file=sys.stderr)
        return 2

    results = [
        await run_grouped_create_matrix(skip_delete=args.skip_delete),
        await run_grouped_matrix(skip_delete=args.skip_delete),
        await run_legacy_matrix(skip_delete=args.skip_delete),
    ]
    return _print(results)


def main() -> int:
    p = argparse.ArgumentParser(description="YSER PR mutation matrix live")
    p.add_argument("--skip-delete", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
