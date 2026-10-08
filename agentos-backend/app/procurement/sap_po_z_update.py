"""YSER PO incremental Z update (Jul 2026 SAP API — one mutation per POST)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.procurement.line_catalog_group import line_catalog_group
from app.procurement.line_tax_code import line_tax_code, yser_po_group_item_tax_code
from app.procurement.sap_po_payload import (
    build_po_item_text_for_sap,
    format_po_purchase_requisition_item,
    _tax_jurisdiction_for_po_item,
)
from app.procurement.sap_po_z_payload import (
    PO_SERVICE_ACCT_NAV,
    PO_SERVICES_NAV,
    SapZPoItemSnapshot,
    _alloc_purg_amount,
    _build_z_po_delete_item_stub,
    _build_z_po_item_row,
    _company_code,
    _ensure_yser_po_service_refs,
    _fixed_supplier,
    _format_po_z_price,
    _format_z_qty,
    _item_total_quantity,
    _line_total_amount,
    _linked_pr_item_number,
    _unit_price_from_block,
    _wrap_z_po_nav_lists,
    _yser_line_unit,
    _yser_po_acct_cat,
    _yser_po_service_external_ref,
    _z_po_item_delivery_date_for_sap,
    _z_po_payment_terms,
    apply_yser_po_texts_to_z_inner,
    format_z_po_item_number,
    merge_yser_po_update_form_with_sap,
    normalize_service_performer_code,
    prepare_yser_po_create_reconcile_forms,
    validate_yser_po_service_refs_unique,
    yser_effective_line_blocks,
    yser_po_max_service_ref_seq_from_form,
)
from app.procurement.sap_odata_utils import odata_norm as _norm


@dataclass(frozen=True)
class _ServiceSnap:
    key: str
    ext_ref: str
    po_item: str
    block: dict[str, Any]


def _service_key(block: dict[str, Any], *, line_index: int) -> str:
    ref = _yser_po_service_external_ref(block)
    if ref:
        return f"ref:{ref}"
    po_item = format_z_po_item_number(_norm(block.get("purchase_order_item")) or "10")
    svc = normalize_service_performer_code(_norm(block.get("service")))
    return f"legacy:{po_item}:{svc}:{line_index}"


def _index_services(form: dict[str, Any]) -> dict[str, _ServiceSnap]:
    out: dict[str, _ServiceSnap] = {}
    for idx, block in enumerate(yser_effective_line_blocks(form)):
        key = _service_key(block, line_index=idx)
        po_item = format_z_po_item_number(_norm(block.get("purchase_order_item")) or "10")
        out[key] = _ServiceSnap(
            key=key,
            ext_ref=_yser_po_service_external_ref(block),
            po_item=po_item,
            block=block,
        )
    return out


def _alloc_rows(block: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for alloc in block.get("allocations") or []:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qty_f = float(qty_raw.replace(",", "."))
            qty = (
                str(int(qty_f))
                if abs(qty_f - round(qty_f)) < 1e-9
                else f"{qty_f:.3f}".rstrip("0").rstrip(".")
            )
        except ValueError:
            qty = qty_raw
        rows.append((cc, qty))
    return sorted(rows)


def _alloc_rows_resolved(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seq = 1
    for alloc in block.get("allocations") or []:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        acct = _norm(alloc.get("po_acct_assgmt_number")) or str(seq).zfill(2)
        seq += 1
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qty_f = float(qty_raw.replace(",", "."))
            qty = (
                str(int(qty_f))
                if abs(qty_f - round(qty_f)) < 1e-9
                else f"{qty_f:.3f}".rstrip("0").rstrip(".")
            )
        except ValueError:
            qty = qty_raw
        rows.append((cc, qty, acct))
    return sorted(rows)


def _norm_price(raw: Any) -> str:
    s = _norm(raw)
    if not s:
        return ""
    try:
        return f"{float(s.replace(',', '.')):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def _field_fields_changed(
    a: dict[str, Any], b: dict[str, Any], *, header: dict[str, Any] | None = None
) -> bool:
    for field in ("short_text", "delivery_date", "order_unit"):
        if _norm(a.get(field)) != _norm(b.get(field)):
            return True
    for field in ("unit_price", "valuation_price", "net_price"):
        if _norm_price(a.get(field)) != _norm_price(b.get(field)):
            return True
    if _alloc_rows(a) != _alloc_rows(b):
        return True
    if normalize_service_performer_code(_norm(a.get("service"))) != normalize_service_performer_code(
        _norm(b.get("service"))
    ):
        return True
    if header is not None:
        if line_tax_code(a, header) != line_tax_code(b, header):
            return True
    elif _norm(a.get("tax_code")) != _norm(b.get("tax_code")):
        return True
    return False


def yser_po_has_structural_delta(*, sap_form: dict[str, Any], submitted: dict[str, Any]) -> bool:
    sap_idx = _index_services(sap_form)
    sub_idx = _index_services(submitted)
    if set(sap_idx) != set(sub_idx):
        return True
    for key in sap_idx:
        if _alloc_rows(sap_idx[key].block) != _alloc_rows(sub_idx[key].block):
            return True
    return False


def yser_po_create_reconcile_needed(
    *, sap_form: dict[str, Any], submitted: dict[str, Any]
) -> bool:
    """True when SAP is missing one or more submitted services (post-create fallback only)."""
    sap_idx = _index_services(sap_form)
    sub_idx = _index_services(submitted)
    return bool(set(sub_idx) - set(sap_idx))


def _po_acct_rows(
    *,
    po_number: str,
    item_number: str,
    service_no: str,
    allocs: list[dict[str, Any]],
    unit: float,
    line_total: str,
    acct_deleted_ccs: frozenset[str] | None = None,
    single_cc_mode: bool = False,
) -> list[dict[str, Any]]:
    svc_no = normalize_service_performer_code(service_no)
    deleted = acct_deleted_ccs or frozenset()
    valid = [a for a in allocs if isinstance(a, dict) and _norm(a.get("cost_center"))]
    rows: list[dict[str, Any]] = []
    for alloc in valid:
        cc = _norm(alloc.get("cost_center"))
        qty_raw = _norm(alloc.get("qty")) or "1"
        is_del = cc in deleted
        try:
            qf = float(qty_raw.replace(",", "."))
        except ValueError:
            qf = 1.0
        if is_del:
            qf = 0.0
        qty = _format_z_qty(qf)
        row: dict[str, Any] = {
            "CostCenter": cc,
            "IsDeleted": is_del,
            "PurchaseOrder": _norm(po_number),
            "PurchaseOrderItem": item_number,
            "Quantity": qty,
            "PurgDocNetAmount": "0.000" if is_del else _alloc_purg_amount(unit=unit, alloc_qty=qf),
            "ServiceNumber": svc_no,
        }
        acct_no = _norm(alloc.get("po_acct_assgmt_number"))
        if acct_no:
            row["AccountAssignmentNumber"] = acct_no
        if single_cc_mode and cc not in deleted:
            row["MultipleAcctAssgmtDistrPercent"] = "100"
        rows.append(row)
    if not rows and not deleted:
        qty = _format_z_qty(1.0)
        rows.append(
            {
                "CostCenter": "",
                "IsDeleted": False,
                "PurchaseOrder": _norm(po_number),
                "PurchaseOrderItem": item_number,
                "Quantity": qty,
                "PurgDocNetAmount": line_total,
                "ServiceNumber": svc_no,
            }
        )
    return rows


def _po_service_row(
    *,
    po_number: str,
    item_number: str,
    block: dict[str, Any],
    header: dict[str, Any],
    allocs: list[dict[str, Any]],
    service_is_deleted: bool = False,
    acct_deleted_ccs: frozenset[str] | None = None,
    single_cc_mode: bool = False,
) -> dict[str, Any]:
    service_no = normalize_service_performer_code(_norm(block.get("service")))
    short_text = _norm(block.get("short_text"))
    qty_total = _item_total_quantity(allocs)
    unit = _unit_price_from_block(block)
    line_total = _line_total_amount(block, qty_total=qty_total)
    unit_str = _format_po_z_price(unit) if unit > 0 else line_total
    line_uom = _yser_line_unit(block)
    tax_code = line_tax_code(block, header)
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    multi_cc = len([a for a in allocs if isinstance(a, dict) and _norm(a.get("cost_center"))]) > 1
    if acct_deleted_ccs:
        multi_cc = True
    if single_cc_mode:
        multi_cc = False

    acct_rows = _po_acct_rows(
        po_number=po_number,
        item_number=item_number,
        service_no=service_no,
        allocs=allocs,
        unit=unit,
        line_total=line_total,
        acct_deleted_ccs=acct_deleted_ccs,
        single_cc_mode=single_cc_mode,
    )
    for row in acct_rows:
        if tax_code:
            row["TaxCode"] = tax_code
        if tax_jurisdiction:
            row["TaxJurisdiction"] = tax_jurisdiction

    svc: dict[str, Any] = {
        "AccountAssignmentCategory": "",
        "ConfirmedQuantity": _format_z_qty(qty_total),
        "IsDeleted": "X" if service_is_deleted else "",
        "NetAmount": unit_str,
        "NetPriceAmount": line_total,
        "Plant": "",
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_number,
        "QuantityUnit": line_uom,
        "Service": service_no,
        "ServiceEntrySheetItemDesc": short_text,
        "ServicePerformer": "",
        PO_SERVICE_ACCT_NAV: acct_rows,
    }
    if multi_cc and not single_cc_mode:
        svc["MultipleAcctAssgmtDistribution"] = "1"
    elif single_cc_mode:
        svc["MultipleAcctAssgmtDistribution"] = "0"
    ext_ref = _yser_po_service_external_ref(block)
    if ext_ref:
        svc["PurgDocItemExternalReference"] = ext_ref
    return svc


def _po_item_shell(
    *,
    po_number: str,
    po_item: str,
    block: dict[str, Any],
    header: dict[str, Any],
    service_rows: list[dict[str, Any]],
    parent_pr_number: str | None,
) -> dict[str, Any]:
    plant = _norm(header.get("plant"))
    from app.procurement.sap_odata_utils import storage_location_for_sap

    sloc = storage_location_for_sap(_norm(header.get("storage_location")), plant=plant)
    mat_grp = line_catalog_group(block, header, "YSER")
    item_no = format_z_po_item_number(po_item)
    tax_code = line_tax_code(block, header)
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    line_uom = _yser_line_unit(block)
    acct_cat = _yser_po_acct_cat(block)
    item_text = build_po_item_text_for_sap(_norm(block.get("short_text")) or mat_grp)

    net_total = 0.0
    any_multi_cc = False
    for svc_row in service_rows:
        try:
            net_total += float(str(svc_row.get("NetPriceAmount") or "0").replace(",", "."))
        except ValueError:
            pass
        dist = _norm(svc_row.get("MultipleAcctAssgmtDistribution"))
        if dist and dist != "0":
            any_multi_cc = True

    row: dict[str, Any] = {
        "IsReturnsItem": False,
        "MaterialGroup": mat_grp,
        "NetPriceAmount": f"{net_total:.2f}" if net_total > 0 else "0.00",
        "NetPriceQuantity": "1",
        "OrderPriceUnit": line_uom,
        "OrderQuantity": "0.000",
        "Plant": plant,
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_no,
        "PurchaseOrderItemCategory": "9",
        "PurchaseOrderItemText": item_text,
        "PurchaseOrderQuantityUnit": line_uom,
        "StorageLocation": sloc,
        PO_SERVICES_NAV: service_rows,
    }
    if acct_cat:
        row["AccountAssignmentCategory"] = acct_cat
    pr_no = _norm(parent_pr_number)
    if pr_no:
        row["PurchaseRequisition"] = pr_no
        row["PurchaseRequisitionItem"] = format_po_purchase_requisition_item(
            _linked_pr_item_number(block)
        )
    if tax_code:
        row["TaxCode"] = tax_code
    if tax_jurisdiction:
        row["TaxJurisdiction"] = tax_jurisdiction
    if any_multi_cc:
        row["MultipleAcctAssgmtDistribution"] = "1"
    item_delivery = _z_po_item_delivery_date_for_sap(_norm(block.get("delivery_date")))
    if item_delivery:
        row["DeliveryDate"] = item_delivery
    return row


def _service_rows_for_block(
    *,
    po_number: str,
    po_item: str,
    block: dict[str, Any],
    header: dict[str, Any],
    service_is_deleted: bool = False,
    acct_deleted_ccs: frozenset[str] | None = None,
    single_cc_mode: bool = False,
) -> list[dict[str, Any]]:
    allocs = [
        a for a in (block.get("allocations") or []) if isinstance(a, dict) and _norm(a.get("cost_center"))
    ]
    return [
        _po_service_row(
            po_number=po_number,
            item_number=po_item,
            block=block,
            header=header,
            allocs=allocs or [{}],
            service_is_deleted=service_is_deleted,
            acct_deleted_ccs=acct_deleted_ccs,
            single_cc_mode=single_cc_mode,
        )
    ]


def _service_rows_for_cc_delete(
    *,
    po_number: str,
    po_item: str,
    sap_block: dict[str, Any],
    sub_block: dict[str, Any],
    header: dict[str, Any],
) -> tuple[list[dict[str, Any]], frozenset[str], list[dict[str, Any]]]:
    """One POST per service: deleted CC rows + kept rows (SAP PO doc)."""
    sap_allocs = _alloc_rows_resolved(sap_block)
    sub_keys = {cc for cc, _, _ in _alloc_rows_resolved(sub_block)}
    removed = frozenset(cc for cc, _, _ in sap_allocs if cc not in sub_keys)
    if not removed:
        return [], removed, []

    sub_by_cc = {cc: (qty, acct) for cc, qty, acct in _alloc_rows_resolved(sub_block)}
    sap_total_qty = sum(
        float(qty.replace(",", ".")) for _, qty, _ in sap_allocs if qty
    )
    mutation_allocs: list[dict[str, Any]] = []
    for cc, qty, acct in sap_allocs:
        if cc in removed:
            mutation_allocs.append(
                {"cost_center": cc, "qty": qty, "po_acct_assgmt_number": acct}
            )
        elif cc in sub_by_cc:
            sqty, sacct = sub_by_cc[cc]
            if len(sub_keys) == 1:
                try:
                    kept_f = float(str(sqty).replace(",", "."))
                except ValueError:
                    kept_f = 0.0
                if kept_f < sap_total_qty:
                    sqty = (
                        str(int(sap_total_qty))
                        if abs(sap_total_qty - round(sap_total_qty)) < 1e-9
                        else f"{sap_total_qty:.3f}".rstrip("0").rstrip(".")
                    )
            mutation_allocs.append(
                {
                    "cost_center": cc,
                    "qty": sqty,
                    "po_acct_assgmt_number": sacct or acct,
                }
            )

    single_cc = len(sub_keys) == 1
    block = copy.deepcopy(sub_block)
    block["allocations"] = mutation_allocs
    rows = _service_rows_for_block(
        po_number=po_number,
        po_item=po_item,
        block=block,
        header=header,
        acct_deleted_ccs=removed,
        single_cc_mode=single_cc,
    )
    if rows and mutation_allocs:
        kept_allocs = [a for a in mutation_allocs if a.get("cost_center") not in removed]
        qty_total = _item_total_quantity(kept_allocs)
        unit = _unit_price_from_block(sub_block)
        line_total = _format_po_z_price(
            unit * qty_total if unit > 0 else float(_line_total_amount(sub_block, qty_total=qty_total))
        )
        rows[0]["ConfirmedQuantity"] = _format_z_qty(qty_total)
        rows[0]["NetPriceAmount"] = line_total
        if unit > 0:
            rows[0]["NetAmount"] = _format_po_z_price(unit)
        accts = rows[0].get(PO_SERVICE_ACCT_NAV) or []
        if isinstance(accts, list):
            accts.sort(key=lambda r: bool(r.get("IsDeleted")))
            for acct in accts:
                if acct.get("IsDeleted"):
                    continue
                try:
                    qf = float(str(acct.get("Quantity") or "0").replace(",", "."))
                except ValueError:
                    qf = qty_total
                acct["PurgDocNetAmount"] = _alloc_purg_amount(unit=unit, alloc_qty=qf)
    return rows, removed, mutation_allocs


def _wrap_mutation(
    *,
    po_number: str,
    header: dict[str, Any],
    items: list[dict[str, Any]],
    parent_pr_number: str | None,
    attach_header: bool = False,
) -> dict[str, Any]:
    pur_org = _norm(header.get("purchasing_org"))
    supplier = _fixed_supplier(header)
    inner: dict[str, Any] = {
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderType": "YSER",
        "CompanyCode": _company_code(header, pur_org),
        "PurchasingGroup": _norm(header.get("purchasing_group")),
        "PurchasingOrganization": pur_org,
        "Supplier": supplier,
        "to_PurchaseOrderItem": items,
    }
    pay_terms = _z_po_payment_terms(header)
    if pay_terms:
        inner["PaymentTerms"] = pay_terms
    if attach_header:
        apply_yser_po_texts_to_z_inner(inner, header)
    return _wrap_z_po_nav_lists(inner)


def _attach_header_note_to_posts(
    posts: list[dict[str, Any]],
    *,
    po_key: str,
    note_changed: bool,
    header: dict[str, Any],
    sub_idx: dict[str, _ServiceSnap],
    parent_pr_number: str | None,
) -> None:
    if not note_changed:
        return
    if not posts:
        if not sub_idx:
            return
        posts.append(
            _wrap_mutation(
                po_number=po_key,
                header=header,
                items=[
                    _header_only_stub_item(
                        po_key=po_key,
                        header=header,
                        sub_idx=sub_idx,
                        parent_pr_number=parent_pr_number,
                    )
                ],
                parent_pr_number=parent_pr_number,
                attach_header=True,
            )
        )
        return
    inner = posts[-1].get("d") if isinstance(posts[-1].get("d"), dict) else posts[-1]
    if isinstance(inner, dict):
        apply_yser_po_texts_to_z_inner(inner, header)


def _append_field_update_posts(
    posts: list[dict[str, Any]],
    *,
    po_key: str,
    header: dict[str, Any],
    sap_idx: dict[str, _ServiceSnap],
    sub_idx: dict[str, _ServiceSnap],
    parent_pr_number: str | None,
    skip_keys: frozenset[str] | None = None,
) -> None:
    skip = skip_keys or frozenset()
    for key, sub_snap in sub_idx.items():
        if key in skip:
            continue
        sap_snap = sap_idx.get(key)
        if sap_snap is None:
            continue
        if not _field_fields_changed(sap_snap.block, sub_snap.block, header=header):
            continue
        posts.append(
            _wrap_mutation(
                po_number=po_key,
                header=header,
                items=[
                    _po_item_shell(
                        po_number=po_key,
                        po_item=sub_snap.po_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=_service_rows_for_block(
                            po_number=po_key,
                            po_item=sub_snap.po_item,
                            block=sub_snap.block,
                            header=header,
                        ),
                        parent_pr_number=parent_pr_number,
                    )
                ],
                parent_pr_number=parent_pr_number,
            )
        )


def _header_only_stub_item(
    *,
    po_key: str,
    header: dict[str, Any],
    sub_idx: dict[str, _ServiceSnap],
    parent_pr_number: str | None,
) -> dict[str, Any]:
    first = next(iter(sub_idx.values()))
    return _po_item_shell(
        po_number=po_key,
        po_item=first.po_item,
        block=first.block,
        header=header,
        service_rows=_service_rows_for_block(
            po_number=po_key,
            po_item=first.po_item,
            block=first.block,
            header=header,
        ),
        parent_pr_number=parent_pr_number,
    )


def build_yser_po_update_posts(
    *,
    submitted: dict[str, Any],
    sap_form: dict[str, Any],
    po_number: str,
    sap_item_count: int,
    ticket_id: str | None,
    parent_pr_number: str | None = None,
    existing_items: list[SapZPoItemSnapshot] | None = None,
    min_ref_seq: int = 0,
    create_reconcile: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build one-or-more Z POST bodies (incremental when structure changes).

    Returns ``(posts, merged_form)`` for post-update verify.

    When ``create_reconcile`` is true (post-create SAP partial persist), only POST
    missing services onto their grouped PO items — no deletes or field mutations.
    """
    po_key = _norm(po_number)
    if not po_key:
        raise ValueError("PO number is required for YSER update")

    if create_reconcile:
        sub_work, sap_work = prepare_yser_po_create_reconcile_forms(
            submitted, sap_form=sap_form
        )
        working = copy.deepcopy(sub_work)
        merged = merge_yser_po_update_form_with_sap(working, sap_form=sap_work)
        _ensure_yser_po_service_refs(
            merged,
            ticket_id=ticket_id,
            min_ref_seq=max(
                min_ref_seq, yser_po_max_service_ref_seq_from_form(sap_work)
            ),
        )
        dup_err = validate_yser_po_service_refs_unique(merged)
        if dup_err:
            raise ValueError(dup_err)

        header = merged.get("header") if isinstance(merged.get("header"), dict) else {}
        sap_idx = _index_services(sap_work)
        sub_idx = _index_services(merged)
        posts: list[dict[str, Any]] = []

        active_sap_items = {
            format_z_po_item_number(s.item_number)
            for s in (existing_items or [])
            if s.item_number and not s.is_deleted
        }
        desired_po_items = {snap.po_item for snap in sub_idx.values()}
        new_po_items = (
            desired_po_items - active_sap_items if active_sap_items else set()
        )
        missing_blocks_by_item: dict[str, list[dict[str, Any]]] = {}

        for key, sub_snap in sub_idx.items():
            if key in sap_idx:
                continue
            missing_blocks_by_item.setdefault(sub_snap.po_item, []).append(sub_snap.block)
            if sub_snap.po_item in new_po_items:
                continue
            rows = _service_rows_for_block(
                po_number=po_key,
                po_item=sub_snap.po_item,
                block=sub_snap.block,
                header=header,
            )
            posts.append(
                _wrap_mutation(
                    po_number=po_key,
                    header=header,
                    items=[
                        _po_item_shell(
                            po_number=po_key,
                            po_item=sub_snap.po_item,
                            block=sub_snap.block,
                            header=header,
                            service_rows=rows,
                            parent_pr_number=parent_pr_number,
                        )
                    ],
                    parent_pr_number=parent_pr_number,
                )
            )

        for po_item in sorted(new_po_items):
            blocks = missing_blocks_by_item.get(po_item) or []
            if not blocks:
                continue
            posts.append(
                _wrap_mutation(
                    po_number=po_key,
                    header=header,
                    items=[
                        _build_z_po_item_row(
                            blocks=blocks,
                            header=header,
                            item_number=po_item,
                            po_number=po_key,
                            parent_pr_number=parent_pr_number,
                            tax_code=yser_po_group_item_tax_code(blocks, header),
                            ticket_id=ticket_id,
                        )
                    ],
                    parent_pr_number=parent_pr_number,
                )
            )

        if not posts:
            return [], merged
        return posts, merged

    working = copy.deepcopy(submitted)
    merged = merge_yser_po_update_form_with_sap(working, sap_form=sap_form)
    _ensure_yser_po_service_refs(
        merged, ticket_id=ticket_id, min_ref_seq=max(min_ref_seq, yser_po_max_service_ref_seq_from_form(sap_form))
    )
    dup_err = validate_yser_po_service_refs_unique(merged)
    if dup_err:
        raise ValueError(dup_err)

    header = merged.get("header") if isinstance(merged.get("header"), dict) else {}
    sap_header = sap_form.get("header") if isinstance(sap_form.get("header"), dict) else {}
    note_changed = _norm(sap_header.get("header_note")) != _norm(header.get("header_note"))

    sap_idx = _index_services(sap_form)
    sub_idx = _index_services(merged)
    posts: list[dict[str, Any]] = []
    cc_mutated_keys: set[str] = set()

    active_sap_items = {
        format_z_po_item_number(s.item_number)
        for s in (existing_items or [])
        if s.item_number and not s.is_deleted
    }
    desired_po_items = {snap.po_item for snap in sub_idx.values()}

    if not yser_po_has_structural_delta(sap_form=sap_form, submitted=merged):
        _append_field_update_posts(
            posts,
            po_key=po_key,
            header=header,
            sap_idx=sap_idx,
            sub_idx=sub_idx,
            parent_pr_number=parent_pr_number,
        )
        if not posts and note_changed:
            if not sub_idx:
                raise ValueError("No YSER PO line changes to submit to SAP")
            posts.append(
                _wrap_mutation(
                    po_number=po_key,
                    header=header,
                    items=[
                        _header_only_stub_item(
                            po_key=po_key,
                            header=header,
                            sub_idx=sub_idx,
                            parent_pr_number=parent_pr_number,
                        )
                    ],
                    parent_pr_number=parent_pr_number,
                    attach_header=True,
                )
            )
        else:
            _attach_header_note_to_posts(
                posts,
                po_key=po_key,
                note_changed=note_changed,
                header=header,
                sub_idx=sub_idx,
                parent_pr_number=parent_pr_number,
            )
        return posts, merged

    # 1) CC deletes
    for key, sap_snap in sap_idx.items():
        sub_snap = sub_idx.get(key)
        if sub_snap is None:
            continue
        rows, removed, mutation_allocs = _service_rows_for_cc_delete(
            po_number=po_key,
            po_item=sap_snap.po_item,
            sap_block=sap_snap.block,
            sub_block=sub_snap.block,
            header=header,
        )
        if not removed:
            continue
        cc_mutated_keys.add(key)
        _apply_cc_delete_to_merged_line(merged, sub_snap.block, mutation_allocs, removed)
        posts.append(
            _wrap_mutation(
                po_number=po_key,
                header=header,
                items=[
                    _po_item_shell(
                        po_number=po_key,
                        po_item=sap_snap.po_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=rows,
                        parent_pr_number=parent_pr_number,
                    )
                ],
                parent_pr_number=parent_pr_number,
            )
        )

    # 2) Service deletes — deleted-only rows (do not re-post kept siblings)
    deleted_keys = {key for key in sap_idx if key not in sub_idx}
    for key in deleted_keys:
        sap_snap = sap_idx[key]
        rows = _service_rows_for_block(
            po_number=po_key,
            po_item=sap_snap.po_item,
            block=sap_snap.block,
            header=header,
            service_is_deleted=True,
        )
        posts.append(
            _wrap_mutation(
                po_number=po_key,
                header=header,
                items=[
                    _po_item_shell(
                        po_number=po_key,
                        po_item=sap_snap.po_item,
                        block=sap_snap.block,
                        header=header,
                        service_rows=rows,
                        parent_pr_number=parent_pr_number,
                    )
                ],
                parent_pr_number=parent_pr_number,
            )
        )

    # 3) Whole PO item deletes (legacy — no services left on item)
    if existing_items:
        for sap_no in sorted(active_sap_items - desired_po_items):
            snap = next(
                (s for s in existing_items if format_z_po_item_number(s.item_number) == sap_no and not s.is_deleted),
                None,
            )
            if snap is None:
                continue
            posts.append(
                _wrap_mutation(
                    po_number=po_key,
                    header=header,
                    items=[
                        _build_z_po_delete_item_stub(
                            snap=snap,
                            header=header,
                            po_number=po_key,
                            tax_code=_norm(snap.tax_code) or line_tax_code({}, header),
                        )
                    ],
                    parent_pr_number=parent_pr_number,
                )
            )

    # 4) New services on existing PO items
    new_po_items = desired_po_items - active_sap_items if active_sap_items else set()
    for key, sub_snap in sub_idx.items():
        if key in sap_idx:
            continue
        if sub_snap.po_item in new_po_items:
            continue
        rows = _service_rows_for_block(
            po_number=po_key,
            po_item=sub_snap.po_item,
            block=sub_snap.block,
            header=header,
        )
        posts.append(
            _wrap_mutation(
                po_number=po_key,
                header=header,
                items=[
                    _po_item_shell(
                        po_number=po_key,
                        po_item=sub_snap.po_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=rows,
                        parent_pr_number=parent_pr_number,
                    )
                ],
                parent_pr_number=parent_pr_number,
            )
        )

    # 5) CC adds on existing services
    for key, sub_snap in sub_idx.items():
        if key in cc_mutated_keys:
            continue
        sap_snap = sap_idx.get(key)
        if sap_snap is None:
            continue
        sap_allocs = {cc for cc, _ in _alloc_rows(sap_snap.block)}
        sub_allocs = {cc for cc, _ in _alloc_rows(sub_snap.block)}
        added = sub_allocs - sap_allocs
        if not added:
            continue
        for cc in sorted(added):
            block = copy.deepcopy(sub_snap.block)
            allocs = [
                a
                for a in (block.get("allocations") or [])
                if isinstance(a, dict) and _norm(a.get("cost_center")) == cc
            ]
            block["allocations"] = allocs
            rows = _service_rows_for_block(
                po_number=po_key,
                po_item=sub_snap.po_item,
                block=block,
                header=header,
            )
            posts.append(
                _wrap_mutation(
                    po_number=po_key,
                    header=header,
                    items=[
                        _po_item_shell(
                            po_number=po_key,
                            po_item=sub_snap.po_item,
                            block=block,
                            header=header,
                            service_rows=rows,
                            parent_pr_number=parent_pr_number,
                        )
                    ],
                    parent_pr_number=parent_pr_number,
                )
            )

    # 6) Field updates
    _append_field_update_posts(
        posts,
        po_key=po_key,
        header=header,
        sap_idx=sap_idx,
        sub_idx=sub_idx,
        parent_pr_number=parent_pr_number,
        skip_keys=frozenset(cc_mutated_keys),
    )
    _attach_header_note_to_posts(
        posts,
        po_key=po_key,
        note_changed=note_changed,
        header=header,
        sub_idx=sub_idx,
        parent_pr_number=parent_pr_number,
    )

    if not posts:
        raise ValueError("No YSER PO line changes to submit to SAP")
    return posts, merged


def _apply_cc_delete_to_merged_line(
    merged: dict[str, Any],
    sub_block: dict[str, Any],
    mutation_allocs: list[dict[str, Any]],
    removed: frozenset[str],
) -> None:
    ref = _yser_po_service_external_ref(sub_block)
    kept = [
        {"cost_center": a["cost_center"], "qty": a["qty"], **(
            {"po_acct_assgmt_number": a["po_acct_assgmt_number"]}
            if a.get("po_acct_assgmt_number")
            else {}
        )}
        for a in mutation_allocs
        if a.get("cost_center") not in removed
    ]
    for line in yser_effective_line_blocks(merged):
        if ref and _yser_po_service_external_ref(line) == ref:
            line["allocations"] = kept
            return
        if not ref and normalize_service_performer_code(_norm(line.get("service"))) == normalize_service_performer_code(
            _norm(sub_block.get("service"))
        ):
            line["allocations"] = kept
            return
