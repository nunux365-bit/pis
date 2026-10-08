"""Read PR items from SAP (API_PURCHASEREQ_PROCESS_SRV) for PO prefill and create-time enrichment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.procurement.sap_odata_utils import odata_date_to_form, odata_norm as _norm
from app.procurement.sap_odata_utils import odata_results_list
from app.procurement.sap_pr_payload import strip_pr_item_text_marker
from app.procurement.sap_pr_client import get_pr, sap_pr_configured


@dataclass(frozen=True)
class SapPrAcctRow:
    seq: str
    cost_center: str
    quantity: str
    master_asset: str = ""


@dataclass
class SapPrItemLine:
    item_no: str
    material: str
    plant: str
    sloc: str
    material_group: str
    qty: str
    unit: str
    price: str
    pur_group: str
    pur_org: str
    item_cat: str
    acct_cat: str
    short_text: str
    service_performer: str = ""
    master_asset: str = ""
    fixed_supplier: str = ""
    delivery_date: str = ""
    acct_rows: list[SapPrAcctRow] = field(default_factory=list)
    """Real SAP ``PRItem`` when ``item_no`` is a synthetic UI line id (multi-service Z PR)."""
    sap_pr_item: str = ""


def _parse_pr_item_row(raw: dict[str, Any]) -> SapPrItemLine | None:
    if not isinstance(raw, dict):
        return None
    item_no = _norm(raw.get("PurchaseRequisitionItem"))
    if not item_no:
        return None
    accts: list[SapPrAcctRow] = []
    master_asset = ""
    acct_node = raw.get("to_PurchaseReqnAcctAssgmt")
    if isinstance(acct_node, dict):
        for a in acct_node.get("results") or []:
            if not isinstance(a, dict):
                continue
            cc = _norm(a.get("CostCenter"))
            qty = _norm(a.get("Quantity"))
            asset = _norm(a.get("MasterFixedAsset"))
            if asset and not master_asset:
                master_asset = asset
            if not (cc or qty or asset):
                continue
            accts.append(
                SapPrAcctRow(
                    seq=_norm(a.get("PurchaseReqnAcctAssgmtNumber")),
                    cost_center=cc,
                    quantity=qty or "0",
                    master_asset=asset,
                )
            )
    return SapPrItemLine(
        item_no=item_no,
        sap_pr_item=item_no,
        material=_norm(raw.get("Material")),
        plant=_norm(raw.get("Plant")),
        sloc=_norm(raw.get("StorageLocation")),
        material_group=_norm(raw.get("MaterialGroup")),
        qty=_norm(raw.get("RequestedQuantity")) or "1.000",
        unit=_norm(raw.get("BaseUnit")),
        price=_norm(raw.get("PurchaseRequisitionPrice")),
        pur_group=_norm(raw.get("PurchasingGroup")),
        pur_org=_norm(raw.get("PurchasingOrganization")),
        item_cat=_norm(raw.get("PurchasingDocumentItemCategory")),
        acct_cat=_norm(raw.get("AccountAssignmentCategory")),
        short_text=strip_pr_item_text_marker(
            _norm(raw.get("PurchaseRequisitionItemText"))
        )[:80],
        service_performer=_norm(raw.get("ServicePerformer")),
        master_asset=master_asset,
        fixed_supplier=_norm(raw.get("FixedSupplier")),
        delivery_date=odata_date_to_form(raw.get("DeliveryDate")),
        acct_rows=accts,
    )


def _parse_z_pr_item_row(
    raw: dict[str, Any], *, ui_line_start: int = 10
) -> list[SapPrItemLine]:
    """Map Z ``PRItemSet`` row (+ inline ``to_Services``) → one or more ``SapPrItemLine``."""
    if not isinstance(raw, dict):
        return []
    props = raw
    item_no = _norm(props.get("PRItem"))
    if not item_no:
        return []
    svc_node = raw.get("to_Services")
    if isinstance(svc_node, list):
        svc_entries = svc_node
    else:
        svc_entries = odata_results_list(svc_node)

    from app.procurement.sap_pr_z_payload import cluster_yser_pr_service_entries

    clusters = cluster_yser_pr_service_entries(svc_entries)
    if not clusters:
        line = _parse_z_pr_item_row_legacy_single(raw)
        return [line] if line else []

    out: list[SapPrItemLine] = []
    sap_item_no = item_no.lstrip("0") or item_no
    ui_line_no = ui_line_start
    for cluster in clusters:
        parsed = _parse_z_pr_service_cluster(
            props, cluster, sap_item_no=sap_item_no, ui_item_no=str(ui_line_no)
        )
        if parsed:
            out.append(parsed)
            ui_line_no += 10
    return out


def _parse_z_pr_item_row_legacy_single(raw: dict[str, Any]) -> SapPrItemLine | None:
    """Single-service fallback when ``to_Services`` is empty."""
    if not isinstance(raw, dict):
        return None
    props = raw
    item_no = _norm(props.get("PRItem"))
    if not item_no:
        return None
    service_performer = _norm(props.get("ServiceNo"))
    accts: list[SapPrAcctRow] = []
    cc = _norm(props.get("CostCenter"))
    qty = _norm(props.get("Quantity"))
    if cc:
        accts.append(SapPrAcctRow(seq="01", cost_center=cc, quantity=qty or "1"))
    return SapPrItemLine(
        item_no=item_no.lstrip("0") or item_no,
        sap_pr_item=item_no.lstrip("0") or item_no,
        material="",
        plant=_norm(props.get("Plant")),
        sloc=_norm(props.get("SLoc")),
        material_group=_norm(props.get("MaterialGroup")),
        qty=qty or "1.000",
        unit="EA",
        price=_norm(props.get("ValuationPrice")),
        pur_group=_norm(props.get("PurGroup")),
        pur_org=_norm(props.get("PurOrg")),
        item_cat=_norm(props.get("ItemCat")),
        acct_cat=_norm(props.get("ActAssignmentCat")),
        short_text=strip_pr_item_text_marker(_norm(props.get("ShortText")))[:80],
        service_performer=service_performer,
        fixed_supplier=_norm(props.get("FixedVendor")),
        delivery_date=odata_date_to_form(props.get("DeliveryDate")),
        acct_rows=accts,
    )


def _parse_z_pr_service_cluster(
    props: dict[str, Any],
    cluster: list[dict[str, Any]],
    *,
    sap_item_no: str,
    ui_item_no: str,
) -> SapPrItemLine | None:
    from app.procurement.sap_odata_utils import odata_entity_properties, odata_text
    from app.procurement.sap_pr_z_payload import normalize_service_performer_code

    service_performer = ""
    accts: list[SapPrAcctRow] = []
    short_text = ""
    for svc in cluster:
        if not isinstance(svc, dict):
            continue
        sp = odata_entity_properties(svc)
        code = normalize_service_performer_code(
            _norm(sp.get("Service")) or _norm(sp.get("ServiceNumber"))
        )
        if code and not service_performer:
            service_performer = code
        if not short_text:
            short_text = strip_pr_item_text_marker(odata_text(sp.get("ShortText")))[:80]
        cc = _norm(sp.get("CostCenter"))
        qty = _norm(sp.get("DistrQuantity")) or _norm(sp.get("Quantity"))
        if cc:
            accts.append(
                SapPrAcctRow(
                    seq=_norm(sp.get("PRAcctAssgmtNumber")),
                    cost_center=cc,
                    quantity=qty or "1",
                )
            )
    if not service_performer:
        service_performer = _norm(props.get("ServiceNo"))
    return SapPrItemLine(
        item_no=ui_item_no,
        sap_pr_item=sap_item_no,
        material="",
        plant=_norm(props.get("Plant")),
        sloc=_norm(props.get("SLoc")),
        material_group=_norm(props.get("MaterialGroup")),
        qty=_norm(props.get("Quantity")) or "1.000",
        unit="EA",
        price=_norm(props.get("ValuationPrice")),
        pur_group=_norm(props.get("PurGroup")),
        pur_org=_norm(props.get("PurOrg")),
        item_cat=_norm(props.get("ItemCat")),
        acct_cat=_norm(props.get("ActAssignmentCat")),
        short_text=short_text or strip_pr_item_text_marker(_norm(props.get("ShortText")))[:80],
        service_performer=service_performer,
        fixed_supplier=_norm(props.get("FixedVendor")),
        delivery_date=odata_date_to_form(props.get("DeliveryDate")),
        acct_rows=accts,
    )


async def fetch_pr_items(
    *,
    pr_number: str,
    ticket_id: str = "read-pr-items",
    document_type: str = "",
) -> tuple[list[SapPrItemLine], str | None]:
    """Load active PR lines + account assignments from SAP (header GET + deferred nav)."""
    if not sap_pr_configured():
        return [], "SAP credentials not configured"
    pr_number = _norm(pr_number)
    if not pr_number:
        return [], "PR number is required"

    dt = (document_type or "").upper()
    if dt == "YSER":
        from app.procurement.sap_pr_z_client import get_yser_pr

        body, err = await get_yser_pr(pr_number=pr_number, ticket_id=ticket_id)
        if err or not body:
            return [], err or "SAP YSER PR read failed"
        root = body.get("d") if isinstance(body.get("d"), dict) else body
        if not isinstance(root, dict):
            return [], "SAP YSER PR read returned unexpected body"
        out: list[SapPrItemLine] = []
        ui_line_no = 10
        for row in odata_results_list(root.get("to_Items")):
            parsed = _parse_z_pr_item_row(row, ui_line_start=ui_line_no)
            out.extend(parsed)
            if parsed:
                ui_line_no += 10 * len(parsed)
    else:
        body, err = await get_pr(pr_number=pr_number, ticket_id=ticket_id)
        if err or not body:
            return [], err or "SAP PR read failed"

        root = body.get("d") if isinstance(body.get("d"), dict) else body
        if not isinstance(root, dict):
            return [], "SAP PR read returned unexpected body"

        out = []
        for row in odata_results_list(root.get("to_PurchaseReqnItem")):
            parsed = _parse_pr_item_row(row)
            if parsed:
                out.append(parsed)

    out.sort(key=lambda x: int(x.item_no or "0"))
    if not out:
        return [], f"SAP PR {pr_number} has no items"
    return out, None
