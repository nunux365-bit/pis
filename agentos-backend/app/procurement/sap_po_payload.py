"""Map AgentOS procurement forms to SAP API_PURCHASEORDER_PROCESS_SRV payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.procurement.field_schema import (
    PO_INCOTERMS_LOCATION2_MAX_LEN,
    PO_REQUESTOR_EMAIL_MAX_LEN,
)
from app.procurement.line_catalog_group import line_catalog_group
from app.procurement.line_tax_code import line_tax_code
from app.procurement.sap_odata_utils import (
    is_sap_deleted_flag,
    odata_base_root as _base_root,
    odata_date_to_form,
    odata_entity_key,
    odata_entity_properties,
    odata_norm as _norm,
    odata_results_list,
    odata_text,
    storage_location_for_sap,
)
from app.procurement.sap_pr_payload import (
    SapAcctSegment,
    _base_unit,
    _company_code,
    _fixed_supplier,
    _max_acct_seq_number,
    _requested_quantity_from_allocs,
    _sap_date,
    _sap_quantity,
    build_pur_reqn_description,
    legacy_sap_ticket_ref_tag,
    sap_ticket_correspnc_alnum_marker,
    sap_ticket_correspnc_external_marker,
    sap_ticket_ref_tag,
    strip_pr_item_text_marker,
)
from app.procurement.sap_yast_acct import (
    material_account_assignment_category,
    yast_acct_extra_fields,
    yast_acct_row_should_emit,
    yast_optional_gl_fields,
)

PO_SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/"
PO_HEADER_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
PO_ITEM_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrderItem"


def normalize_po_item_number(raw: Any) -> str:
    """Canonical PO item number for matching (``00010`` and ``10`` → ``10``)."""
    s = _norm(raw)
    if not s:
        return ""
    if s.isdigit():
        return str(int(s))
    return s

# QAS PR-linked + ``to_AccountAssignment`` reads use ``A_PurOrdAccountAssignment``.
PO_ACCT_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurOrdAccountAssignment"
# Legacy name kept for deferred-expand helpers that still reference this nav.
PO_ACCT_LEGACY_COLLECTION = (
    "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrderAccountAssignment"
)

PO_DEFAULT_CURRENCY = "INR"
# Legacy reference only — payloads omit PaymentTerms unless header.payment_terms is set.
PO_DEFAULT_PAYMENT_TERMS = "0001"
# Legacy placeholder — do not auto-send on create (blocks SAP JEXS/GST on QAS). Kept for tests
# that assert explicit header.tax_jurisdiction passthrough.
PO_DEFAULT_TAX_JURISDICTION = "ABCDABCDAB"

# OData navigation for account assignment segments on create (deep insert).
PO_ACCT_NAV = "to_PurchaseOrderAccountAssignment"
# PO All API doc uses this nav on YAST create (not ``to_PurchaseOrderAccountAssignment``).
PO_ACCT_CREATE_NAV = "to_AccountAssignment"
PO_SCHEDULE_NAV = "to_ScheduleLine"
PO_DEFAULT_CONTROLLING_AREA = "T1MG"
PO_DEFAULT_GL_ACCOUNT = "29000001"
PO_ITEM_TEXT_MAX_LEN = 40
# QAS ``CorrespncInternalReference`` (EKKO-VERKF) — 12 chars; PR ``PurReqnDescription`` allows 40.
PO_CORRESPNC_INTERNAL_REFERENCE_MAX_LEN = 12


def build_po_correspnc_internal_reference(header_note: str) -> str:
    """OPS header note for PO ``CorrespncInternalReference`` (max 12 on QAS; trace is external only)."""
    return _norm(header_note)[:PO_CORRESPNC_INTERNAL_REFERENCE_MAX_LEN]


def apply_po_correspnc_internal_reference(
    body: dict[str, Any],
    header: dict[str, Any],
    *,
    force: bool = False,
) -> None:
    """Set ``CorrespncInternalReference`` when the form has a header note (or ``force`` for PATCH)."""
    note = build_po_correspnc_internal_reference(_norm(header.get("header_note")))
    if note or force:
        body["CorrespncInternalReference"] = note


def _tax_jurisdiction_for_po_item(header: dict[str, Any], tax_code: str) -> str:
    """Send jurisdiction only when the form sets ``header.tax_jurisdiction``.

    Omitting the default placeholder lets SAP auto-calculate JEXS/GST on QAS. Some tax
    codes (e.g. XE) may still require an explicit jurisdiction from master data.
    """
    if not tax_code:
        return ""
    return _norm(header.get("tax_jurisdiction"))


def _payment_terms_for_payload(header: dict[str, Any]) -> str:
    """Only send when the form has a value (no UI / SAP default until user selects)."""
    return _norm(header.get("payment_terms"))


def sap_po_service_root_url(base_url: str) -> str:
    """OData service root for PO — CSRF fetch."""
    return f"{_base_root(base_url)}{PO_SERVICE_PATH}"


def po_collection_url(base_url: str) -> str:
    return f"{_base_root(base_url)}{PO_HEADER_COLLECTION}"


_MATERIAL_PO_TYPES = frozenset({"YUNB", "YAST"})


def po_requestor_email_for_sap(header: dict[str, Any]) -> str:
    """SAP salesperson / requestor email (``SupplierRespSalesPersonName``, max 30)."""
    return _norm(header.get("requestor_email"))[:PO_REQUESTOR_EMAIL_MAX_LEN]


def po_creator_incoterms_location1_for_sap(creator_email: str | None) -> str:
    """YUNB/YAST logged-in creator email on ``IncotermsLocation1`` (max 70)."""
    return _norm(creator_email)[:PO_INCOTERMS_LOCATION2_MAX_LEN]


def _apply_standard_po_requestor_header_fields(
    body: dict[str, Any],
    header: dict[str, Any],
    *,
    document_type: str,
    creator_email: str | None = None,
    include_creator_incoterms: bool = True,
) -> None:
    requestor_email = po_requestor_email_for_sap(header)
    if requestor_email:
        body["SupplierRespSalesPersonName"] = requestor_email
    if include_creator_incoterms and (document_type or "").upper() in _MATERIAL_PO_TYPES:
        loc1 = po_creator_incoterms_location1_for_sap(creator_email)
        if loc1:
            body["IncotermsLocation1"] = loc1


def po_entity_url(base_url: str, po_number: str) -> str:
    key = odata_entity_key(po_number)
    if not key:
        raise ValueError("PO number is required for entity URL")
    return f"{_base_root(base_url)}{PO_HEADER_COLLECTION}('{key}')"


def po_item_entity_url(base_url: str, *, po_number: str, item_number: str) -> str:
    po_key = odata_entity_key(po_number)
    item_key = odata_entity_key(item_number)
    if not po_key or not item_key:
        raise ValueError("PO number and item number are required")
    return (
        f"{_base_root(base_url)}{PO_ITEM_COLLECTION}"
        f"(PurchaseOrder='{po_key}',PurchaseOrderItem='{item_key}')"
    )


def po_schedule_line_entity_url(
    base_url: str,
    *,
    po_number: str,
    item_number: str,
    schedule_line: str = "1",
) -> str:
    """Qty updates must PATCH schedule-line quantity (item ``OrderQuantity`` is not processable).

    QAS keys: ``PurchasingDocument``, ``PurchasingDocumentItem``, ``ScheduleLine``.
    """
    po_key = odata_entity_key(po_number)
    item_key = odata_entity_key(item_number)
    sl_key = odata_entity_key(schedule_line) or "1"
    if not po_key or not item_key:
        raise ValueError("PO number and item number are required")
    return (
        f"{_base_root(base_url)}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/"
        f"A_PurchaseOrderScheduleLine(PurchasingDocument='{po_key}',"
        f"PurchasingDocumentItem='{item_key}',ScheduleLine='{sl_key}')"
    )


def po_read_full_url(base_url: str, po_number: str) -> str:
    # QAS rejects ``to_PurchaseOrderAccountAssignment`` on $expand; schedule + deferred follow.
    return (
        f"{po_entity_url(base_url, po_number)}"
        f"?$expand=to_PurchaseOrderItem/{PO_SCHEDULE_NAV}"
    )


def po_item_collection_post_url(base_url: str, po_number: str) -> str:
    return f"{po_entity_url(base_url, po_number)}/to_PurchaseOrderItem"


def po_acct_assgmt_entity_url(
    base_url: str,
    *,
    po_number: str,
    item_number: str,
    acct_assgmt_number: str,
) -> str:
    po_key = odata_entity_key(po_number)
    item_key = odata_entity_key(item_number)
    acct_key = odata_entity_key(acct_assgmt_number)
    if not po_key or not item_key or not acct_key:
        raise ValueError("PO number, item number, and account assignment number are required")
    return (
        f"{_base_root(base_url)}{PO_ACCT_COLLECTION}"
        f"(PurchaseOrder='{po_key}',PurchaseOrderItem='{item_key}',"
        f"AccountAssignmentNumber='{acct_key}')"
    )


def po_acct_collection_post_url(base_url: str) -> str:
    return f"{_base_root(base_url)}{PO_ACCT_COLLECTION}"


def build_po_item_text_for_sap(short_text: str, *, ticket_id: str | None = None) -> str:
    """``PurchaseOrderItemText`` — OPS short text only (trace is on header external fields)."""
    _ = ticket_id
    base = _norm(short_text) or "PO"
    return base[:PO_ITEM_TEXT_MAX_LEN]


def _net_price_amount(block: dict[str, Any], *, kind: str) -> str:
    kind_u = (kind or "").upper()
    if kind_u == "PO":
        v = _norm(block.get("net_price"))
        if v:
            return _format_po_net_price(v)
    for key in ("unit_price", "valuation_price", "gross_price", "net_price"):
        v = _norm(block.get(key))
        if v:
            raw = v
            if kind_u == "PO":
                return _format_po_net_price(raw)
            return raw
    return ""


def _format_po_net_price(raw: str) -> str:
    """SAP PO API expects decimal strings (e.g. ``100.00``)."""
    text = _norm(raw)
    if not text:
        return "0.00"
    try:
        return f"{float(text.replace(',', '.')):.2f}"
    except ValueError:
        return text


def format_po_purchase_requisition_item(raw: Any) -> str:
    """SAP PO API expects 5-digit PR item numbers (e.g. ``00010``)."""
    s = _norm(raw)
    if not s:
        return ""
    if s.isdigit():
        return str(int(s)).zfill(5)
    return s


def _po_order_quantity_pr_linked(block: dict[str, Any]) -> str:
    """PR-linked PO create — SAP sample uses whole units (``1`` not ``1.000``)."""
    allocs = block.get("allocations")
    if not isinstance(allocs, list):
        allocs = []
    qty = _requested_quantity_from_allocs(allocs)
    if not qty:
        return "1"
    sap_qty = _sap_quantity(qty)
    try:
        value = float(sap_qty.replace(",", "."))
        if value == int(value):
            return str(int(value))
    except ValueError:
        pass
    return sap_qty


def _po_purchase_order_item_category(document_type: str, block: dict[str, Any]) -> str:
    """PO item category per SAP doc: ``0`` material (YUNB/YAST), ``9`` service (YSER)."""
    explicit = _norm(block.get("item_category"))
    if explicit in ("0", "9"):
        return explicit
    dt = (document_type or "").upper()
    if dt == "YSER":
        return "9"
    return "0"


def _po_account_assignment_category(document_type: str, block: dict[str, Any], form_value: str) -> str:
    """YUNB → ``K``; YAST → ``A`` (asset) per PO API."""
    return material_account_assignment_category(document_type, form_value)


def _delivery_from_schedule_node(node: Any) -> str:
    for entry in odata_results_list(node):
        props = odata_entity_properties(entry)
        d = odata_date_to_form(props.get("ScheduleLineDeliveryDate"))
        if d:
            return d
    return ""


def _build_po_schedule_lines_for_create(
    *,
    block: dict[str, Any],
    order_qty: str,
) -> list[dict[str, Any]]:
    """PO delivery date lives on schedule line 0001 (OData ``/Date(ms)/``), not item ``DeliveryDate``."""
    from app.procurement.sap_odata_utils import (
        default_procurement_delivery_date,
        sanitize_form_delivery_date,
    )

    raw_dd = _norm(block.get("delivery_date"))
    if not raw_dd:
        return []
    form_dd = sanitize_form_delivery_date(
        raw_dd,
        fallback=default_procurement_delivery_date(),
    )
    delivery = _sap_date(form_dd)
    if not delivery:
        return []
    row: dict[str, Any] = {
        "ScheduleLine": "0001",
        "ScheduleLineDeliveryDate": delivery,
    }
    if order_qty:
        row["ScheduleLineOrderQuantity"] = order_qty
    return [row]


def _build_po_deep_acct_for_create(
    *,
    po_number: str,
    item_number: str,
    block: dict[str, Any],
    header: dict[str, Any],
    document_type: str,
    allocs: list[dict[str, Any]],
    unit: str,
) -> list[dict[str, Any]]:
    """PR-aligned deep insert: ``to_AccountAssignment`` rows on PO item create."""
    dt = (document_type or "").upper()
    if dt not in ("YUNB", "YAST"):
        return []

    rows = _build_po_acct_rows(
        po_number=po_number,
        item_number=item_number,
        block=block,
        document_type=dt,
        allocs=allocs,
    )

    if dt == "YAST":
        line_asset = _norm(block.get("asset"))
        if line_asset and not rows:
            qty = _sap_quantity(
                _requested_quantity_from_allocs(allocs if isinstance(allocs, list) else [])
            ) or "1.000"
            rows = [
                {
                    "PurchaseOrder": _norm(po_number),
                    "PurchaseOrderItem": item_number,
                    "AccountAssignmentNumber": "1",
                    "Quantity": qty,
                    **yast_acct_extra_fields(asset=line_asset),
                }
            ]
        gl_fields = yast_optional_gl_fields(header=header, block=block)
        for row in rows:
            if not _norm(row.get("MasterFixedAsset")) and line_asset:
                row["MasterFixedAsset"] = line_asset
            row.update(gl_fields)
            if unit:
                row["PurchaseOrderQuantityUnit"] = unit

    return rows


def _build_po_item_row_for_create_pr_linked(
    *,
    block: dict[str, Any],
    document_type: str,
    header: dict[str, Any],
    item_number: int,
    po_number: str,
    parent_pr_number: str,
    tax_code: str,
    ticket_id: str | None = None,
) -> dict[str, Any]:
    """PR-linked create — QAS-verified minimal shape (no plant/sloc/acct/schedule on item)."""
    dt = (document_type or "").upper()
    item_no_str = str(item_number)
    unit = _base_unit(block)
    pr_item_raw = _norm(block.get("purchase_requisition_item")) or "10"

    row: dict[str, Any] = {
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_no_str,
        "Material": "",
        "OrderQuantity": _po_order_quantity_pr_linked(block),
        "PurchaseRequisition": _norm(parent_pr_number),
        "PurchaseRequisitionItem": format_po_purchase_requisition_item(pr_item_raw),
        "PurchaseOrderQuantityUnit": unit,
        "NetPriceAmount": _net_price_amount(block, kind="PO") or "0.00",
        "PurchaseOrderItemCategory": _po_purchase_order_item_category(dt, block),
    }
    if tax_code:
        row["TaxCode"] = tax_code
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    if tax_jurisdiction:
        row["TaxJurisdiction"] = tax_jurisdiction
    if dt == "YAST":
        act_cat = _po_account_assignment_category(
            dt, block, _norm(block.get("account_assignment_cat"))
        )
        if act_cat:
            row["AccountAssignmentCategory"] = act_cat
    qty = row["OrderQuantity"]
    schedule = _build_po_schedule_lines_for_create(block=block, order_qty=str(qty))
    if schedule:
        row[PO_SCHEDULE_NAV] = schedule
    item_text = build_po_item_text_for_sap(
        _norm(block.get("short_text")),
        ticket_id=ticket_id,
    )
    if item_text:
        row["PurchaseOrderItemText"] = item_text
    return row


def _build_po_item_row_for_create(
    *,
    block: dict[str, Any],
    document_type: str,
    header: dict[str, Any],
    item_number: int,
    po_number: str,
    parent_pr_number: str | None,
    tax_code: str,
    ticket_id: str | None = None,
) -> dict[str, Any]:
    """POST item body for standalone PO create (plant, schedule, deep acct)."""
    pr_no = _norm(parent_pr_number)
    if pr_no:
        return _build_po_item_row_for_create_pr_linked(
            block=block,
            document_type=document_type,
            header=header,
            item_number=item_number,
            po_number=po_number,
            parent_pr_number=pr_no,
            tax_code=tax_code,
            ticket_id=ticket_id,
        )

    dt = (document_type or "").upper()
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(_norm(header.get("storage_location")), plant=plant)
    material = _norm(block.get("material")) if dt != "YSER" else ""
    unit = _base_unit(block)
    qty = _requested_quantity_from_allocs(
        block.get("allocations") if isinstance(block.get("allocations"), list) else []
    )
    price = _net_price_amount(block, kind="PO")
    item_no_str = str(item_number)
    allocs = block.get("allocations")
    if not isinstance(allocs, list):
        allocs = []

    row: dict[str, Any] = {
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_no_str,
        "Plant": plant,
        "StorageLocation": sloc,
        "Material": material,
        "OrderQuantity": qty,
        "PurchaseOrderQuantityUnit": unit,
        "NetPriceAmount": price or "0.00",
        "PurchaseOrderItemCategory": _po_purchase_order_item_category(dt, block),
    }
    if tax_code:
        row["TaxCode"] = tax_code
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    if tax_jurisdiction:
        row["TaxJurisdiction"] = tax_jurisdiction

    schedule = _build_po_schedule_lines_for_create(block=block, order_qty=qty)
    if schedule:
        row[PO_SCHEDULE_NAV] = schedule

    item_text = build_po_item_text_for_sap(
        _norm(block.get("short_text")),
        ticket_id=ticket_id,
    )
    if item_text:
        row["PurchaseOrderItemText"] = item_text

    inline = _build_po_deep_acct_for_create(
        po_number=po_number,
        item_number=item_no_str,
        block=block,
        header=header,
        document_type=dt,
        allocs=allocs,
        unit=unit,
    )
    if inline:
        row[PO_ACCT_CREATE_NAV] = inline
        if len(inline) > 1:
            row["MultipleAcctAssgmtDistribution"] = "1"

    mat_grp = line_catalog_group(block, header, dt)
    if mat_grp:
        row["MaterialGroup"] = mat_grp
    if dt == "YAST":
        row["OrderPriceUnit"] = unit
        info_rec = _norm(block.get("purchasing_info_record"))
        if info_rec:
            row["PurchasingInfoRecord"] = info_rec

    act_cat = _po_account_assignment_category(
        dt, block, _norm(block.get("account_assignment_cat"))
    )
    if act_cat:
        row["AccountAssignmentCategory"] = act_cat
    elif inline and dt == "YUNB":
        row["AccountAssignmentCategory"] = "K"

    return row


def po_create_item_has_inline_acct(item_row: dict[str, Any]) -> bool:
    return bool(item_row.get(PO_ACCT_CREATE_NAV))


def _build_po_acct_rows(
    *,
    po_number: str,
    item_number: str,
    block: dict[str, Any],
    document_type: str,
    allocs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dt = (document_type or "").upper()
    line_asset = _norm(block.get("asset"))
    rows: list[dict[str, Any]] = []
    seq = 1
    for alloc in allocs:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        qty = _sap_quantity(_norm(alloc.get("qty"))) or "1.000"
        row_asset = _norm(alloc.get("asset")) or line_asset
        if dt == "YAST":
            if not yast_acct_row_should_emit(asset=row_asset, qty=qty):
                continue
        elif not cc:
            continue
        row: dict[str, Any] = {
            "PurchaseOrder": _norm(po_number),
            "PurchaseOrderItem": item_number,
            "AccountAssignmentNumber": str(seq),
            "Quantity": qty,
        }
        if dt == "YAST":
            row.update(yast_acct_extra_fields(asset=row_asset))
            row.update(yast_optional_gl_fields(block=block))
        elif cc:
            row["CostCenter"] = cc
        rows.append(row)
        seq += 1
    return rows


def _build_po_item_row(
    *,
    block: dict[str, Any],
    document_type: str,
    header: dict[str, Any],
    item_number: int,
    po_number: str,
    parent_pr_number: str | None,
    kind: str,
    tax_code: str,
    ticket_id: str | None = None,
) -> dict[str, Any]:
    dt = (document_type or "").upper()
    pur_org = _norm(header.get("purchasing_org"))
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(
        _norm(header.get("storage_location")), plant=plant
    )
    mat_grp = line_catalog_group(block, header, dt)

    material = _norm(block.get("material")) if dt != "YSER" else ""
    service_no = _norm(block.get("service")) if dt == "YSER" else ""
    act_cat = _po_account_assignment_category(
        dt, block, _norm(block.get("account_assignment_cat"))
    )
    unit = _base_unit(block)
    qty = _requested_quantity_from_allocs(
        block.get("allocations") if isinstance(block.get("allocations"), list) else []
    )
    price = _net_price_amount(block, kind=kind)
    product_type = "2" if dt == "YSER" else "1"
    item_text = build_po_item_text_for_sap(
        _norm(block.get("short_text")),
        ticket_id=ticket_id,
    )

    allocs = block.get("allocations")
    if not isinstance(allocs, list) or not allocs:
        allocs = [{}]

    item_no_str = str(item_number)
    row: dict[str, Any] = {
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_no_str,
        "Plant": plant,
        "StorageLocation": sloc,
        "MaterialGroup": mat_grp,
        "AccountAssignmentCategory": act_cat,
        "ProductType": product_type,
        "Material": material,
        "OrderQuantity": qty,
        "PurchaseOrderQuantityUnit": unit,
        "OrderPriceUnit": unit,
        "NetPriceAmount": price or "0.00",
        "NetPriceQuantity": "1",
        "PurchaseOrderItemText": item_text,
        "PurchaseOrderItemCategory": _po_purchase_order_item_category(dt, block),
    }
    if tax_code:
        row["TaxCode"] = tax_code
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    if tax_jurisdiction:
        row["TaxJurisdiction"] = tax_jurisdiction
    if dt == "YSER" and service_no:
        row["ServicePerformer"] = service_no

    pr_no = _norm(parent_pr_number)
    if pr_no:
        row["PurchaseRequisition"] = pr_no
        pr_item = _norm(block.get("purchase_requisition_item")) or "10"
        row["PurchaseRequisitionItem"] = format_po_purchase_requisition_item(pr_item)

    # Deep insert on ``to_PurchaseOrderAccountAssignment`` is rejected on QAS create; cost
    # centres are implied via ``AccountAssignmentCategory`` + PR link / follow-up acct POST.

    return row


def _iter_po_form_lines(
    form: dict[str, Any], *, document_type: str
) -> list[tuple[str, dict[str, Any]]]:
    """(PurchaseOrderItem number, line block) in SAP item steps of 10."""
    dt = (document_type or "").upper()
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []
    out: list[tuple[str, dict[str, Any]]] = []
    item_no = 10
    for block in lines_in:
        if not isinstance(block, dict):
            continue
        allocs = block.get("allocations")
        if not isinstance(allocs, list) or not allocs:
            allocs = [{}]
        material = _norm(block.get("material")) if dt != "YSER" else ""
        service_no = _norm(block.get("service")) if dt == "YSER" else ""
        has_line = bool(material or service_no or _norm(block.get("short_text")))
        has_alloc = any(
            isinstance(a, dict) and (_norm(a.get("cost_center")) or _norm(a.get("qty")))
            for a in allocs
        )
        if not has_line and not has_alloc:
            continue
        out.append((str(item_no), block))
        item_no += 10
    return out


def build_po_payload(
    *,
    form: dict[str, Any],
    document_type: str,
    po_number: str = "",
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    kind: str = "PO",
    creator_email: str | None = None,
) -> dict[str, Any]:
    """POST body for ``A_PurchaseOrder`` (deep insert ``to_PurchaseOrderItem``)."""
    dt = (document_type or "").upper()
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    pur_org = _norm(header.get("purchasing_org"))
    supplier = _fixed_supplier(header)
    if not supplier:
        raise ValueError("Vendor (Supplier) is required for SAP PO create")

    po_key = _norm(po_number)

    is_create = not po_key
    items: list[dict[str, Any]] = []
    for item_no_str, block in _iter_po_form_lines(form, document_type=dt):
        line_tax = line_tax_code(block, header)
        if is_create:
            items.append(
                _build_po_item_row_for_create(
                    block=block,
                    document_type=dt,
                    header=header,
                    item_number=int(item_no_str),
                    po_number=po_key,
                    parent_pr_number=parent_pr_number,
                    tax_code=line_tax,
                    ticket_id=ticket_id,
                )
            )
        else:
            items.append(
                _build_po_item_row(
                    block=block,
                    document_type=dt,
                    header=header,
                    item_number=int(item_no_str),
                    po_number=po_key,
                    parent_pr_number=parent_pr_number,
                    kind=kind,
                    tax_code=line_tax,
                    ticket_id=ticket_id,
                )
            )

    if not items:
        raise ValueError("No PO line items to send to SAP (empty blocks or allocations).")

    payload: dict[str, Any] = {
        "PurchaseOrder": po_key,
        "CompanyCode": _company_code(header, pur_org),
        "PurchaseOrderType": dt,
        "Supplier": supplier,
        "PurchasingOrganization": pur_org,
        "PurchasingGroup": _norm(header.get("purchasing_group")),
        "DocumentCurrency": PO_DEFAULT_CURRENCY,
        "to_PurchaseOrderItem": items,
    }
    pay_terms = _payment_terms_for_payload(header)
    if pay_terms:
        payload["PaymentTerms"] = pay_terms
    _apply_standard_po_requestor_header_fields(
        payload, header, document_type=dt, creator_email=creator_email
    )
    apply_po_correspnc_internal_reference(payload, header)
    if is_create and ticket_id:
        marker = sap_ticket_correspnc_external_marker(ticket_id)
        if marker:
            payload["CorrespncExternalReference"] = marker
    return payload


def po_line_needs_acct_post_create(block: dict[str, Any]) -> bool:
    """Account rows are deep-inserted on create (PR-aligned); no post-create POST."""
    return False


def iter_po_acct_post_bodies(
    *,
    form: dict[str, Any],
    document_type: str,
    po_number: str,
) -> list[dict[str, Any]]:
    """Account-assignment POST bodies after PO create (multi-CC; PR-linked or standalone)."""
    bodies: list[dict[str, Any]] = []
    po_key = _norm(po_number)
    if not po_key:
        return bodies
    dt = (document_type or "").upper()
    for item_no_str, block in _iter_po_form_lines(form, document_type=dt):
        if not po_line_needs_acct_post_create(block):
            continue
        allocs = block.get("allocations")
        if not isinstance(allocs, list):
            continue
        acct_rows = _build_po_acct_rows(
            po_number=po_key,
            item_number=item_no_str,
            block=block if isinstance(block, dict) else {},
            document_type=dt,
            allocs=allocs,
        )
        for seq, acct in enumerate(acct_rows, start=1):
            body = _po_acct_post_body(acct, seq=seq)
            body["PurchaseOrder"] = po_key
            body["PurchaseOrderItem"] = item_no_str
            bodies.append(body)
    return bodies


def build_po_item_delete_patch() -> dict[str, str]:
    return {"PurchasingDocumentDeletionCode": "X"}


def build_po_acct_delete_patch() -> dict[str, str]:
    return {"PurchasingDocumentDeletionCode": "X"}


def _po_item_deleted(props: dict[str, Any]) -> bool:
    return is_sap_deleted_flag(odata_text(props.get("PurchasingDocumentDeletionCode"))) or is_sap_deleted_flag(
        odata_text(props.get("IsDeleted"))
    )


@dataclass
class SapPoItemSnapshot:
    item_number: str
    is_deleted: bool
    material: str = ""
    service_performer: str = ""
    order_quantity: str = ""
    item_text: str = ""
    delivery_date: str = ""
    schedule_line: str = "1"
    plant: str = ""
    storage_location: str = ""
    net_price: str = ""
    tax_code: str = ""
    tax_jurisdiction: str = ""
    material_group: str = ""
    order_unit: str = ""
    cost_centers: list[str] = field(default_factory=list)
    acct_qty_by_cc: dict[str, str] = field(default_factory=dict)
    acct_segments: list[SapAcctSegment] = field(default_factory=list)


def _sap_qty_equal(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return a == b
    if a == b:
        return True
    try:
        return float(a.replace(",", ".")) == float(b.replace(",", "."))
    except ValueError:
        return False


def _po_item_acct_node_from_read(entry: dict[str, Any], props: dict[str, Any]) -> Any:
    """Create nav ``to_AccountAssignment`` and update nav ``to_PurchaseOrderAccountAssignment``."""
    for nav in (PO_ACCT_CREATE_NAV, PO_ACCT_NAV):
        node = entry.get(nav)
        if node is None:
            node = props.get(nav)
        if node is not None:
            return node
    return None


def parse_po_items_from_read(body: dict[str, Any]) -> tuple[str, list[SapPoItemSnapshot]]:
    """Parse OData GET ``A_PurchaseOrder`` (+ expanded items and account assignments)."""
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return "", []
    header_ref = odata_text(root.get("CorrespncInternalReference"))
    items_node = root.get("to_PurchaseOrderItem")
    snapshots: list[SapPoItemSnapshot] = []
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        item_no = normalize_po_item_number(odata_text(props.get("PurchaseOrderItem")))
        if not item_no:
            continue
        deleted = _po_item_deleted(props)
        acct_node = _po_item_acct_node_from_read(entry, props)
        sched_node = entry.get(PO_SCHEDULE_NAV) or props.get(PO_SCHEDULE_NAV)
        delivery = odata_date_to_form(props.get("DeliveryDate")) or _delivery_from_schedule_node(
            sched_node
        )
        schedule_line = "1"
        for sched_entry in odata_results_list(sched_node):
            sp = odata_entity_properties(sched_entry)
            sl = odata_text(sp.get("ScheduleLine"))
            if sl:
                schedule_line = sl.lstrip("0") or sl
                break
        ccs: list[str] = []
        acct_qty: dict[str, str] = {}
        acct_segments: list[SapAcctSegment] = []
        for acct_entry in odata_results_list(acct_node):
            ap = odata_entity_properties(acct_entry)
            if is_sap_deleted_flag(odata_text(ap.get("PurchasingDocumentDeletionCode"))) or is_sap_deleted_flag(
                odata_text(ap.get("IsDeleted"))
            ):
                continue
            seq = odata_text(ap.get("AccountAssignmentNumber"))
            cc = odata_text(ap.get("CostCenter"))
            qty = odata_text(ap.get("Quantity"))
            asset = odata_text(ap.get("MasterFixedAsset"))
            if seq and (cc or asset or qty):
                acct_segments.append(
                    SapAcctSegment(
                        seq=seq,
                        cost_center=cc,
                        quantity=qty,
                        master_asset=asset,
                    )
                )
            if cc:
                ccs.append(cc)
                if qty:
                    acct_qty[cc] = qty
        snapshots.append(
            SapPoItemSnapshot(
                item_number=item_no,
                is_deleted=deleted,
                material=odata_text(props.get("Material")),
                service_performer=odata_text(props.get("ServicePerformer")),
                order_quantity=odata_text(props.get("OrderQuantity")),
                item_text=odata_text(props.get("PurchaseOrderItemText")),
                delivery_date=delivery,
                schedule_line=schedule_line,
                plant=odata_text(props.get("Plant")),
                storage_location=odata_text(props.get("StorageLocation")),
                net_price=odata_text(props.get("NetPriceAmount")),
                tax_code=odata_text(props.get("TaxCode")),
                tax_jurisdiction=odata_text(props.get("TaxJurisdiction")),
                material_group=odata_text(props.get("MaterialGroup")),
                order_unit=odata_text(props.get("PurchaseOrderQuantityUnit")),
                cost_centers=ccs,
                acct_qty_by_cc=acct_qty,
                acct_segments=acct_segments,
            )
        )
    return header_ref, snapshots


def _po_acct_patch_body(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in acct.items()
        if k
        not in (
            "PurchaseOrder",
            "PurchaseOrderItem",
            "AccountAssignmentNumber",
        )
    }


# Re-sending PR-link keys on item PATCH can re-trigger VERTEX / partner issues on QAS.
# TaxCode / TaxJurisdiction are allowed on update so UI tax changes reach SAP.
_PO_ITEM_PATCH_OMIT_KEYS: frozenset[str] = frozenset(
    {
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
    }
)


def po_item_patch_body(item_body: dict[str, Any]) -> dict[str, Any]:
    """Item entity PATCH — omit keys and OData wrappers."""
    return {
        k: v
        for k, v in item_body.items()
        if k not in ("PurchaseOrder", "PurchaseOrderItem")
        and k not in _PO_ITEM_PATCH_OMIT_KEYS
        and not k.startswith("to_")
    }


def _acct_segment_unchanged(seg: SapAcctSegment, acct_patch: dict[str, Any]) -> bool:
    from app.procurement.sap_yast_acct import asset_codes_equal

    cc = _norm(acct_patch.get("CostCenter"))
    if cc and cc != seg.cost_center:
        return False
    asset = _norm(acct_patch.get("MasterFixedAsset"))
    if asset and seg.master_asset and not asset_codes_equal(asset, seg.master_asset):
        return False
    qty = _norm(acct_patch.get("Quantity"))
    if qty and seg.quantity and not _sap_qty_equal(qty, seg.quantity):
        return False
    return True


def _po_acct_post_body(acct: dict[str, Any], *, seq: int) -> dict[str, Any]:
    body = _po_acct_patch_body(acct)
    body["AccountAssignmentNumber"] = str(seq)
    return body


def build_po_header_patch(
    *,
    form: dict[str, Any],
    ticket_id: str | None,
    document_type: str = "",
    creator_email: str | None = None,
    existing_sap_header: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Header PATCH body for PO resubmit (trace marker on ``CorrespncExternalReference`` only).

  When ``existing_sap_header`` is set, only fields that differ from SAP are included
  (QAS rejects bundled no-op / read-only fields such as ``SupplierRespSalesPersonName``).
    """
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    body: dict[str, Any] = {"PurchasingGroup": _norm(header.get("purchasing_group"))}
    _apply_standard_po_requestor_header_fields(
        body,
        header,
        document_type=document_type,
        creator_email=creator_email,
        include_creator_incoterms=False,
    )
    apply_po_correspnc_internal_reference(body, header, force=True)
    if not existing_sap_header:
        return body
    delta: dict[str, Any] = {}
    for key, val in body.items():
        if _norm(val) != _norm(existing_sap_header.get(key)):
            delta[key] = val
    return delta


PoItemUpdatePatch = tuple[
    str,
    dict[str, Any],
    list[tuple[str, dict[str, Any]]],
    list[dict[str, Any]],
    list[str],
]


def build_po_item_patches(
    *,
    form: dict[str, Any],
    document_type: str,
    po_number: str,
    parent_pr_number: str | None = None,
    existing_items: list[SapPoItemSnapshot] | None = None,
    ticket_id: str | None = None,
    creator_email: str | None = None,
) -> list[PoItemUpdatePatch]:
    """Item + account-assignment diff per line (CC by cost centre; YAST by asset)."""
    from app.procurement.sap_yast_acct import asset_codes_equal

    dt = (document_type or "").upper()
    full = build_po_payload(
        form=form,
        document_type=document_type,
        po_number=po_number,
        parent_pr_number=parent_pr_number,
        kind="PO",
        ticket_id=None,
        creator_email=creator_email,
    )
    existing_by_item = {
        normalize_po_item_number(s.item_number): s
        for s in (existing_items or [])
        if s.item_number and not s.is_deleted
    }
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    blocks_by_item = {
        normalize_po_item_number(k): v for k, v in _iter_po_form_lines(form, document_type=document_type)
    }

    patches: list[PoItemUpdatePatch] = []
    for item in full.get("to_PurchaseOrderItem") or []:
        if not isinstance(item, dict):
            continue
        item_no = normalize_po_item_number(item.get("PurchaseOrderItem"))
        if not item_no:
            continue
        block = blocks_by_item.get(item_no) or {}
        allocs = block.get("allocations") if isinstance(block.get("allocations"), list) else []
        acct_rows = _build_po_acct_rows(
            po_number=po_number,
            item_number=item_no,
            block=block if isinstance(block, dict) else {},
            document_type=document_type,
            allocs=allocs if isinstance(allocs, list) else [],
        )
        item_body = {
            k: v
            for k, v in item.items()
            if k != PO_ACCT_NAV and not k.startswith("to_")
        }
        snapshot = existing_by_item.get(item_no)
        sap_segments = list(snapshot.acct_segments) if snapshot else []

        acct_patches: list[tuple[str, dict[str, Any]]] = []
        acct_creates: list[dict[str, Any]] = []
        acct_deletes: list[str] = []
        matched_seqs: set[str] = set()
        next_seq = _max_acct_seq_number(sap_segments) + 1

        for acct in acct_rows:
            if not isinstance(acct, dict):
                continue
            cc = _norm(acct.get("CostCenter"))
            asset = _norm(acct.get("MasterFixedAsset"))
            sap_seg = None
            if cc:
                sap_seg = next(
                    (
                        s
                        for s in sap_segments
                        if s.cost_center == cc and s.seq not in matched_seqs
                    ),
                    None,
                )
            elif dt == "YAST" and asset:
                sap_seg = next(
                    (
                        s
                        for s in sap_segments
                        if asset_codes_equal(s.master_asset, asset)
                        and s.seq not in matched_seqs
                    ),
                    None,
                )
                if sap_seg is None:
                    sap_seg = next(
                        (s for s in sap_segments if s.seq not in matched_seqs),
                        None,
                    )
            elif not cc:
                # YUNB / others: require cost centre on outbound acct rows.
                continue
            if sap_seg:
                matched_seqs.add(sap_seg.seq)
                acct_patch = _po_acct_patch_body(acct)
                if not _acct_segment_unchanged(sap_seg, acct_patch):
                    if dt == "YAST" and asset and asset_codes_equal(asset, sap_seg.master_asset):
                        # Asset identity already matches SAP — patch qty (and unit) only.
                        acct_patch = {
                            k: v
                            for k, v in acct_patch.items()
                            if k in ("Quantity", "PurchaseOrderQuantityUnit")
                        }
                    if acct_patch:
                        acct_patches.append((sap_seg.seq, acct_patch))
            else:
                acct_creates.append(_po_acct_post_body(acct, seq=next_seq))
                next_seq += 1

        for seg in sap_segments:
            if seg.seq and seg.seq not in matched_seqs:
                acct_deletes.append(seg.seq)

        # QAS material PO GET sometimes omits account assignments even when inline
        # acct was created on POST item; duplicate acct POST returns 404.
        if snapshot is not None and not sap_segments and (acct_creates or acct_deletes):
            acct_creates = []
            acct_deletes = []
            acct_patches = []

        patches.append((item_no, item_body, acct_patches, acct_creates, acct_deletes))
    return patches


@dataclass
class PoResubmitPlan:
    header_patch: dict[str, Any]
    item_patches: list[PoItemUpdatePatch]
    items_to_mark_deleted: list[str]
    items_to_create: list[dict[str, Any]]
    desired_item_numbers: list[str]


def build_po_resubmit_plan(
    *,
    form: dict[str, Any],
    document_type: str,
    po_number: str,
    existing_items: list[SapPoItemSnapshot],
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
    existing_sap_header: dict[str, Any] | None = None,
) -> PoResubmitPlan:
    header_patch = build_po_header_patch(
        form=form,
        ticket_id=ticket_id,
        document_type=document_type,
        creator_email=creator_email,
        existing_sap_header=existing_sap_header,
    )
    item_patches = build_po_item_patches(
        form=form,
        document_type=document_type,
        po_number=po_number,
        parent_pr_number=parent_pr_number,
        existing_items=existing_items,
        ticket_id=None,
        creator_email=creator_email,
    )
    desired_nos = [p[0] for p in item_patches]
    active_sap = {
        normalize_po_item_number(s.item_number)
        for s in existing_items
        if s.item_number and not s.is_deleted
    }
    desired_set = set(desired_nos)
    to_delete = sorted(
        active_sap - desired_set,
        key=lambda x: int(x) if x.isdigit() else 0,
        reverse=True,
    )

    full = build_po_payload(
        form=form,
        document_type=document_type,
        po_number=po_number,
        ticket_id=None,
        parent_pr_number=parent_pr_number,
        kind="PO",
        creator_email=creator_email,
    )
    items_by_no: dict[str, dict[str, Any]] = {}
    for row in full.get("to_PurchaseOrderItem") or []:
        if isinstance(row, dict):
            no = normalize_po_item_number(row.get("PurchaseOrderItem"))
            if no:
                items_by_no[no] = row

    to_create = [items_by_no[no] for no in desired_nos if no in items_by_no and no not in active_sap]

    return PoResubmitPlan(
        header_patch=header_patch,
        item_patches=item_patches,
        items_to_mark_deleted=to_delete,
        items_to_create=to_create,
        desired_item_numbers=desired_nos,
    )


def verify_po_read_against_form(
    body: dict[str, Any],
    *,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
) -> list[str]:
    """Compare SAP GET (full expand) to form; return human-readable mismatches."""
    header_ref, snapshots = parse_po_items_from_read(body)
    mismatches: list[str] = []
    hdr = form.get("header") if isinstance(form.get("header"), dict) else {}
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    exp_pay = _norm(hdr.get("payment_terms"))
    if exp_pay and isinstance(root, dict):
        sap_pay = _norm(odata_text(root.get("PaymentTerms")))
        if sap_pay and sap_pay != exp_pay:
            mismatches.append(
                f"PaymentTerms SAP {sap_pay!r} != form {exp_pay!r}"
            )
    exp_email = po_requestor_email_for_sap(hdr)
    if exp_email and isinstance(root, dict):
        sap_email = _norm(odata_text(root.get("SupplierRespSalesPersonName")))
        if sap_email and sap_email != exp_email:
            mismatches.append(
                f"SupplierRespSalesPersonName SAP {sap_email!r} != form {exp_email!r}"
            )
    dt = (document_type or "").upper()
    if dt in _MATERIAL_PO_TYPES:
        exp_loc1 = po_creator_incoterms_location1_for_sap(creator_email)
        if exp_loc1 and isinstance(root, dict):
            sap_loc1 = _norm(odata_text(root.get("IncotermsLocation1")))
            if sap_loc1 and sap_loc1 != exp_loc1:
                mismatches.append(
                    f"IncotermsLocation1 SAP {sap_loc1!r} != creator {exp_loc1!r}"
                )
    if isinstance(root, dict):
        from app.procurement.sap_po_z_payload import verify_po_texts_against_form
        from app.procurement.sap_ticket_form_read import _po_header_note

        mismatches.extend(
            verify_po_texts_against_form(root, header=hdr, document_type=dt)
        )
        if dt in _MATERIAL_PO_TYPES:
            exp_note = build_po_correspnc_internal_reference(_norm(hdr.get("header_note")))
            sap_note = _po_header_note(header_ref, exp_note)
            # QAS may omit CorrespncInternalReference on GET after line-only resubmit.
            if sap_note and exp_note and sap_note != exp_note:
                mismatches.append(
                    f"CorrespncInternalReference SAP {sap_note!r} != form {exp_note!r}"
                )
    try:
        payload = build_po_payload(
            form=form,
            document_type=document_type,
            po_number="1",
            ticket_id=ticket_id,
            parent_pr_number=parent_pr_number,
            kind="PO",
            creator_email=creator_email,
        )
    except ValueError as e:
        mismatches.append(f"form payload: {e}")
        return mismatches

    expected_items = payload.get("to_PurchaseOrderItem") or []
    active = {normalize_po_item_number(s.item_number): s for s in snapshots if not s.is_deleted}

    for exp in expected_items:
        if not isinstance(exp, dict):
            continue
        item_no = normalize_po_item_number(exp.get("PurchaseOrderItem"))
        sap = active.get(item_no)
        if not sap:
            mismatches.append(f"item {item_no}: missing on SAP (or marked deleted)")
            continue
        dt = (document_type or "").upper()
        if dt == "YSER":
            exp_id = _norm(exp.get("ServicePerformer"))
            if exp_id and sap.service_performer != exp_id:
                mismatches.append(
                    f"item {item_no}: ServicePerformer SAP {sap.service_performer!r} != {exp_id!r}"
                )
        else:
            exp_mat = _norm(exp.get("Material"))
            if exp_mat and sap.material != exp_mat:
                mismatches.append(
                    f"item {item_no}: Material SAP {sap.material!r} != {exp_mat!r}"
                )
        exp_qty = _norm(exp.get("OrderQuantity"))
        if exp_qty and sap.order_quantity and not _sap_qty_equal(sap.order_quantity, exp_qty):
            mismatches.append(
                f"item {item_no}: OrderQuantity SAP {sap.order_quantity!r} != {exp_qty!r}"
            )
        exp_block = None
        for want_no, blk in _iter_po_form_lines(form, document_type=document_type):
            if normalize_po_item_number(want_no) == item_no:
                exp_block = blk
                break
        if isinstance(exp_block, dict):
            from app.procurement.sap_odata_utils import sanitize_form_delivery_date

            exp_dd = sanitize_form_delivery_date(_norm(exp_block.get("delivery_date")))
            sap_dd = sanitize_form_delivery_date(sap.delivery_date)
            if exp_dd and sap_dd and exp_dd != sap_dd:
                mismatches.append(
                    f"item {item_no}: delivery_date SAP {sap_dd!r} != form {exp_dd!r}"
                )
            exp_short = _norm(exp_block.get("short_text"))
            if exp_short:
                expected_text = build_po_item_text_for_sap(exp_short)
                sap_plain = strip_pr_item_text_marker(sap.item_text or "")
                if sap_plain and sap_plain != expected_text:
                    mismatches.append(
                        f"item {item_no}: PurchaseOrderItemText SAP {sap.item_text!r} != {expected_text!r}"
                    )
        exp_allocs = (
            exp_block.get("allocations")
            if isinstance(exp_block, dict) and isinstance(exp_block.get("allocations"), list)
            else []
        )
        exp_acct_rows = _build_po_acct_rows(
            po_number="1",
            item_number=item_no,
            block=exp_block if isinstance(exp_block, dict) else {},
            document_type=document_type,
            allocs=exp_allocs if isinstance(exp_allocs, list) else [],
        )
        exp_cc_qty: dict[str, str] = {}
        for a in exp_acct_rows:
            cc = _norm(a.get("CostCenter"))
            if not cc:
                continue
            exp_cc_qty[cc] = _norm(a.get("Quantity"))
        exp_ccs = sorted(exp_cc_qty.keys())
        sap_ccs = sorted({s.cost_center for s in sap.acct_segments if s.cost_center})
        if not sap_ccs:
            sap_ccs = sorted(sap.cost_centers)
        exp_asset = _norm(exp_block.get("asset")) if isinstance(exp_block, dict) else ""
        acct_unreadable = bool(exp_ccs) and not sap_ccs and not sap.acct_segments
        if dt == "YAST" and exp_asset and not exp_ccs:
            acct_unreadable = True
        if exp_ccs and not acct_unreadable and sap_ccs != exp_ccs:
            mismatches.append(
                f"item {item_no}: cost centers SAP {sap_ccs!r} != form {exp_ccs!r}"
            )
        if not acct_unreadable:
            for cc, exp_q in exp_cc_qty.items():
                sap_q = sap.acct_qty_by_cc.get(cc, "")
                if exp_q and sap_q and not _sap_qty_equal(sap_q, exp_q):
                    mismatches.append(
                        f"item {item_no}: qty for {cc} SAP {sap_q!r} != form {exp_q!r}"
                    )

    exp_nos = {
        normalize_po_item_number(x.get("PurchaseOrderItem"))
        for x in expected_items
        if isinstance(x, dict)
    }
    exp_nos.discard("")
    for item_no in sorted(active.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        if item_no not in exp_nos:
            mismatches.append(f"item {item_no}: extra active line on SAP not in form")

    return mismatches


def parse_po_number_from_response(data: dict[str, Any]) -> str | None:
    if not isinstance(data, dict):
        return None
    d = data.get("d")
    if isinstance(d, dict):
        po = _norm(d.get("PurchaseOrder"))
        if po:
            return po
    po = _norm(data.get("PurchaseOrder"))
    return po or None
