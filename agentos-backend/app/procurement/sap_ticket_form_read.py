"""Build AgentOS ticket ``form`` from SAP OData GET (PR/PO) for display and edit."""

from __future__ import annotations

import copy
from typing import Any

from app.procurement.line_catalog_group import catalog_group_field, sync_header_catalog_group
from app.procurement.sap_odata_utils import (
    is_sap_deleted_flag,
    odata_date_to_form,
    odata_entity_properties,
    odata_norm,
    odata_results_list,
    odata_text,
)
from app.procurement.sap_po_client import get_po
from app.procurement.sap_po_payload import (
    PO_ACCT_CREATE_NAV,
    PO_ACCT_NAV,
    PO_SCHEDULE_NAV,
    _po_item_deleted,
)
from app.procurement.sap_pr_client import get_pr
from app.procurement.sap_pr_z_payload import form_from_z_pr_read
from app.procurement.sap_pr_z_client import get_yser_pr
from app.procurement.sap_yast_acct import allocations_from_sap_acct_node

SAP_NO_ACTIVE_LINES_PR = "SAP PR has no active line items"
SAP_NO_ACTIVE_LINES_PO = "SAP PO has no active line items"


def is_sap_no_active_lines_error(err: str | None) -> bool:
    e = (err or "").strip()
    return e in (SAP_NO_ACTIVE_LINES_PR, SAP_NO_ACTIVE_LINES_PO)


def _delivery_from_po_schedule_node(node: Any) -> str:
    for entry in odata_results_list(node):
        props = odata_entity_properties(entry)
        d = odata_date_to_form(props.get("ScheduleLineDeliveryDate"))
        if d:
            return d
    return ""


def _allocation_qty_total_from_allocs(allocs: list[Any]) -> float:
    total = 0.0
    for alloc in allocs:
        if not isinstance(alloc, dict):
            continue
        q = odata_norm(alloc.get("qty"))
        if not q:
            continue
        try:
            total += float(q.replace(",", "."))
        except ValueError:
            pass
    return total


def _format_form_price(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{round(value, 2):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _material_pr_prices_from_sap(
    unit_price_raw: str, allocs: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """SAP ``PurchaseRequisitionPrice`` is per unit; UI keeps unit + valuation (line total)."""
    unit_s = odata_norm(unit_price_raw)
    if not unit_s:
        return "", "", ""
    qty = _allocation_qty_total_from_allocs(allocs)
    if qty <= 0:
        qty = 1.0
    try:
        unit = float(unit_s.replace(",", "."))
    except ValueError:
        return unit_s, unit_s, unit_s
    line_total = unit * qty
    val_out = _format_form_price(line_total)
    return unit_s, val_out, unit_s


def _seed_lines(form: dict[str, Any] | None) -> list[dict[str, Any]]:
    if isinstance(form, dict) and isinstance(form.get("lines"), list):
        return [copy.deepcopy(x) for x in form["lines"] if isinstance(x, dict)]
    return []


def _resolve_pr_seed_line(
    line: dict[str, Any],
    *,
    document_type: str,
    by_ref: dict[str, dict[str, Any]],
    by_item_service: dict[tuple[str, str], dict[str, Any]],
    by_item_unique: dict[str, dict[str, Any]],
    seed_lines: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match a hydrated PR line to its DB seed row (YSER: ref, then item+service)."""
    dt = (document_type or "").upper()
    ref = odata_norm(line.get("sap_pr_service_ref"))
    if dt == "YSER" and ref and ref in by_ref:
        return by_ref[ref]
    item = odata_norm(line.get("purchase_requisition_item"))
    if dt == "YSER" and item:
        svc = odata_norm(line.get("service"))
        if svc:
            seed_ln = by_item_service.get((item, svc))
            if seed_ln is not None:
                return seed_ln
    if item and item in by_item_unique:
        return by_item_unique[item]
    if len(seed_lines) == 1:
        return seed_lines[0]
    return None


def merge_pr_form_from_seed(
    form: dict[str, Any],
    *,
    document_type: str,
    seed_form: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill gaps from the last DB form when SAP read omits fields (e.g. YSER ``ServicePerformer``)."""
    if not seed_form:
        return form
    seed_h = _seed_header(seed_form)
    header = form.get("header")
    if isinstance(header, dict):
        for key, val in seed_h.items():
            if val and not odata_norm(header.get(key)):
                header[key] = val

    seed_lines = _seed_lines(seed_form)
    lines = form.get("lines")
    if not isinstance(lines, list) or not seed_lines:
        return form

    by_ref: dict[str, dict[str, Any]] = {}
    by_item_service: dict[tuple[str, str], dict[str, Any]] = {}
    by_item_buckets: dict[str, list[dict[str, Any]]] = {}
    for sl in seed_lines:
        item_no = odata_norm(sl.get("purchase_requisition_item"))
        if item_no:
            by_item_buckets.setdefault(item_no, []).append(sl)
        ref = odata_norm(sl.get("sap_pr_service_ref"))
        if ref:
            by_ref[ref] = sl
        svc = odata_norm(sl.get("service"))
        if item_no and svc:
            by_item_service[(item_no, svc)] = sl
    by_item_unique = {
        item: rows[0] for item, rows in by_item_buckets.items() if len(rows) == 1
    }

    dt = (document_type or "").upper()
    for line in lines:
        if not isinstance(line, dict):
            continue
        seed_ln = _resolve_pr_seed_line(
            line,
            document_type=document_type,
            by_ref=by_ref,
            by_item_service=by_item_service,
            by_item_unique=by_item_unique,
            seed_lines=seed_lines,
        )
        if not seed_ln:
            continue
        if dt == "YSER" and not odata_norm(line.get("service")):
            svc = odata_norm(seed_ln.get("service"))
            if svc:
                line["service"] = svc
        if not odata_norm(line.get("material")):
            mat = odata_norm(seed_ln.get("material"))
            if mat:
                line["material"] = mat
        allocs = line.get("allocations")
        seed_allocs = seed_ln.get("allocations")
        if isinstance(allocs, list) and isinstance(seed_allocs, list):
            has_cc = any(
                isinstance(a, dict) and odata_norm(a.get("cost_center"))
                for a in allocs
            )
            has_asset = any(
                isinstance(a, dict) and odata_norm(a.get("asset")) for a in allocs
            ) or odata_norm(line.get("asset"))
            seed_has_asset = any(
                isinstance(a, dict) and odata_norm(a.get("asset")) for a in seed_allocs
            ) or odata_norm(seed_ln.get("asset"))
            if dt == "YAST":
                # QA YAST never uses cost centres — only overlay seed when it has assets
                # and SAP returned neither asset nor usable splits.
                if not has_asset and seed_has_asset:
                    line["allocations"] = copy.deepcopy(seed_allocs)
            elif not has_cc and seed_allocs:
                line["allocations"] = copy.deepcopy(seed_allocs)
        if not odata_norm(line.get("short_text")):
            st = odata_norm(seed_ln.get("short_text"))
            if st:
                line["short_text"] = st
        if not odata_norm(line.get("order_unit")):
            uom = odata_norm(seed_ln.get("order_unit"))
            if uom:
                line["order_unit"] = uom
        if not odata_norm(line.get("delivery_date")):
            dd = odata_norm(seed_ln.get("delivery_date"))
            if dd:
                line["delivery_date"] = dd
        if not odata_norm(line.get("tax_code")):
            tc = odata_norm(seed_ln.get("tax_code"))
            if tc:
                line["tax_code"] = tc
    header = form.get("header")
    if isinstance(header, dict) and isinstance(lines, list):
        from app.procurement.line_tax_code import sync_header_tax_code

        sync_header_tax_code(header, lines)
    return form


def merge_po_form_from_seed(
    form: dict[str, Any],
    *,
    document_type: str,
    seed_form: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill PO form gaps from DB seed when SAP read omits UI fields (delivery date, service code)."""
    form = merge_pr_form_from_seed(form, document_type=document_type, seed_form=seed_form)
    if (document_type or "").upper() != "YSER" or not seed_form:
        return form
    from app.procurement.sap_pr_z_payload import normalize_service_performer_code

    seed_lines = _seed_lines(seed_form)
    lines = form.get("lines")
    if not isinstance(lines, list) or not seed_lines:
        return form

    seed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    seed_by_svc: dict[str, dict[str, Any]] = {}
    seed_by_ref: dict[str, dict[str, Any]] = {}
    for sl in seed_lines:
        ref = odata_norm(sl.get("sap_po_service_ref"))
        if ref:
            seed_by_ref[ref] = sl
        svc = normalize_service_performer_code(odata_norm(sl.get("service")))
        po_item = odata_norm(sl.get("purchase_order_item"))
        if svc and po_item:
            seed_by_key[(po_item, svc)] = sl
        if svc and svc not in seed_by_svc:
            seed_by_svc[svc] = sl

    for line in lines:
        if not isinstance(line, dict):
            continue
        ref = odata_norm(line.get("sap_po_service_ref"))
        seed_ln = seed_by_ref.get(ref) if ref else None
        svc = normalize_service_performer_code(odata_norm(line.get("service")))
        po_item = odata_norm(line.get("purchase_order_item"))
        if seed_ln is None:
            seed_ln = seed_by_key.get((po_item, svc)) if po_item and svc else None
        if seed_ln is None and svc:
            seed_ln = seed_by_svc.get(svc)
        if seed_ln is None and len(seed_lines) == 1:
            seed_ln = seed_lines[0]
        if not seed_ln:
            continue
        if not odata_norm(line.get("short_text")):
            st = odata_norm(seed_ln.get("short_text"))
            if st:
                line["short_text"] = st
        if not odata_norm(line.get("delivery_date")):
            dd = odata_norm(seed_ln.get("delivery_date"))
            if dd:
                line["delivery_date"] = dd
        if not odata_norm(line.get("order_unit")):
            uom = odata_norm(seed_ln.get("order_unit"))
            if uom:
                line["order_unit"] = uom
        allocs = line.get("allocations")
        seed_allocs = seed_ln.get("allocations")
        if isinstance(allocs, list) and isinstance(seed_allocs, list):
            has_cc = any(
                isinstance(a, dict) and odata_norm(a.get("cost_center")) for a in allocs
            )
            if not has_cc and seed_allocs:
                line["allocations"] = copy.deepcopy(seed_allocs)
    return form


def _seed_header(form: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(form, dict) and isinstance(form.get("header"), dict):
        return copy.deepcopy(form["header"])
    return {}


def _merge_header(seed: dict[str, Any], sap_header: dict[str, Any]) -> dict[str, Any]:
    out = {**seed}
    for key, val in sap_header.items():
        v = odata_norm(val)
        if v:
            out[key] = v
    return out


def _allocations_from_pr_acct(acct_node: Any, *, document_type: str = "") -> list[dict[str, str]]:
    from app.procurement.sap_yast_acct import allocations_from_sap_acct_node

    rows, _asset = allocations_from_sap_acct_node(acct_node, document_type=document_type)
    return rows


def _po_item_acct_node(entry: dict[str, Any], props: dict[str, Any]) -> Any:
    for nav in (PO_ACCT_CREATE_NAV, PO_ACCT_NAV):
        node = entry.get(nav)
        if node is None:
            node = props.get(nav)
        if node is not None:
            return node
    return None


def _allocations_from_po_acct(
    acct_node: Any, *, document_type: str = "", order_qty_fallback: str = ""
) -> tuple[list[dict[str, str]], str]:
    from app.procurement.sap_yast_acct import allocations_from_sap_acct_node

    return allocations_from_sap_acct_node(
        acct_node,
        document_type=document_type,
        order_qty_fallback=order_qty_fallback,
    )


def form_from_pr_sap_read(
    body: dict[str, Any],
    *,
    document_type: str,
    seed_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map ``GET A_PurchaseRequisitionHeader`` (+ expand) → AgentOS PR form."""
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return {"header": _seed_header(seed_form), "lines": []}

    seed_h = _seed_header(seed_form)
    from app.procurement.sap_pr_payload import (
        strip_agentos_bracket_markers_from_text,
        strip_pr_item_text_marker,
    )

    header = _merge_header(
        seed_h,
        {
            "purchasing_org": odata_text(root.get("PurchasingOrganization")),
            "header_note": strip_agentos_bracket_markers_from_text(
                odata_text(root.get("PurReqnDescription"))
            ),
        },
    )

    lines: list[dict[str, Any]] = []
    items_node = root.get("to_PurchaseReqnItem")
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        if is_sap_deleted_flag(odata_text(props.get("IsDeleted"))):
            continue
        item_no = odata_text(props.get("PurchaseRequisitionItem"))
        if not item_no:
            continue

        acct_node = props.get("to_PurchaseReqnAcctAssgmt")
        if acct_node is None and isinstance(entry.get("to_PurchaseReqnAcctAssgmt"), dict):
            acct_node = entry.get("to_PurchaseReqnAcctAssgmt")

        price = odata_text(props.get("PurchaseRequisitionPrice"))
        dt = (document_type or "").upper()
        _allocs, asset = allocations_from_sap_acct_node(
            acct_node,
            document_type=dt,
            order_qty_fallback=odata_text(props.get("RequestedQuantity")) or "1",
        )
        if not _allocs:
            _allocs = (
                [{"asset": "", "qty": "1"}]
                if dt == "YAST"
                else [{"cost_center": "", "qty": "1"}]
            )
        unit_p, val_p, gross_p = _material_pr_prices_from_sap(price, _allocs)
        grp_key = catalog_group_field(dt)
        line: dict[str, Any] = {
            "material": odata_text(props.get("Material")),
            "service": odata_text(props.get("ServicePerformer")),
            grp_key: odata_text(props.get("MaterialGroup")),
            "short_text": strip_pr_item_text_marker(
                odata_text(props.get("PurchaseRequisitionItemText"))
            ),
            "delivery_date": odata_date_to_form(props.get("DeliveryDate")),
            "unit_price": unit_p,
            "valuation_price": val_p,
            "gross_price": gross_p,
            "order_unit": odata_text(props.get("BaseUnit")),
            "item_category": odata_text(props.get("PurchasingDocumentItemCategory")),
            "account_assignment_cat": odata_text(props.get("AccountAssignmentCategory")),
            "purchase_requisition_item": item_no,
            "allocations": _allocs,
        }
        if asset:
            line["asset"] = asset
        lines.append(line)

        if not header.get("plant"):
            plant = odata_text(props.get("Plant"))
            if plant:
                header["plant"] = plant
        if not header.get("storage_location"):
            sloc = odata_text(props.get("StorageLocation"))
            if sloc:
                header["storage_location"] = sloc
        if not header.get("purchasing_group"):
            pg = odata_text(props.get("PurchasingGroup"))
            if pg:
                header["purchasing_group"] = pg
        supplier = odata_text(props.get("FixedSupplier"))
        if supplier and not header.get("vendor"):
            header["vendor"] = supplier

    lines.sort(key=lambda r: int(str(r.get("purchase_requisition_item") or "0") or "0"))
    sync_header_catalog_group(header, lines, document_type)
    return {"header": header, "lines": lines}


def _strip_po_item_text_marker(text: str) -> str:
    import re

    c = text.strip()
    m = re.match(r"^(.*)(AO[0-9A-F]{8})$", c, re.IGNORECASE)
    if m and m.group(1):
        return m.group(1)
    if re.fullmatch(r"AO[0-9A-F]{8}", c, re.IGNORECASE):
        return ""
    return c


def _po_header_note(correspnc: str, seed_note: str) -> str:
    import re

    c = correspnc.strip()
    if c.startswith("[AO:"):
        return seed_note or c
    m = re.match(r"^(.*)(AO[0-9A-F]{8}(?:[0-9A-F]{24})?)$", c, re.IGNORECASE)
    if m:
        user_part = (m.group(1) or "").strip()
        if user_part:
            return user_part
        return seed_note or c
    if re.fullmatch(r"AO[0-9A-F]{8}(?:[0-9A-F]{24})?", c, re.IGNORECASE):
        return seed_note or ""
    return c or seed_note


def form_from_po_sap_read(
    body: dict[str, Any],
    *,
    document_type: str,
    seed_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map ``GET A_PurchaseOrder`` (+ expand) → AgentOS PO form."""
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return {"header": _seed_header(seed_form), "lines": []}

    dt = (document_type or "").upper()
    seed_h = _seed_header(seed_form)
    correspnc = odata_text(root.get("CorrespncInternalReference"))
    header = _merge_header(
        seed_h,
        {
            "vendor": odata_text(root.get("Supplier")),
            "purchasing_org": odata_text(root.get("PurchasingOrganization")),
            "purchasing_group": odata_text(root.get("PurchasingGroup")),
            "header_note": _po_header_note(correspnc, odata_norm(seed_h.get("header_note"))),
            "tax_code": odata_text(root.get("TaxCode")) or seed_h.get("tax_code", ""),
            "payment_terms": odata_text(root.get("PaymentTerms"))
            or seed_h.get("payment_terms", ""),
            "requestor_email": (
                odata_text(root.get("SupplierRespSalesPersonName"))
                or odata_norm(seed_h.get("requestor_email"))
            ),
        },
    )
    from app.procurement.sap_po_z_payload import po_text_fields_from_sap_root

    header = _merge_header(header, po_text_fields_from_sap_root(root))

    lines: list[dict[str, Any]] = []
    items_node = root.get("to_PurchaseOrderItem")
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        if _po_item_deleted(props):
            continue
        item_no = odata_text(props.get("PurchaseOrderItem"))
        if not item_no:
            continue

        acct_node = _po_item_acct_node(entry, props)
        sched_node = entry.get(PO_SCHEDULE_NAV)
        if sched_node is None:
            sched_node = props.get(PO_SCHEDULE_NAV)

        price = odata_text(props.get("NetPriceAmount"))
        allocs, asset = _allocations_from_po_acct(
            acct_node,
            document_type=dt,
            order_qty_fallback=odata_text(props.get("OrderQuantity")) or "1",
        )
        if not allocs:
            allocs = (
                [{"asset": "", "qty": odata_text(props.get("OrderQuantity")) or "1"}]
                if dt == "YAST"
                else [{"cost_center": "", "qty": odata_text(props.get("OrderQuantity")) or "1"}]
            )
        unit_p, val_p, _ = _material_pr_prices_from_sap(price, allocs)
        delivery = odata_date_to_form(props.get("DeliveryDate")) or _delivery_from_po_schedule_node(
            sched_node
        )
        grp_key = catalog_group_field(dt)
        line: dict[str, Any] = {
            "material": odata_text(props.get("Material")),
            "service": odata_text(props.get("ServicePerformer")),
            grp_key: odata_text(props.get("MaterialGroup")),
            "short_text": _strip_po_item_text_marker(
                odata_text(props.get("PurchaseOrderItemText"))
            ),
            "delivery_date": delivery,
            "tax_code": odata_text(props.get("TaxCode")),
            "unit_price": unit_p or price,
            "net_price": unit_p or price,
            "valuation_price": val_p,
            "order_unit": odata_text(props.get("PurchaseOrderQuantityUnit")),
            "item_category": odata_text(props.get("PurchaseOrderItemCategory")),
            "account_assignment_cat": odata_text(props.get("AccountAssignmentCategory")),
            "purchase_requisition_item": odata_text(props.get("PurchaseRequisitionItem")),
            "allocations": allocs,
            "_sort_item": item_no,
        }
        if asset:
            line["asset"] = asset
        pr_no = odata_text(props.get("PurchaseRequisition"))
        if pr_no:
            line["_sap_purchase_requisition"] = pr_no
        lines.append(line)

        if not header.get("plant"):
            plant = odata_text(props.get("Plant"))
            if plant:
                header["plant"] = plant
        if not header.get("storage_location"):
            sloc = odata_text(props.get("StorageLocation"))
            if sloc:
                header["storage_location"] = sloc

    lines.sort(key=lambda r: int(str(r.get("_sort_item") or "0") or "0"))
    for row in lines:
        row.pop("_sort_item", None)
    sync_header_catalog_group(header, lines, document_type)
    from app.procurement.line_tax_code import sync_header_tax_code

    sync_header_tax_code(header, lines)
    return {"header": header, "lines": lines}


async def load_form_from_sap(
    *,
    kind: str,
    document_type: str,
    sap_id: str,
    ticket_id: str,
    seed_form: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Returns ``(form, form_source, error)`` — ``form_source`` is ``sap`` or ``db``.

    YSER uses Z ``PRHeaderSet`` (deferred items/services). YUNB/YAST use standard OData.
    """
    sap_id = odata_norm(sap_id)
    if not sap_id:
        return None, "db", None

    kind_u = (kind or "").upper()
    dt = (document_type or "").upper()
    if kind_u == "PR":
        if dt == "YSER":
            body, err = await get_yser_pr(pr_number=sap_id, ticket_id=ticket_id)
            if err or not body:
                return None, "db", err or "SAP YSER read failed"
            form = form_from_z_pr_read(
                body, document_type=document_type, seed_form=seed_form
            )
        else:
            body, err = await get_pr(pr_number=sap_id, ticket_id=ticket_id)
            if err or not body:
                return None, "db", err or "SAP PR read failed"
            form = form_from_pr_sap_read(
                body, document_type=document_type, seed_form=seed_form
            )
        form = merge_pr_form_from_seed(
            form, document_type=document_type, seed_form=seed_form
        )
        if not form.get("lines"):
            return None, "db", SAP_NO_ACTIVE_LINES_PR
        return form, "sap", None

    if kind_u == "PO":
        body, err = await get_po(
            po_number=sap_id, ticket_id=ticket_id, document_type=document_type
        )
        if err or not body:
            return None, "db", err or "SAP PO read failed"
        if dt == "YSER":
            from app.procurement.sap_po_z_payload import form_from_z_po_read

            form = form_from_z_po_read(body, seed_form=seed_form)
        else:
            form = form_from_po_sap_read(
                body, document_type=document_type, seed_form=seed_form
            )
        form = merge_po_form_from_seed(
            form, document_type=document_type, seed_form=seed_form
        )
        if not form.get("lines"):
            return None, "db", SAP_NO_ACTIVE_LINES_PO
        return form, "sap", None

    return None, "db", f"Unknown ticket kind {kind_u!r}"
