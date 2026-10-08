"""Align PO forms with live SAP PR data before PO OData create."""

from __future__ import annotations

import copy
from typing import Any

from app.procurement.field_schema import PO_TEXT_HEADER_KEYS
from app.procurement.line_catalog_group import catalog_group_field, sync_header_catalog_group
from app.procurement.line_tax_code import line_tax_code, sync_header_tax_code
from app.procurement.sap_odata_utils import (
    default_procurement_delivery_date,
    sanitize_form_delivery_date,
)
from app.procurement.sap_odata_utils import odata_norm as _norm
from app.procurement.sap_pr_items import SapPrItemLine, fetch_pr_items
from app.procurement.sap_ticket_form_read import _material_pr_prices_from_sap


def _line_material_or_service(document_type: str, row: dict[str, Any]) -> str:
    dt = (document_type or "").upper()
    if dt == "YSER":
        return str(row.get("service") or row.get("material") or "").strip()
    return str(row.get("material") or "").strip()


def _allocation_cc_set(row: dict[str, Any]) -> frozenset[str]:
    allocs = row.get("allocations")
    if not isinstance(allocs, list):
        return frozenset()
    return frozenset(
        str(a.get("cost_center") or "").strip()
        for a in allocs
        if isinstance(a, dict) and str(a.get("cost_center") or "").strip()
    )


def _pr_cc_set(item: SapPrItemLine) -> frozenset[str]:
    return frozenset(r.cost_center for r in item.acct_rows if r.cost_center)


def _norm_item_no(item_no: str) -> str:
    raw = str(item_no or "").strip()
    if raw.isdigit():
        return str(int(raw))
    return raw


def _sap_pr_item_no(item: SapPrItemLine) -> str:
    return _norm_item_no(item.sap_pr_item or item.item_no)


def _yser_item_matches_pr_ref(item: SapPrItemLine, pr_ref: str) -> bool:
    ref = _norm_item_no(pr_ref)
    if not ref:
        return False
    return _norm_item_no(item.item_no) == ref or _sap_pr_item_no(item) == ref


def _best_yser_pr_item_match(
    row: dict[str, Any],
    candidates: list[SapPrItemLine],
) -> SapPrItemLine | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    svc = _line_material_or_service("YSER", row)
    pool = candidates
    if svc:
        svc_matches = [i for i in candidates if i.service_performer == svc]
        if len(svc_matches) == 1:
            return svc_matches[0]
        if svc_matches:
            pool = svc_matches

    line_ccs = _allocation_cc_set(row)
    if line_ccs:
        scored = [(len(line_ccs & _pr_cc_set(i)), i) for i in pool]
        scored.sort(key=lambda t: (-t[0], int(t[1].item_no or "0")))
        if scored[0][0] > 0:
            return scored[0][1]

    return pool[0]


def _item_no_matches(a: str, b: str) -> bool:
    return _norm_item_no(a) == _norm_item_no(b)


def _resolve_yser_po_line_pr_item(
    row: dict[str, Any],
    *,
    sap_items: list[SapPrItemLine],
) -> SapPrItemLine | None:
    """Map one YSER PO line to its SAP PR service row (PR item + service + CC)."""
    if not sap_items:
        return None
    pri = str(row.get("purchase_requisition_item") or row.get("sap_pr_item") or "").strip()
    if pri:
        matches = [i for i in sap_items if _yser_item_matches_pr_ref(i, pri)]
        hit = _best_yser_pr_item_match(row, matches)
        if hit:
            return hit
    return match_po_line_to_pr_item(
        row=row,
        document_type="YSER",
        sap_items=sap_items,
        used_item_nos=set(),
    )


def _stamp_yser_po_line_from_pr_item(row: dict[str, Any], pr_item: SapPrItemLine) -> None:
    """Header fields from SAP PR item — keep Z-read ``purchase_requisition_item`` when set."""
    if not str(row.get("purchase_requisition_item") or "").strip():
        row["purchase_requisition_item"] = _sap_pr_item_no(pr_item)
    row["account_assignment_cat"] = pr_item.acct_cat
    if pr_item.item_cat:
        row["item_category"] = pr_item.item_cat
    if pr_item.unit and not str(row.get("order_unit") or "").strip():
        row["order_unit"] = pr_item.unit
    if pr_item.price and not str(row.get("unit_price") or "").strip():
        row["unit_price"] = pr_item.price
        row["net_price"] = pr_item.price


def match_po_line_to_pr_item(
    *,
    row: dict[str, Any],
    document_type: str,
    sap_items: list[SapPrItemLine],
    used_item_nos: set[str],
) -> SapPrItemLine | None:
    """Map one PO form line to a SAP PR item (supports partial PR → PO)."""
    dt = (document_type or "").upper()
    candidates = [i for i in sap_items if i.item_no not in used_item_nos]
    if not candidates and not (dt == "YSER" and sap_items):
        return None

    explicit = str(row.get("purchase_requisition_item") or row.get("sap_pr_item") or "").strip()
    if explicit:
        pool = sap_items if dt == "YSER" else candidates
        if dt == "YSER":
            explicit_matches = [i for i in pool if _yser_item_matches_pr_ref(i, explicit)]
            hit = _best_yser_pr_item_match(row, explicit_matches)
            if hit:
                return hit
        for item in pool:
            if _item_no_matches(item.item_no, explicit):
                return item

    key = _line_material_or_service(document_type, row)
    if key:
        material_matches = [
            i
            for i in candidates
            if i.material == key
            or ((document_type or "").upper() == "YSER" and i.service_performer == key)
        ]
        if len(material_matches) == 1:
            return material_matches[0]
        if len(material_matches) > 1:
            line_ccs = _allocation_cc_set(row)
            best: SapPrItemLine | None = None
            best_score = -1
            for item in material_matches:
                score = len(line_ccs & _pr_cc_set(item)) if line_ccs else 0
                if score > best_score:
                    best_score = score
                    best = item
            if best:
                return best
            return material_matches[0]

    line_ccs = _allocation_cc_set(row)
    if line_ccs:
        scored = [(len(line_ccs & _pr_cc_set(i)), i) for i in candidates]
        scored.sort(key=lambda t: (-t[0], int(t[1].item_no or "0")))
        if scored[0][0] > 0:
            return scored[0][1]

    return candidates[0]


def _qty_for_ui(qty: str, *, fallback: str = "") -> str:
    raw = str(qty or "").strip() or str(fallback or "").strip() or "1"
    try:
        value = float(raw.replace(",", "."))
        if value == int(value):
            return str(int(value))
    except ValueError:
        pass
    return raw


def pr_allocation_skeleton(
    sap_item: SapPrItemLine,
    *,
    document_type: str,
) -> list[dict[str, str]]:
    """Cost-centre / asset split structure from SAP PR (qty is PR default only)."""
    dt = (document_type or "").upper()
    rows: list[dict[str, str]] = []
    for a in sap_item.acct_rows:
        qty = _qty_for_ui(a.quantity, fallback=sap_item.qty)
        if dt == "YAST":
            asset = a.master_asset or sap_item.master_asset or ""
            if asset or qty:
                rows.append({"asset": asset, "qty": qty})
        elif a.cost_center:
            rows.append({"cost_center": a.cost_center, "qty": qty})
    if not rows:
        if dt == "YAST":
            rows.append(
                {
                    "asset": sap_item.master_asset or "",
                    "qty": _qty_for_ui(sap_item.qty),
                }
            )
        else:
            rows.append({"cost_center": "", "qty": _qty_for_ui(sap_item.qty)})
    return rows


def _allocation_structure_matches(
    pr_rows: list[dict[str, str]],
    user_rows: list[dict[str, Any]],
    *,
    document_type: str,
) -> bool:
    if len(pr_rows) != len(user_rows):
        return False
    dt = (document_type or "").upper()
    from app.procurement.sap_yast_acct import asset_codes_equal

    for pr_row, user_row in zip(pr_rows, user_rows):
        if not isinstance(user_row, dict):
            return False
        if dt == "YAST":
            if not asset_codes_equal(
                str(user_row.get("asset") or ""),
                pr_row.get("asset", ""),
            ):
                return False
        elif str(user_row.get("cost_center") or "").strip() != pr_row.get("cost_center", ""):
            return False
    return True


def merge_allocations_from_pr(
    sap_item: SapPrItemLine,
    *,
    document_type: str,
    existing: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """Keep PR cost centres / assets / split rows; allow PO to change quantities only."""
    dt = (document_type or "").upper()
    skeleton = pr_allocation_skeleton(sap_item, document_type=document_type)
    if not isinstance(existing, list):
        return skeleton
    if not _allocation_structure_matches(skeleton, existing, document_type=document_type):
        if len(skeleton) == 1 and len(existing) == 1 and isinstance(existing[0], dict):
            ex = existing[0]
            qty = _qty_for_ui(str(ex.get("qty") or ""), fallback=skeleton[0]["qty"])
            if dt == "YAST":
                return [{"asset": skeleton[0].get("asset", ""), "qty": qty}]
            return [
                {
                    "cost_center": skeleton[0]["cost_center"],
                    "qty": qty,
                }
            ]
        return skeleton
    merged: list[dict[str, str]] = []
    for i, skel in enumerate(skeleton):
        ex = existing[i] if isinstance(existing[i], dict) else {}
        qty = _qty_for_ui(str(ex.get("qty") or ""), fallback=skel["qty"])
        if dt == "YAST":
            merged.append({"asset": skel.get("asset", ""), "qty": qty})
        else:
            merged.append({"cost_center": skel["cost_center"], "qty": qty})
    return merged


def _set_header_if_empty(header: dict[str, Any], key: str, value: str) -> None:
    if value and not str(header.get(key) or "").strip():
        header[key] = value


def clear_po_only_text_header_fields(header: dict[str, Any]) -> None:
    """PO long-text fields must not inherit from PR prefill."""
    for key in PO_TEXT_HEADER_KEYS:
        header[key] = ""


def merge_po_form_from_sap_pr_read(
    form: dict[str, Any],
    sap_form: dict[str, Any],
    *,
    document_type: str,
) -> None:
    """Fill empty PO fields from SAP PR GET (``form_from_pr_sap_read``) — user edits are kept."""
    header = form.get("header")
    sap_h = sap_form.get("header") if isinstance(sap_form.get("header"), dict) else {}
    if isinstance(header, dict) and sap_h:
        for key in (
            "purchasing_org",
            "company_code",
            "purchasing_group",
            "plant",
            "storage_location",
            "material_group",
            "service_group",
            "vendor",
            "tax_code",
            "tax_jurisdiction",
            "payment_terms",
            "header_note",
        ):
            _set_header_if_empty(header, key, str(sap_h.get(key) or "").strip())

    sap_lines = sap_form.get("lines")
    if not isinstance(sap_lines, list):
        return
    lines = form.get("lines")
    if not isinstance(lines, list):
        return
    dt = (document_type or "").upper()
    by_item: dict[str, dict[str, Any]] = {}
    for sl in sap_lines:
        if isinstance(sl, dict):
            item_no = str(sl.get("purchase_requisition_item") or "").strip()
            if item_no:
                by_item[item_no] = sl
    for row in lines:
        if not isinstance(row, dict):
            continue
        sap_ln = by_item.get(str(row.get("purchase_requisition_item") or "").strip())
        if not sap_ln and len(by_item) == 1:
            sap_ln = next(iter(by_item.values()))
        if not sap_ln:
            continue
        if not sanitize_form_delivery_date(str(row.get("delivery_date") or "")):
            dd = sanitize_form_delivery_date(
                str(sap_ln.get("delivery_date") or ""),
                fallback=default_procurement_delivery_date(),
            )
            if dd:
                row["delivery_date"] = dd
        if dt == "YSER" and not str(row.get("service") or "").strip():
            svc = str(sap_ln.get("service") or "").strip()
            if svc:
                row["service"] = svc
        if not str(row.get("order_unit") or "").strip():
            uom = str(sap_ln.get("order_unit") or "").strip()
            if uom:
                row["order_unit"] = uom
        if dt != "YSER" and not str(row.get("material") or "").strip():
            mat = str(sap_ln.get("material") or "").strip()
            if mat:
                row["material"] = mat
        grp_key = catalog_group_field(dt)
        if not str(row.get(grp_key) or "").strip():
            grp = str(sap_ln.get(grp_key) or "").strip()
            if grp:
                row[grp_key] = grp
        if not str(row.get("tax_code") or "").strip():
            tc = str(sap_ln.get("tax_code") or "").strip()
            if tc:
                row["tax_code"] = tc
    header = form.get("header")
    if isinstance(header, dict):
        sync_header_catalog_group(header, lines, document_type)
        sync_header_tax_code(header, lines)


def apply_sap_pr_item_defaults_to_header(
    header: dict[str, Any],
    sap_items: list[SapPrItemLine],
    *,
    document_type: str = "",
) -> None:
    """Header org/plant/vendor from SAP PR items when PO header fields are still empty."""
    if not sap_items:
        return
    dt = (document_type or "").upper()
    first = sap_items[0]
    _set_header_if_empty(header, "plant", first.plant)
    _set_header_if_empty(header, "storage_location", first.sloc)
    _set_header_if_empty(header, "material_group", first.material_group)
    if dt == "YSER":
        _set_header_if_empty(header, "service_group", first.material_group)
    _set_header_if_empty(header, "purchasing_group", first.pur_group)
    if first.pur_org:
        _set_header_if_empty(header, "purchasing_org", first.pur_org)
        _set_header_if_empty(header, "company_code", first.pur_org)
    for item in sap_items:
        if item.fixed_supplier:
            _set_header_if_empty(header, "vendor", item.fixed_supplier)
            break


def _apply_sap_item_to_po_line(
    row: dict[str, Any],
    *,
    sap_item: SapPrItemLine,
    document_type: str,
) -> None:
    dt = (document_type or "").upper()
    row["purchase_requisition_item"] = _sap_pr_item_no(sap_item) if dt == "YSER" else sap_item.item_no
    if dt == "YSER":
        row["sap_pr_item"] = _sap_pr_item_no(sap_item)
    if not sanitize_form_delivery_date(str(row.get("delivery_date") or "")):
        dd = sanitize_form_delivery_date(
            sap_item.delivery_date,
            fallback=default_procurement_delivery_date(),
        )
        if dd:
            row["delivery_date"] = dd
    if sap_item.unit and not str(row.get("order_unit") or "").strip():
        row["order_unit"] = sap_item.unit
    if sap_item.short_text and not str(row.get("short_text") or "").strip():
        row["short_text"] = sap_item.short_text
    row["account_assignment_cat"] = sap_item.acct_cat
    if sap_item.item_cat:
        row["item_category"] = sap_item.item_cat
    grp_key = catalog_group_field(dt)
    if sap_item.material_group and not str(row.get(grp_key) or "").strip():
        row[grp_key] = sap_item.material_group
    if sap_item.price and not str(row.get("unit_price") or "").strip():
        row["unit_price"] = sap_item.price
        row["net_price"] = sap_item.price

    existing_allocs = row.get("allocations")
    if not isinstance(existing_allocs, list):
        existing_allocs = []
    # Z PR fans out one item → many services; keep per-service allocations from prefill.
    if dt == "YSER" and str(row.get("service") or "").strip():
        has_cc = any(
            isinstance(a, dict) and str(a.get("cost_center") or "").strip()
            for a in existing_allocs
        )
        if has_cc:
            return

    # Cost centres / split rows from PR; quantities may differ on the PO.
    row["allocations"] = merge_allocations_from_pr(
        sap_item, document_type=document_type, existing=existing_allocs
    )

    if dt == "YSER" and sap_item.service_performer and not str(row.get("service") or "").strip():
        row["service"] = sap_item.service_performer
    if dt != "YSER" and sap_item.material and not str(row.get("material") or "").strip():
        row["material"] = sap_item.material
    if dt == "YAST" and sap_item.master_asset:
        row["asset"] = sap_item.master_asset
    _stamp_po_line_valuation(row)


def _stamp_po_line_valuation(row: dict[str, Any]) -> None:
    """UI line total = per-unit price × sum of allocation qty (not sent to SAP on PO)."""
    unit = str(row.get("unit_price") or row.get("net_price") or "").strip()
    if not unit:
        return
    allocs = row.get("allocations")
    if not isinstance(allocs, list):
        allocs = []
    _, val_p, _ = _material_pr_prices_from_sap(unit, allocs)
    if val_p:
        row["valuation_price"] = val_p


async def enrich_po_form_from_sap_pr(
    form: dict[str, Any],
    *,
    document_type: str,
    pr_sap_id: str,
    ticket_id: str,
) -> str | None:
    """Mutate PO ``form`` in place from live SAP PR. Returns error message or None."""
    items, err = await fetch_pr_items(
        pr_number=pr_sap_id, ticket_id=ticket_id, document_type=document_type
    )
    if err:
        return err
    return _enrich_po_form_with_items(form, document_type=document_type, sap_items=items)


def _enrich_po_form_with_items(
    form: dict[str, Any],
    *,
    document_type: str,
    sap_items: list[SapPrItemLine],
) -> str | None:
    header = form.get("header")
    if not isinstance(header, dict):
        form["header"] = {}
        header = form["header"]

    lines = form.get("lines")
    if not isinstance(lines, list) or not lines:
        return "PO form has no lines to link to SAP PR"

    dt = (document_type or "").upper()
    used: set[str] = set()
    for row in lines:
        if not isinstance(row, dict):
            continue
        sap_item = match_po_line_to_pr_item(
            row=row,
            document_type=document_type,
            sap_items=sap_items,
            used_item_nos=used,
        )
        if not sap_item:
            return "Could not match a PO line to SAP PR item — check service/material and cost centres"
        if dt != "YSER":
            used.add(sap_item.item_no)
        _apply_sap_item_to_po_line(row, sap_item=sap_item, document_type=document_type)

    apply_sap_pr_item_defaults_to_header(header, sap_items, document_type=document_type)
    sync_header_catalog_group(header, lines, document_type)
    return None


def sap_pr_items_to_po_form(
    items: list[SapPrItemLine],
    *,
    document_type: str,
    header_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a PO draft from SAP PR lines (prefill — user may delete lines for partial PO)."""
    dt = (document_type or "").upper()
    header: dict[str, Any] = {}
    if isinstance(header_seed, dict):
        seed_h = header_seed.get("header")
        if isinstance(seed_h, dict):
            header = copy.deepcopy(seed_h)

    clear_po_only_text_header_fields(header)
    apply_sap_pr_item_defaults_to_header(header, items, document_type=document_type)

    lines: list[dict[str, Any]] = []
    for sap_item in items:
        if dt == "YAST":
            allocs = [
                {
                    "asset": a.master_asset or sap_item.master_asset or "",
                    "qty": a.quantity or sap_item.qty,
                }
                for a in sap_item.acct_rows
                if a.master_asset or a.quantity
            ]
            if not allocs and sap_item.master_asset:
                allocs = [{"asset": sap_item.master_asset, "qty": sap_item.qty}]
        else:
            allocs = [
                {"cost_center": a.cost_center, "qty": a.quantity or sap_item.qty}
                for a in sap_item.acct_rows
                if a.cost_center
            ]
        row: dict[str, Any] = {
            "material": sap_item.material,
            "service": sap_item.service_performer,
            "short_text": sap_item.short_text,
            "unit_price": sap_item.price,
            "net_price": sap_item.price,
            "delivery_date": sanitize_form_delivery_date(
                sap_item.delivery_date,
                fallback=default_procurement_delivery_date(),
            ),
            "allocations": allocs,
        }
        _apply_sap_item_to_po_line(row, sap_item=sap_item, document_type=document_type)
        lines.append(row)

    seed_lines = (
        header_seed.get("lines")
        if isinstance(header_seed, dict) and isinstance(header_seed.get("lines"), list)
        else []
    )
    for i, row in enumerate(lines):
        if not isinstance(row, dict):
            continue
        if _norm(row.get("tax_code")):
            continue
        seed_row = seed_lines[i] if i < len(seed_lines) and isinstance(seed_lines[i], dict) else {}
        pri = _norm(row.get("purchase_requisition_item"))
        if isinstance(seed_lines, list) and pri:
            for sl in seed_lines:
                if isinstance(sl, dict) and _norm(sl.get("purchase_requisition_item")) == pri:
                    seed_row = sl
                    break
        seed_h = (
            header_seed.get("header")
            if isinstance(header_seed, dict) and isinstance(header_seed.get("header"), dict)
            else {}
        )
        tc = line_tax_code(seed_row if isinstance(seed_row, dict) else {}, seed_h)
        if tc:
            row["tax_code"] = tc

    sync_header_catalog_group(header, lines, document_type)
    sync_header_tax_code(header, lines)
    return {"header": header, "lines": lines}


def _po_allocations_match_yser(
    form: dict[str, Any],
    *,
    sap_items: list[SapPrItemLine],
) -> str | None:
    """YSER Z PR: each PO service line must match its linked PR item (or legacy single item)."""
    lines = form.get("lines")
    if not isinstance(lines, list):
        return "PO form has no lines"
    if not sap_items:
        return "Could not match a PO line to SAP PR item"
    legacy_union = _pr_cc_set(sap_items[0]) if len(sap_items) == 1 else frozenset()
    for row in lines:
        if not isinstance(row, dict):
            continue
        line_ccs = _allocation_cc_set(row)
        if not line_ccs:
            return (
                "Account assignment cannot be changed on a purchase order linked to a "
                "purchase request — cost centre splits must match SAP PR"
            )
        pr_item = _resolve_yser_po_line_pr_item(row, sap_items=sap_items)
        if not pr_item:
            return "Could not match a PO line to SAP PR item"
        pr_ccs = _pr_cc_set(pr_item)
        if line_ccs.issubset(pr_ccs):
            continue
        if len(sap_items) == 1 and line_ccs.issubset(legacy_union):
            continue
        return (
            "Account assignment cannot be changed on a purchase order linked to a "
            "purchase request — cost centre splits must match SAP PR"
        )
    return None


def po_allocations_match_sap_pr(
    form: dict[str, Any],
    *,
    document_type: str,
    sap_items: list[SapPrItemLine],
) -> str | None:
    """Return error if PO form allocations differ from SAP PR (linked PO must not change acct)."""
    dt = (document_type or "").upper()
    if dt == "YSER":
        return _po_allocations_match_yser(form, sap_items=sap_items)
    lines = form.get("lines")
    if not isinstance(lines, list):
        return "PO form has no lines"
    used: set[str] = set()
    for row in lines:
        if not isinstance(row, dict):
            continue
        sap_item = match_po_line_to_pr_item(
            row=row,
            document_type=document_type,
            sap_items=sap_items,
            used_item_nos=used,
        )
        if not sap_item:
            return "Could not match a PO line to SAP PR item"
        used.add(sap_item.item_no)
        expected = pr_allocation_skeleton(sap_item, document_type=document_type)
        actual_in = row.get("allocations")
        if not isinstance(actual_in, list):
            return "PO line allocations must match the linked purchase request"
        if not _allocation_structure_matches(expected, actual_in, document_type=document_type):
            if dt == "YAST":
                return (
                    "Account assignment cannot be changed on a purchase order linked to a "
                    "purchase request — asset splits must match SAP PR"
                )
            return (
                "Account assignment cannot be changed on a purchase order linked to a "
                "purchase request — cost centre splits must match SAP PR"
            )
        if dt == "YAST":
            from app.procurement.sap_yast_acct import asset_codes_equal

            act_assets: list[str] = []
            for a in actual_in:
                if isinstance(a, dict):
                    av = str(a.get("asset") or "").strip()
                    if av:
                        act_assets.append(av)
            line_asset = _norm(row.get("asset"))
            if line_asset and act_assets and not asset_codes_equal(line_asset, act_assets[0]):
                return (
                    "Asset on the purchase order must match the linked purchase request"
                )
    return None


async def build_po_prefill_form_from_pr(
    pr_form: dict[str, Any] | None,
    *,
    document_type: str,
    pr_sap_id: str,
    ticket_id: str,
) -> tuple[dict[str, Any], str | None]:
    """SAP-first PO prefill; falls back to DB copy of PR form on read failure."""
    from app.procurement.sap_ticket_form_read import form_from_pr_sap_read

    seed = pr_form if isinstance(pr_form, dict) else None
    dt = (document_type or "").upper()
    items, err = await fetch_pr_items(
        pr_number=pr_sap_id, ticket_id=ticket_id, document_type=document_type
    )
    if items and not err:
        form = sap_pr_items_to_po_form(
            items,
            document_type=document_type,
            header_seed=seed,
        )
        if dt == "YSER":
            from app.procurement.sap_pr_z_client import get_yser_pr
            from app.procurement.sap_pr_z_payload import form_from_z_pr_read

            body, read_err = await get_yser_pr(pr_number=pr_sap_id, ticket_id=ticket_id)
            if body and not read_err:
                sap_form = form_from_z_pr_read(
                    body, document_type=document_type, seed_form=seed
                )
                z_lines = sap_form.get("lines")
                if isinstance(z_lines, list) and z_lines:
                    # Z read: one UI line per service; each line keeps its PR item (10, 20, …).
                    form["lines"] = copy.deepcopy(z_lines)
                    for row in form["lines"]:
                        if not isinstance(row, dict):
                            continue
                        pr_item = _resolve_yser_po_line_pr_item(row, sap_items=items)
                        if pr_item is not None:
                            _stamp_yser_po_line_from_pr_item(row, pr_item)
                merge_po_form_from_sap_pr_read(
                    form, sap_form, document_type=document_type
                )
        else:
            from app.procurement.sap_pr_client import get_pr

            body, read_err = await get_pr(pr_number=pr_sap_id, ticket_id=ticket_id)
            if body and not read_err:
                sap_form = form_from_pr_sap_read(
                    body, document_type=document_type, seed_form=seed
                )
                merge_po_form_from_sap_pr_read(
                    form, sap_form, document_type=document_type
                )
        header = form.get("header")
        if not isinstance(header, dict):
            form["header"] = {}
            header = form["header"]
        clear_po_only_text_header_fields(header)
        apply_sap_pr_item_defaults_to_header(header, items, document_type=document_type)
        from app.procurement.field_schema import normalize_form

        return normalize_form(document_type, form), None
    if isinstance(pr_form, dict):
        form = copy.deepcopy(pr_form)
        header = form.get("header")
        if isinstance(header, dict):
            clear_po_only_text_header_fields(header)
        from app.procurement.field_schema import normalize_form

        return normalize_form(document_type, form), err
    return {"header": {}, "lines": []}, err
