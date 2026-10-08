#!/usr/bin/env python3
"""YSER PR UI-style chained mutations + legacy (no ext-ref) PR sign-off.

Simulates realistic multi-step UI edits (each step: hydrate → edit → update → verify):
  - delete CC → add different CC
  - delete service → add new service (same group + new item)
  - add service to existing group
  - legacy PRs without ``sap_pr_service_ref`` (simple + complex): hydrate, update, add, delete

Usage (from agentos-backend):
  python scripts/integration/run_yser_pr_ui_chains_signoff_live.py
  python scripts/integration/run_yser_pr_ui_chains_signoff_live.py --skip-delete
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
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form
from scripts.integration.run_yser_pr_e2e_signoff_live import (
    yser_complex_form,
    yser_grouped_two_service_form,
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


@dataclass
class ChainOut:
    chain_id: str
    pr_number: str = ""
    steps: list[Step] = field(default_factory=list)

    def ok(self) -> bool:
        ran = [s for s in self.steps if s.status != "SKIP"]
        return bool(ran) and all(s.status == "OK" for s in ran)


def _prep(form: dict[str, Any]) -> dict[str, Any]:
    out = normalize_form("YSER", form)
    apply_procurement_defaults(out, document_type="YSER", kind="PR")
    return out


def _refs(form: dict[str, Any]) -> str:
    refs = [
        str(l.get("sap_pr_service_ref") or "")
        for l in yser_effective_line_blocks(form)
        if isinstance(l, dict)
    ]
    dup = validate_yser_form_service_refs_unique(form)
    return f"refs={refs} dup={dup or 'none'}"


def _strip_ext_refs(form: dict[str, Any]) -> None:
    for line in yser_effective_line_blocks(form):
        if isinstance(line, dict):
            line.pop("sap_pr_service_ref", None)


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


async def _verify(*, pr: str, form: dict[str, Any], ticket_id: str, expect_lines: int) -> tuple[bool, str]:
    body, err = await get_yser_pr(pr_number=pr, ticket_id=f"{ticket_id}-v")
    if err or not body:
        return False, err or "GET failed"
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
    mm = verify_yser_read_matches_form(body, form=form)
    n = len(ui.get("lines") or [])
    if n != expect_lines:
        mm.insert(0, f"lines {n} != {expect_lines}")
    dup = validate_yser_form_service_refs_unique(ui)
    if dup:
        mm.insert(0, dup)
    if mm:
        return False, "; ".join(mm[:3])
    return True, f"lines={n} items={count_z_pr_sap_items(body)} {_refs(ui)}"


async def _update(pr: str, form: dict[str, Any], ticket_id: str) -> str | None:
    dup = validate_yser_form_service_refs_unique(form)
    if dup:
        return dup
    _, err = await update_pr(sap_id=pr, ticket_id=ticket_id, form=form, document_type="YSER")
    return err


async def run_legacy_no_ref_chain(*, label: str, builder, skip_delete: bool) -> ChainOut:
    """Simulate pre-ext-ref PR: hydrate, field update, add service, delete service."""
    out = ChainOut(chain_id=f"legacy_no_ref_{label}")
    tid = str(uuid.uuid4())
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    svc2 = _env("SAP_SERVICE_2")
    g2 = _env("SAP_SERVICE_GROUP_2", "SD05-0001")

    form = _prep(builder(header_note=f"leg-{label}-{tid[:8]}"))
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    hydrated, herr = await _hydrate(pr=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("hydrate", "FAIL", herr[:200]))
        return out
    _strip_ext_refs(hydrated)
    out.steps.append(Step("hydrate_strip_refs", "OK", _refs(hydrated)))

    upd = copy.deepcopy(hydrated)
    lines = upd.get("lines") or []
    if lines and isinstance(lines[0], dict):
        lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " leg-upd")[:40]
    err = await _update(pr, upd, f"{tid}-u1")
    out.steps.append(Step("field_update_no_refs", "OK" if not err else "FAIL", (err or _refs(upd))[:200]))
    if err:
        return out

    refreshed, _ = await _hydrate(pr=pr, seed=upd, ticket_id=f"{tid}-h1")
    if not refreshed:
        out.steps.append(Step("hydrate_after_update", "FAIL", "hydrate failed"))
        return out
    out.steps.append(Step("hydrate_after_update", "OK", _refs(refreshed)))

    add = copy.deepcopy(refreshed)
    add["lines"].append(
        {
            "service": svc2,
            "service_group": g2 if label == "complex" else add["lines"][0].get("service_group"),
            "short_text": "Legacy add svc",
            "delivery_date": add["lines"][0].get("delivery_date"),
            "unit_price": "75",
            "valuation_price": "75",
            "allocations": [{"cost_center": cc1, "qty": "1"}],
        }
    )
    expect_after_add = len(add["lines"])
    err = await _update(pr, add, f"{tid}-add")
    out.steps.append(Step("add_service", "OK" if not err else "FAIL", (err or "")[:200]))
    if err:
        return out
    ok, det = await _verify(pr=pr, form=add, ticket_id=tid, expect_lines=expect_after_add)
    out.steps.append(Step("verify_add", "OK" if ok else "FAIL", det))

    refreshed2, _ = await _hydrate(pr=pr, seed=add, ticket_id=f"{tid}-h2")
    if refreshed2 and len(refreshed2.get("lines") or []) >= 2:
        del_form = copy.deepcopy(refreshed2)
        del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
        err = await _update(pr, del_form, f"{tid}-del")
        out.steps.append(Step("delete_service", "OK" if not err else "FAIL", (err or "")[:200]))
        if not err:
            ok2, det2 = await _verify(pr=pr, form=del_form, ticket_id=tid, expect_lines=1)
            out.steps.append(Step("verify_delete", "OK" if ok2 else "FAIL", det2))
    else:
        out.steps.append(Step("delete_service", "SKIP", "need 2+ lines"))

    if not skip_delete:
        ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
        out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


async def run_grouped_ui_chains(*, skip_delete: bool) -> ChainOut:
    """Grouped PR: CC swap chain + service swap chain on one PR."""
    out = ChainOut(chain_id="grouped_ui_chains")
    tid = str(uuid.uuid4())
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    svc2 = _env("SAP_SERVICE_2")

    form = _prep(yser_grouped_two_service_form(header_note=f"chains-{tid[:8]}"))
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create_2svc_grouped", "OK", pr))

    hydrated, herr = await _hydrate(pr=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("hydrate", "FAIL", herr[:200]))
        return out
    out.steps.append(Step("hydrate", "OK", _refs(hydrated)))

    # Chain A: delete CC2 → add CC2 back (different qty) — single save
    cc_swap = copy.deepcopy(hydrated)
    allocs = cc_swap["lines"][0].get("allocations") or []
    if len(allocs) >= 2:
        cc_swap["lines"][0]["allocations"] = [copy.deepcopy(allocs[0])]
        cc_swap["lines"][0]["allocations"].append({"cost_center": cc2, "qty": "2"})
        for line in cc_swap["lines"]:
            if isinstance(line, dict):
                line["allocations"] = copy.deepcopy(cc_swap["lines"][0]["allocations"])
    else:
        cc_swap["lines"][0]["allocations"] = [{"cost_center": cc1, "qty": "1"}, {"cost_center": cc2, "qty": "2"}]
    err = await _update(pr, cc_swap, f"{tid}-cc")
    out.steps.append(Step("del_cc_then_add_cc", "OK" if not err else "FAIL", (err or "")[:200]))
    if err:
        return out
    ok, det = await _verify(pr=pr, form=cc_swap, ticket_id=tid, expect_lines=2)
    out.steps.append(Step("verify_cc_chain", "OK" if ok else "FAIL", det))

    after_cc, _ = await _hydrate(pr=pr, seed=cc_swap, ticket_id=f"{tid}-hcc")

    # Chain B: delete service 2 → add service 2 back (new ext ref) — single save
    if after_cc and len(after_cc.get("lines") or []) >= 2:
        svc_swap = copy.deepcopy(after_cc)
        kept = copy.deepcopy(svc_swap["lines"][0])
        svc_swap["lines"] = [kept]
        svc_swap["lines"].append(
            {
                "service": svc2,
                "short_text": "Re-added svc",
                "delivery_date": kept.get("delivery_date"),
                "unit_price": "90",
                "valuation_price": "90",
                "allocations": copy.deepcopy(kept.get("allocations") or [{"cost_center": cc1, "qty": "1"}]),
                "service_group": kept.get("service_group"),
            }
        )
        err = await _update(pr, svc_swap, f"{tid}-svc")
        out.steps.append(Step("del_svc_then_add_svc", "OK" if not err else "FAIL", (err or "")[:200]))
        if not err:
            ok2, det2 = await _verify(pr=pr, form=svc_swap, ticket_id=tid, expect_lines=2)
            out.steps.append(Step("verify_svc_chain", "OK" if ok2 else "FAIL", det2))
            after_svc, _ = await _hydrate(pr=pr, seed=svc_swap, ticket_id=f"{tid}-hsvc")
            if after_svc:
                out.steps.append(Step("hydrate_final", "OK", _refs(after_svc)))
    else:
        out.steps.append(Step("del_svc_then_add_svc", "SKIP", "need 2 hydrated lines"))

    # Chain C: add third service to existing group (sequential save)
    base, _ = await _hydrate(pr=pr, seed=cc_swap, ticket_id=f"{tid}-h3")
    if base:
        add_grp = copy.deepcopy(base)
        add_grp["lines"].append(
            {
                "service": svc2,
                "short_text": "Third grouped svc",
                "delivery_date": add_grp["lines"][0].get("delivery_date"),
                "unit_price": "55",
                "valuation_price": "55",
                "allocations": [{"cost_center": cc1, "qty": "1"}],
                "service_group": add_grp["lines"][0].get("service_group"),
            }
        )
        err = await _update(pr, add_grp, f"{tid}-add3")
        out.steps.append(Step("add_service_existing_group", "OK" if not err else "FAIL", (err or "")[:200]))
        if not err:
            ok3, det3 = await _verify(pr=pr, form=add_grp, ticket_id=tid, expect_lines=len(add_grp["lines"]))
            out.steps.append(Step("verify_add_group", "OK" if ok3 else "FAIL", det3))

    if not skip_delete:
        ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
        out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


def _print(results: list[ChainOut]) -> int:
    fails = 0
    print("\n" + "=" * 72)
    print("YSER PR UI CHAINS + LEGACY NO-REF SIGN-OFF")
    print("=" * 72)
    for r in results:
        mark = "PASS" if r.ok() else "FAIL"
        if not r.ok():
            fails += 1
        print(f"\n[{mark}] {r.chain_id} PR={r.pr_number or '-'}")
        for s in r.steps:
            print(f"  {s.status:4} {s.name:28} {s.detail[:110]}")
    print("\n" + "=" * 72)
    print(f"TOTAL: {len(results) - fails}/{len(results)} chains passed")
    return 1 if fails else 0


async def main_async(args: argparse.Namespace) -> int:
    load_env()
    print(integration_defaults_summary(apply_integration_reference_defaults()))
    if not sap_pr_configured():
        print("SAP not configured.", file=sys.stderr)
        return 2

    results = [
        await run_legacy_no_ref_chain(label="simple", builder=simple_yser_form, skip_delete=args.skip_delete),
        await run_legacy_no_ref_chain(label="complex", builder=yser_complex_form, skip_delete=args.skip_delete),
        await run_grouped_ui_chains(skip_delete=args.skip_delete),
    ]
    return _print(results)


def main() -> int:
    p = argparse.ArgumentParser(description="YSER PR UI chains + legacy sign-off")
    p.add_argument("--skip-delete", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
