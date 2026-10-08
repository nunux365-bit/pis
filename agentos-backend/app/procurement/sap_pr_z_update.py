"""YSER PR incremental Z update (Jul 2026 SAP API — one mutation per POST)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.procurement.line_catalog_group import line_catalog_group
from app.procurement.sap_pr_z_payload import (
    _alloc_pr_acct_assgmt_number,
    _ensure_yser_pr_service_refs,
    _fixed_vendor,
    _inline_service_rows,
    _item_total_quantity,
    _yser_pr_service_external_ref,
    _z_delivery_date,
    _z_pr_item_key_for_block,
    finalize_z_yser_update_post_body,
    format_z_pr_item_number,
    merge_yser_update_form_with_sap,
    normalize_pr_acct_assgmt_number,
    normalize_service_performer_code,
    storage_location_for_sap,
    yser_assign_update_item_numbers,
    yser_effective_line_blocks,
    yser_form_item_numbers,
    yser_pr_item_number_for_block,
    yser_sap_item_numbers,
    validate_yser_form_service_refs_unique,
    Z_ACCOUNT_ASSIGNMENT_CAT,
    Z_ITEM_CATEGORY_SERVICE,
    Z_ITEM_HEADER_QUANTITY,
)
from app.procurement.sap_odata_utils import odata_norm as _norm


@dataclass(frozen=True)
class _ServiceSnap:
    key: str
    ext_ref: str
    pr_item: str
    block: dict[str, Any]

    @property
    def service(self) -> str:
        return normalize_service_performer_code(_norm(self.block.get("service")))


def _service_key(block: dict[str, Any], *, line_index: int) -> str:
    ref = _yser_pr_service_external_ref(block)
    if ref:
        return f"ref:{ref}"
    item = normalize_pr_item_number(
        _norm(block.get("sap_pr_item") or block.get("purchase_requisition_item"))
        or yser_pr_item_number_for_block(block, line_index=line_index)
    )
    svc = normalize_service_performer_code(_norm(block.get("service")))
    acct = normalize_pr_acct_assgmt_number(_norm(block.get("pr_acct_assgmt_number")))
    return f"legacy:{item}:{svc}:{acct}"


def normalize_pr_item_number(raw: str) -> str:
    from app.procurement.sap_pr_payload import normalize_pr_item_number as _nip

    return _nip(raw)


def _index_services(form: dict[str, Any]) -> dict[str, _ServiceSnap]:
    out: dict[str, _ServiceSnap] = {}
    for idx, block in enumerate(yser_effective_line_blocks(form)):
        key = _service_key(block, line_index=idx)
        pr_item = _z_pr_item_key_for_block(block, line_index=idx)
        out[key] = _ServiceSnap(
            key=key,
            ext_ref=_yser_pr_service_external_ref(block),
            pr_item=pr_item,
            block=block,
        )
    return out


def _alloc_rows(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(cost_center, qty, pr_acct_assgmt_number) per allocation."""
    rows: list[tuple[str, str, str]] = []
    for alloc in block.get("allocations") or []:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        acct = normalize_pr_acct_assgmt_number(_norm(alloc.get("pr_acct_assgmt_number")))
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qty_f = float(qty_raw.replace(",", "."))
            qty = str(int(qty_f)) if abs(qty_f - round(qty_f)) < 1e-9 else f"{qty_f:.3f}".rstrip("0").rstrip(".")
        except ValueError:
            qty = qty_raw
        rows.append((cc, qty, acct))
    return sorted(rows)


def _alloc_rows_resolved(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``_alloc_rows`` with fallback ``PRAcctAssgmtNumber`` (matches ``_inline_service_rows``)."""
    rows: list[tuple[str, str, str]] = []
    seq = 1
    for alloc in block.get("allocations") or []:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        acct, seq = _alloc_pr_acct_assgmt_number(alloc, fallback_seq=seq)
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qty_f = float(qty_raw.replace(",", "."))
            qty = str(int(qty_f)) if abs(qty_f - round(qty_f)) < 1e-9 else f"{qty_f:.3f}".rstrip("0").rstrip(".")
        except ValueError:
            qty = qty_raw
        rows.append((cc, qty, acct))
    return sorted(rows)


def _field_fields_changed(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for field in ("short_text", "delivery_date", "order_unit"):
        if _norm(a.get(field)) != _norm(b.get(field)):
            return True
    # Prefer unit/valuation (UI + Z update contract). ``gross_price`` is often a
    # hydrated line-total mirror; comparing it after defaults can false-positive.
    for field in ("unit_price", "valuation_price"):
        if _norm_price(a.get(field)) != _norm_price(b.get(field)):
            return True
    if not _norm(a.get("valuation_price")) and not _norm(b.get("valuation_price")):
        if _norm_price(a.get("gross_price")) != _norm_price(b.get("gross_price")):
            return True
    if _alloc_rows(a) != _alloc_rows(b):
        return True
    if normalize_service_performer_code(_norm(a.get("service"))) != normalize_service_performer_code(
        _norm(b.get("service"))
    ):
        return True
    return False


def _norm_price(raw: Any) -> str:
    s = _norm(raw)
    if not s:
        return ""
    try:
        return f"{float(s.replace(',', '.')):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def yser_has_structural_delta(*, sap_form: dict[str, Any], submitted: dict[str, Any]) -> bool:
    sap_idx = _index_services(sap_form)
    sub_idx = _index_services(submitted)
    if set(sap_idx) != set(sub_idx):
        return True
    for key in sap_idx:
        if _alloc_rows(sap_idx[key].block) != _alloc_rows(sub_idx[key].block):
            return True
    return False


def _item_shell(
    *,
    pr_number: str,
    pr_item: str,
    block: dict[str, Any],
    header: dict[str, Any],
    service_rows: list[dict[str, Any]],
    header_note: str,
) -> dict[str, Any]:
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(_norm(header.get("storage_location")), plant=plant)
    mat_grp = line_catalog_group(block, header, "YSER")
    first_cc = ""
    for row in service_rows:
        cc = _norm(row.get("CostCenter"))
        if cc:
            first_cc = cc
            break
    return {
        "PRItem": pr_item,
        "PRNumber": pr_number,
        "PurOrg": _norm(header.get("purchasing_org")),
        "PurGroup": _norm(header.get("purchasing_group")),
        "ActAssignmentCat": Z_ACCOUNT_ASSIGNMENT_CAT,
        "ItemCat": Z_ITEM_CATEGORY_SERVICE,
        "Material": "",
        "ShortText": _norm(block.get("short_text")) or mat_grp,
        "HeaderNote": header_note[:40] if header_note else "",
        "Plant": plant,
        "MaterialGroup": mat_grp,
        "DeliveryDate": _z_delivery_date(_norm(block.get("delivery_date"))),
        "SLoc": sloc,
        "ServiceNo": "",
        "Quantity": Z_ITEM_HEADER_QUANTITY,
        "CostCenter": first_cc,
        "Asset": "",
        "FixedVendor": _fixed_vendor(header),
        "ValuationPrice": "",
        "to_Services": service_rows,
    }


def _service_rows_for_block(
    *,
    pr_number: str,
    pr_item: str,
    block: dict[str, Any],
    for_update: bool,
    service_is_deleted: bool = False,
    acct_delete_keys: frozenset[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    allocs = [
        a for a in (block.get("allocations") or []) if isinstance(a, dict) and _norm(a.get("cost_center"))
    ]
    service_no = normalize_service_performer_code(_norm(block.get("service")))
    ext_ref = _yser_pr_service_external_ref(block)
    rows, _ = _inline_service_rows(
        pr_key=pr_number if for_update else "",
        pr_item=pr_item,
        service_no=service_no,
        short_text="",
        block=block,
        allocs=allocs or [{}],
        next_acct_seq=1,
        grand_total_qty=_item_total_quantity(allocs) if allocs else 1.0,
        for_update=for_update,
        use_absolute_distr=True,
        ext_ref=ext_ref,
        service_is_deleted=service_is_deleted,
        acct_delete_keys=acct_delete_keys,
        omit_service_short_text=for_update,
    )
    return rows


def _service_rows_for_cc_delete(
    *,
    pr_number: str,
    pr_item: str,
    sap_block: dict[str, Any],
    sub_block: dict[str, Any],
) -> tuple[list[dict[str, Any]], frozenset[tuple[str, str]]]:
    """One POST per service: deleted CC rows + kept rows (SAP doc § CC delete)."""
    sap_allocs = _alloc_rows_resolved(sap_block)
    sub_keys = {(cc, acct) for cc, _, acct in _alloc_rows_resolved(sub_block)}
    removed = frozenset(
        (cc, acct) for cc, _, acct in sap_allocs if (cc, acct) not in sub_keys
    )
    if not removed:
        return [], removed

    sub_by_key = { (cc, acct): (qty, acct) for cc, qty, acct in _alloc_rows(sub_block) }
    mutation_allocs: list[dict[str, Any]] = []
    for cc, qty, acct in sap_allocs:
        if (cc, acct) in removed:
            mutation_allocs.append(
                {"cost_center": cc, "qty": qty, "pr_acct_assgmt_number": acct}
            )
        elif (cc, acct) in sub_by_key:
            sqty, _ = sub_by_key[(cc, acct)]
            mutation_allocs.append(
                {"cost_center": cc, "qty": sqty, "pr_acct_assgmt_number": acct}
            )

    rows, _ = _inline_service_rows(
        pr_key=pr_number,
        pr_item=pr_item,
        service_no=normalize_service_performer_code(_norm(sap_block.get("service"))),
        short_text="",
        block=sub_block,
        allocs=mutation_allocs,
        next_acct_seq=1,
        grand_total_qty=_item_total_quantity(mutation_allocs),
        for_update=True,
        use_absolute_distr=True,
        ext_ref=_yser_pr_service_external_ref(sap_block),
        acct_delete_keys=removed,
        omit_service_short_text=True,
    )
    return rows, removed


def _wrap_mutation(
    *,
    pr_number: str,
    items: list[dict[str, Any]],
    header_note: str | None = None,
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "PRNumber": pr_number,
        "DocumentType": "YSER",
        "to_Items": items,
    }
    if header_note is not None:
        inner["PurReqnDescription"] = header_note[:40]
    return finalize_z_yser_update_post_body(inner)


def _append_field_update_posts(
    posts: list[dict[str, Any]],
    *,
    pr_key: str,
    header: dict[str, Any],
    sap_idx: dict[str, _ServiceSnap],
    sub_idx: dict[str, _ServiceSnap],
    skip_keys: frozenset[str] | None = None,
) -> None:
    """One POST per changed service (never bundle multi/single-CC siblings)."""
    skip = skip_keys or frozenset()
    for key, sub_snap in sub_idx.items():
        if key in skip:
            continue
        sap_snap = sap_idx.get(key)
        if sap_snap is None:
            continue
        if not _field_fields_changed(sap_snap.block, sub_snap.block):
            continue
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[
                    _item_shell(
                        pr_number=pr_key,
                        pr_item=sub_snap.pr_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=_service_rows_for_block(
                            pr_number=pr_key,
                            pr_item=sub_snap.pr_item,
                            block=sub_snap.block,
                            for_update=True,
                        ),
                        header_note="",
                    )
                ],
            )
        )


def _attach_header_note_to_posts(
    posts: list[dict[str, Any]],
    *,
    pr_key: str,
    note_changed: bool,
    header_note: str,
) -> None:
    if not note_changed:
        return
    if not posts:
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[],
                header_note=header_note,
            )
        )
    else:
        posts[-1] = dict(posts[-1])
        posts[-1]["PurReqnDescription"] = header_note[:40]


def build_yser_pr_update_posts(
    *,
    submitted: dict[str, Any],
    sap_form: dict[str, Any],
    pr_number: str,
    sap_item_count: int,
    ticket_id: str | None,
    min_ref_seq: int = 0,
) -> list[dict[str, Any]]:
    """Build one-or-more Z POST bodies (incremental when structure changes)."""
    pr_key = _norm(pr_number)
    if not pr_key:
        raise ValueError("PR number is required for YSER update")

    working = copy.deepcopy(submitted)
    merged = merge_yser_update_form_with_sap(working, sap_form=sap_form)
    _ensure_yser_pr_service_refs(
        merged, ticket_id=ticket_id, only_without_item=True, min_ref_seq=min_ref_seq
    )
    yser_assign_update_item_numbers(
        merged, sap_form=sap_form, sap_item_count=sap_item_count
    )
    dup_err = validate_yser_form_service_refs_unique(merged)
    if dup_err:
        raise ValueError(dup_err)
    header = merged.get("header") if isinstance(merged.get("header"), dict) else {}
    header_note = _norm(header.get("header_note"))

    sap_header = sap_form.get("header") if isinstance(sap_form.get("header"), dict) else {}
    note_changed = _norm(sap_header.get("header_note")) != header_note

    sap_idx = _index_services(sap_form)
    sub_idx = _index_services(merged)
    posts: list[dict[str, Any]] = []
    cc_mutated_keys: set[str] = set()

    if not yser_has_structural_delta(sap_form=sap_form, submitted=merged):
        _append_field_update_posts(
            posts,
            pr_key=pr_key,
            header=header,
            sap_idx=sap_idx,
            sub_idx=sub_idx,
        )
        _attach_header_note_to_posts(
            posts,
            pr_key=pr_key,
            note_changed=note_changed,
            header_note=header_note,
        )
        return posts

    # 1) CC deletes — one POST per service with deleted + kept rows (SAP doc)
    for key, sap_snap in sap_idx.items():
        sub_snap = sub_idx.get(key)
        if sub_snap is None:
            continue
        rows, removed = _service_rows_for_cc_delete(
            pr_number=pr_key,
            pr_item=sap_snap.pr_item,
            sap_block=sap_snap.block,
            sub_block=sub_snap.block,
        )
        if not removed:
            continue
        cc_mutated_keys.add(key)
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[
                    _item_shell(
                        pr_number=pr_key,
                        pr_item=sap_snap.pr_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=rows,
                        header_note="",
                    )
                ],
            )
        )

    # 2) Service deletes. Default: deleted-only rows (IsDeleted=X). Do NOT re-post
    # kept services — that leaves the PR unable to accept later item IsDeleted (SE/601).
    # Exception: deleting a single-CC service while a sibling remains on the same
    # PRItem must use IsAcctAssgmtDeleted (CC-delete path); IsDeleted=X hits SAP
    # qty-sum / 06-411 on grouped items.
    deleted_keys = {key for key in sap_idx if key not in sub_idx}
    deleted_by_item: dict[str, list[str]] = {}
    for key in deleted_keys:
        deleted_by_item.setdefault(sap_idx[key].pr_item, []).append(key)

    def _kept_siblings_on_item(item_no: str) -> bool:
        return any(snap.pr_item == item_no for snap in sub_idx.values())

    def _kept_multi_cc_sibling_on_item(item_no: str) -> bool:
        return any(
            snap.pr_item == item_no and len(_alloc_rows(snap.block)) > 1
            for snap in sub_idx.values()
        )

    for pr_item, keys in deleted_by_item.items():
        service_rows: list[dict[str, Any]] = []
        shell_block: dict[str, Any] | None = None
        for key in keys:
            sap_snap = sap_idx[key]
            if (
                len(_alloc_rows(sap_snap.block)) == 1
                and _kept_siblings_on_item(pr_item)
                and _kept_multi_cc_sibling_on_item(pr_item)
            ):
                rows = _service_rows_for_block(
                    pr_number=pr_key,
                    pr_item=pr_item,
                    block=sap_snap.block,
                    for_update=True,
                )
                for row in rows:
                    row["IsAcctAssgmtDeleted"] = "X"
                if not rows:
                    continue
                posts.append(
                    _wrap_mutation(
                        pr_number=pr_key,
                        items=[
                            _item_shell(
                                pr_number=pr_key,
                                pr_item=pr_item,
                                block=sap_snap.block,
                                header=header,
                                service_rows=rows,
                                header_note="",
                            )
                        ],
                    )
                )
                continue
            service_rows.extend(
                _service_rows_for_block(
                    pr_number=pr_key,
                    pr_item=pr_item,
                    block=sap_snap.block,
                    for_update=True,
                    service_is_deleted=True,
                )
            )
            shell_block = shell_block or sap_snap.block
        if not service_rows or shell_block is None:
            continue
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[
                    _item_shell(
                        pr_number=pr_key,
                        pr_item=pr_item,
                        block=shell_block,
                        header=header,
                        service_rows=service_rows,
                        header_note="",
                    )
                ],
            )
        )

    # 3) New line items (PRItem not yet on SAP)
    sap_items = yser_sap_item_numbers(sap_form)
    sub_items = yser_form_item_numbers(merged)
    new_items = sorted(sub_items - sap_items, key=lambda x: int(x))
    blocks_by_item: dict[str, list[dict[str, Any]]] = {}
    for idx, block in enumerate(yser_effective_line_blocks(merged)):
        item_no = yser_pr_item_number_for_block(block, line_index=idx)
        if item_no in new_items:
            blocks_by_item.setdefault(item_no, []).append(block)

    for item_no in new_items:
        group_blocks = blocks_by_item.get(item_no) or []
        if not group_blocks:
            continue
        pr_item = format_z_pr_item_number(int(item_no))
        service_rows: list[dict[str, Any]] = []
        for block in group_blocks:
            service_rows.extend(
                _service_rows_for_block(
                    pr_number=pr_key,
                    pr_item=pr_item,
                    block=block,
                    for_update=True,
                )
            )
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[
                    _item_shell(
                        pr_number=pr_key,
                        pr_item=pr_item,
                        block=group_blocks[0],
                        header=header,
                        service_rows=service_rows,
                        header_note="",
                    )
                ],
            )
        )

    # 4) New services on existing items (new ext ref, same PRItem — SAP doc)
    for key, sub_snap in sub_idx.items():
        if key in sap_idx:
            continue
        if normalize_pr_item_number(sub_snap.pr_item) in new_items:
            continue
        rows = _service_rows_for_block(
            pr_number=pr_key,
            pr_item=sub_snap.pr_item,
            block=sub_snap.block,
            for_update=True,
        )
        posts.append(
            _wrap_mutation(
                pr_number=pr_key,
                items=[
                    _item_shell(
                        pr_number=pr_key,
                        pr_item=sub_snap.pr_item,
                        block=sub_snap.block,
                        header=header,
                        service_rows=rows,
                        header_note="",
                    )
                ],
            )
        )

    # 5) CC adds on existing services
    for key, sub_snap in sub_idx.items():
        if key in cc_mutated_keys:
            continue
        sap_snap = sap_idx.get(key)
        if sap_snap is None:
            continue
        sap_allocs = {(cc, acct) for cc, _, acct in _alloc_rows(sap_snap.block)}
        sub_allocs = {(cc, acct) for cc, _, acct in _alloc_rows(sub_snap.block)}
        added = sub_allocs - sap_allocs
        if not added:
            continue
        for cc, acct in sorted(added):
            block = copy.deepcopy(sub_snap.block)
            allocs = [
                a
                for a in (block.get("allocations") or [])
                if isinstance(a, dict) and _norm(a.get("cost_center")) == cc
            ]
            block["allocations"] = allocs
            rows = _service_rows_for_block(
                pr_number=pr_key,
                pr_item=sub_snap.pr_item,
                block=block,
                for_update=True,
            )
            posts.append(
                _wrap_mutation(
                    pr_number=pr_key,
                    items=[
                        _item_shell(
                            pr_number=pr_key,
                            pr_item=sub_snap.pr_item,
                            block=block,
                            header=header,
                            service_rows=rows,
                            header_note="",
                        )
                    ],
                )
            )

    # 6) Field updates on unchanged structure keys.
    _append_field_update_posts(
        posts,
        pr_key=pr_key,
        header=header,
        sap_idx=sap_idx,
        sub_idx=sub_idx,
        skip_keys=frozenset(cc_mutated_keys),
    )
    _attach_header_note_to_posts(
        posts,
        pr_key=pr_key,
        note_changed=note_changed,
        header_note=header_note,
    )

    return posts
