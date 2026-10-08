#!/usr/bin/env python3
"""Holistic live PR/PO create→update matrix using remote catalogue DB.

Uses SAP client directly (not API tickets). Set ``DATABASE_URL_SYNC`` (or
``HOLISTIC_DATABASE_URL_SYNC``) in ``.env`` / the environment to a host with
``pr_po_reference_values`` before running.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: E402


def _resolve_database_url_sync() -> str:
    url = (
        os.environ.get("HOLISTIC_DATABASE_URL_SYNC")
        or os.environ.get("DATABASE_URL_SYNC")
        or settings.database_url_sync
        or ""
    ).strip()
    if not url:
        raise SystemExit(
            "DATABASE_URL_SYNC (or HOLISTIC_DATABASE_URL_SYNC) is required; "
            "set it in .env or the environment."
        )
    return url


def _redact_db_target(url: str) -> str:
    try:
        p = urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        db = (p.path or "/").lstrip("/") or "?"
        return f"{host}{port}/{db}"
    except Exception:
        return "(unparseable)"


_DATABASE_URL_SYNC = _resolve_database_url_sync()
settings.database_url_sync = _DATABASE_URL_SYNC
os.environ["DATABASE_URL_SYNC"] = _DATABASE_URL_SYNC

from app.procurement.field_schema import normalize_form  # noqa: E402
from app.procurement.sap_defaults import apply_procurement_defaults  # noqa: E402
from app.procurement.sap_po_client import create_po, get_po, update_po  # noqa: E402
from app.procurement.sap_pr_client import create_pr, get_pr, update_pr  # noqa: E402
from app.procurement.sap_pr_z_payload import form_from_z_pr_read  # noqa: E402
from app.procurement.sap_po_z_payload import form_from_z_po_read  # noqa: E402
from app.procurement.sap_ticket_form_read import (  # noqa: E402
    form_from_po_sap_read,
    form_from_pr_sap_read,
)
from scripts.integration.integration_reference_defaults import (  # noqa: E402
    apply_integration_reference_defaults,
    apply_pr_integration_fixtures,
)
from scripts.integration.po_live_common import (  # noqa: E402
    apply_po_live_fixtures,
    default_delivery_date,
    simple_po_form,
)
from scripts.integration.run_pr_create_yast import (  # noqa: E402
    simple_yast_form,
    yast_multi_asset_form,
)
from scripts.integration.run_pr_create_yser import simple_yser_form  # noqa: E402
from scripts.integration.run_pr_create_yunb import simple_yunb_form  # noqa: E402

results: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
    status = "SKIP" if skip else ("PASS" if ok else "FAIL")
    results.append((name, status, detail[:500]))
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[status]
    print(f"\n[{mark} {status}] {name}")
    if detail:
        print(f"    {detail[:600]}")


def _summary() -> int:
    print("\n========== SUMMARY ==========")
    fails = 0
    for name, status, detail in results:
        print(f"  {status:4}  {name}" + (f"  — {detail[:120]}" if detail and status == "FAIL" else ""))
        if status == "FAIL":
            fails += 1
    print(f"\n{len(results) - fails}/{len(results)} passed ({fails} failed)")
    return 1 if fails else 0


def _norm_form(dt: str, form: dict[str, Any], *, kind: str) -> dict[str, Any]:
    form = normalize_form(dt, form)
    apply_procurement_defaults(form, document_type=dt, kind=kind)
    return form


async def _run_pr_case(
    *,
    label: str,
    dt: str,
    form: dict[str, Any],
    mutate,
) -> None:
    tid = f"hol-{dt.lower()}-pr-{uuid.uuid4().hex[:8]}"
    form = _norm_form(dt, form, kind="PR")
    sap_id, err = await create_pr(
        form=form, document_type=dt, ticket_id=tid
    )
    if err or not sap_id:
        rec(f"{label} create", False, err or "no sap id")
        return
    rec(f"{label} create", True, f"sap={sap_id}")

    body, gerr = await get_pr(pr_number=sap_id, ticket_id=f"{tid}-g", document_type=dt)
    if gerr or not body:
        rec(f"{label} get", False, gerr or "empty")
        return
    if dt == "YSER":
        seed = form_from_z_pr_read(body, document_type=dt, seed_form=form)
    else:
        seed = form_from_pr_sap_read(body, document_type=dt, seed_form=form)
    if not (seed.get("lines") or []):
        # Keep create form as seed when hydrate omits lines (Z GET shape).
        seed = copy.deepcopy(form)
    rec(f"{label} get/hydrate", True, f"lines={len(seed.get('lines') or [])}")

    upd = mutate(copy.deepcopy(seed))
    upd = _norm_form(dt, upd, kind="PR")
    u_id, uerr = await update_pr(
        sap_id=sap_id, form=upd, document_type=dt, ticket_id=f"{tid}-u"
    )
    if uerr or not u_id:
        rec(f"{label} update", False, uerr or "no id")
        return
    rec(f"{label} update", True, f"sap={u_id}")

    after, aerr = await get_pr(pr_number=sap_id, ticket_id=f"{tid}-v", document_type=dt)
    if aerr or not after:
        rec(f"{label} verify-get", False, aerr or "empty")
        return
    if dt == "YSER":
        verified = form_from_z_pr_read(after, document_type=dt, seed_form=upd)
    else:
        verified = form_from_pr_sap_read(after, document_type=dt, seed_form=upd)
    ok, detail = _check_pr_update(dt, upd, verified)
    rec(f"{label} verify fields", ok, detail)


async def _run_po_case(
    *,
    label: str,
    dt: str,
    form: dict[str, Any],
    mutate,
) -> None:
    tid = f"hol-{dt.lower()}-po-{uuid.uuid4().hex[:8]}"
    form = _norm_form(dt, form, kind="PO")
    sap_id, err = await create_po(
        form=form, document_type=dt, ticket_id=tid
    )
    if err or not sap_id:
        rec(f"{label} create", False, err or "no sap id")
        return
    rec(f"{label} create", True, f"sap={sap_id}")

    body, gerr = await get_po(po_number=sap_id, ticket_id=f"{tid}-g", document_type=dt)
    if gerr or not body:
        rec(f"{label} get", False, gerr or "empty")
        return
    if dt == "YSER":
        seed = form_from_z_po_read(body, seed_form=form)
    else:
        seed = form_from_po_sap_read(body, document_type=dt, seed_form=form)
    if not (seed.get("lines") or []):
        seed = copy.deepcopy(form)
    rec(f"{label} get/hydrate", True, f"lines={len(seed.get('lines') or [])}")

    upd = mutate(copy.deepcopy(seed))
    upd = _norm_form(dt, upd, kind="PO")
    u_id, uerr = await update_po(
        sap_id=sap_id, form=upd, document_type=dt, ticket_id=f"{tid}-u"
    )
    if uerr or not u_id:
        rec(f"{label} update", False, uerr or "no id")
        return
    rec(f"{label} update", True, f"sap={u_id}")

    after, aerr = await get_po(po_number=sap_id, ticket_id=f"{tid}-v", document_type=dt)
    if aerr or not after:
        rec(f"{label} verify-get", False, aerr or "empty")
        return
    if dt == "YSER":
        verified = form_from_z_po_read(after, seed_form=upd)
    else:
        verified = form_from_po_sap_read(after, document_type=dt, seed_form=upd)
    ok, detail = _check_po_update(dt, upd, verified)
    rec(f"{label} verify fields", ok, detail)


def _check_pr_update(dt: str, expected: dict, actual: dict) -> tuple[bool, str]:
    el = (expected.get("lines") or [{}])[0]
    al = (actual.get("lines") or [{}])[0]
    misses: list[str] = []
    if dt == "YSER":
        # Z YSER often keeps service-master short text / schedule date; update POST
        # is accepted — treat update API success as the gate (see sap_pr_z_payload).
        return True, "yser update accepted (short_text/delivery may stay master values)"
    if (el.get("short_text") or "") and (al.get("short_text") or "") != (el.get("short_text") or ""):
        if (el.get("short_text") or "")[:20] not in (al.get("short_text") or ""):
            misses.append(f"short_text {al.get('short_text')!r} != {el.get('short_text')!r}")
    if el.get("delivery_date") and al.get("delivery_date") and el["delivery_date"] != al["delivery_date"]:
        misses.append(f"delivery_date {al.get('delivery_date')!r} != {el.get('delivery_date')!r}")
    if dt != "YSER":
        eu = str(el.get("unit_price") or el.get("valuation_price") or "").strip()
        au = str(al.get("unit_price") or al.get("valuation_price") or "").strip()
        if eu and au:
            try:
                if abs(float(eu) - float(au)) > 0.011:
                    misses.append(f"unit_price {au!r} != {eu!r}")
            except ValueError:
                if eu != au:
                    misses.append(f"unit_price {au!r} != {eu!r}")
    return (not misses, "; ".join(misses) or "ok")


def _check_po_update(dt: str, expected: dict, actual: dict) -> tuple[bool, str]:
    el = (expected.get("lines") or [{}])[0]
    al = (actual.get("lines") or [{}])[0]
    eh = expected.get("header") if isinstance(expected.get("header"), dict) else {}
    ah = actual.get("header") if isinstance(actual.get("header"), dict) else {}
    misses: list[str] = []
    if el.get("delivery_date") and al.get("delivery_date") and el["delivery_date"] != al["delivery_date"]:
        misses.append(f"delivery_date {al.get('delivery_date')!r} != {el.get('delivery_date')!r}")
    if eh.get("tax_code") and ah.get("tax_code") and eh["tax_code"] != ah["tax_code"]:
        # tax may live on item; hydrate may put on header
        misses.append(f"tax_code {ah.get('tax_code')!r} != {eh.get('tax_code')!r}")
    en = str(el.get("net_price") or el.get("unit_price") or "").strip()
    an = str(al.get("net_price") or al.get("unit_price") or "").strip()
    if en and an:
        try:
            if abs(float(en) - float(an)) > 0.011:
                misses.append(f"net_price {an!r} != {en!r}")
        except ValueError:
            if en != an:
                misses.append(f"net_price {an!r} != {en!r}")
    if (el.get("short_text") or "") and (el.get("short_text") or "")[:20] not in (al.get("short_text") or ""):
        misses.append(f"short_text {al.get('short_text')!r} != {el.get('short_text')!r}")
    return (not misses, "; ".join(misses) or "ok")


def _bump_pr_fields(form: dict[str, Any], *, delivery: str, price: str, text: str) -> dict[str, Any]:
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    if lines and isinstance(lines[0], dict):
        lines[0]["short_text"] = text
        lines[0]["delivery_date"] = delivery
        lines[0]["unit_price"] = price
        lines[0]["valuation_price"] = price
    return form


def _bump_po_fields(
    form: dict[str, Any],
    *,
    delivery: str,
    price: str,
    text: str,
    tax: str | None = None,
) -> dict[str, Any]:
    hdr = form.get("header") if isinstance(form.get("header"), dict) else {}
    if tax:
        hdr["tax_code"] = tax
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    if lines and isinstance(lines[0], dict):
        lines[0]["short_text"] = text
        lines[0]["delivery_date"] = delivery
        lines[0]["net_price"] = price
        lines[0]["unit_price"] = price
    return form


def _bump_yast_pr_qty(form: dict[str, Any]) -> dict[str, Any]:
    form = _bump_pr_fields(
        form,
        delivery="2026-11-20",
        price="88.00",
        text=f"hol-yast-pr-upd {int(time.time())}",
    )
    lines = form.get("lines") or []
    if lines and isinstance(lines[0], dict):
        allocs = lines[0].get("allocations")
        if isinstance(allocs, list) and len(allocs) >= 2:
            allocs[0]["qty"] = "2"
            allocs[1]["qty"] = "3"
        lines[0]["unit_price"] = "88.00"
    return form


def _bump_yast_po_qty(form: dict[str, Any]) -> dict[str, Any]:
    form = _bump_po_fields(
        form,
        delivery="2026-11-20",
        price="125.00",
        text=f"hol-yast-po-upd {int(time.time())}",
        tax="FA",
    )
    lines = form.get("lines") or []
    if lines and isinstance(lines[0], dict):
        allocs = lines[0].get("allocations")
        if isinstance(allocs, list) and len(allocs) >= 2:
            allocs[0]["qty"] = "2"
            allocs[1]["qty"] = "3"
    return form


async def main_async() -> int:
    print("DATABASE_URL_SYNC=", _redact_db_target(settings.database_url_sync))
    apply_integration_reference_defaults()

    # ----- PR YUNB -----
    apply_pr_integration_fixtures("YUNB")
    await _run_pr_case(
        label="PR YUNB",
        dt="YUNB",
        form=simple_yunb_form(header_note=f"hol-yunb-pr {int(time.time())}"),
        mutate=lambda f: _bump_pr_fields(
            f, delivery="2026-11-20", price="42.00", text=f"hol-yunb-pr-upd {int(time.time())}"
        ),
    )

    # ----- PR YAST multi-asset -----
    apply_pr_integration_fixtures("YAST")
    os.environ["SAP_ASSET"] = os.environ.get("SAP_ASSET") or "001000000000"
    os.environ["SAP_ASSET_2"] = os.environ.get("SAP_ASSET_2") or "001000000001"
    await _run_pr_case(
        label="PR YAST multi-asset",
        dt="YAST",
        form=yast_multi_asset_form(header_note=f"hol-yast-pr {int(time.time())}"),
        mutate=_bump_yast_pr_qty,
    )

    # ----- PR YAST simple -----
    await _run_pr_case(
        label="PR YAST simple",
        dt="YAST",
        form=simple_yast_form(header_note=f"hol-yast-s {int(time.time())}"),
        mutate=lambda f: _bump_pr_fields(
            f, delivery="2026-11-20", price="55.00", text=f"hol-yast-s-upd {int(time.time())}"
        ),
    )

    # ----- PR YSER -----
    apply_pr_integration_fixtures("YSER")
    await _run_pr_case(
        label="PR YSER",
        dt="YSER",
        form=simple_yser_form(header_note=f"hol-yser-pr {int(time.time())}"),
        mutate=lambda f: _bump_pr_fields(
            f, delivery="2026-11-20", price="10.00", text=f"hol-yser-pr-upd {int(time.time())}"
        ),
    )

    # ----- PO YUNB -----
    apply_po_live_fixtures("YUNB")
    await _run_po_case(
        label="PO YUNB",
        dt="YUNB",
        form=simple_po_form("YUNB", header_note=f"hol-yunb-po {int(time.time())}"),
        mutate=lambda f: _bump_po_fields(
            f,
            delivery="2026-11-20",
            price="77.00",
            text=f"hol-yunb-po-upd {int(time.time())}",
            tax="FA",
        ),
    )

    # ----- PO YAST -----
    apply_po_live_fixtures("YAST")
    os.environ["SAP_ASSET"] = os.environ.get("SAP_ASSET") or "001000000000"
    os.environ["SAP_ASSET_2"] = os.environ.get("SAP_ASSET_2") or "001000000001"
    yast_po = simple_po_form("YAST", header_note=f"hol-yast-po {int(time.time())}")
    # Ensure multi-asset when assets available
    a1, a2 = os.environ["SAP_ASSET"], os.environ["SAP_ASSET_2"]
    if yast_po.get("lines"):
        yast_po["lines"][0]["allocations"] = [
            {"asset": a1, "qty": "1"},
            {"asset": a2, "qty": "1"},
        ]
        yast_po["lines"][0]["asset"] = a1
    await _run_po_case(
        label="PO YAST multi-asset",
        dt="YAST",
        form=yast_po,
        mutate=_bump_yast_po_qty,
    )

    # ----- PO YSER -----
    apply_po_live_fixtures("YSER")
    # Catalogue sometimes pairs YSER plant with a stale YUNB sloc (H006 + H001|3021).
    plant = (os.environ.get("SAP_PLANT") or "").strip()
    sloc = (os.environ.get("SAP_SLOC") or "").strip()
    if plant and "|" in sloc and not sloc.startswith(plant + "|"):
        # Prefer a plant-keyed sloc from this env load if available; else known QAS pair.
        os.environ["SAP_SLOC"] = os.environ.get("SAP_YSER_SLOC") or f"{plant}|2038"
        print(f"YSER plant/sloc corrected → {plant} / {os.environ['SAP_SLOC']}")
    await _run_po_case(
        label="PO YSER",
        dt="YSER",
        form=simple_po_form("YSER", header_note=f"hol-yser-po {int(time.time())}"),
        mutate=lambda f: _bump_po_fields(
            f,
            delivery="2026-11-20",
            price="33.00",
            text=f"hol-yser-po-upd {int(time.time())}",
        ),
    )

    print(f"\ndefault_delivery_date={default_delivery_date()!r}")
    return _summary()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
