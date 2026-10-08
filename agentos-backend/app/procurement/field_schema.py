"""PR/PO form metadata: header + material/service **blocks** + cost-centre **allocations** (Phase 1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.procurement.line_catalog_group import (
    catalog_group_field,
    ensure_form_line_catalog_groups,
    line_catalog_group,
)
from app.procurement.line_tax_code import ensure_form_line_tax_codes, line_tax_code
from app.procurement.reference_query import storage_location_matches_plant
from app.procurement.sap_config import effective_sap_max_attempts
from app.procurement.sap_yast_acct import asset_codes_equal

Widget = Literal["text", "textarea", "number", "date", "select", "search"]

Scope = Literal["header", "block", "allocation"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    scope: Scope
    widget: Widget
    reference_domain: str | None = None
    required_pr: bool = False
    required_po: bool = False
    help_text: str | None = None
    max_length: int | None = None
    ui_visible: bool = True
    po_only: bool = False
    pr_only: bool = False


DOCUMENT_TYPE_LABELS: dict[str, str] = {
    "YSER": "Service",
    "YUNB": "Consumable",
    "YAST": "Asset",
}

DOCUMENT_TYPE_CODES: tuple[str, ...] = ("YSER", "YUNB", "YAST")

# Every header key we persist (including SAP-only fields filled in ``apply_procurement_defaults``).
_HEADER_KEYS_ORDER: tuple[str, ...] = (
    "purchasing_org",
    "company_code",
    "purchasing_doc_type",
    "payment_terms",
    "purchasing_group",
    "plant",
    "storage_location",
    "tax_code",
    "tax_jurisdiction",
    "vendor",
    "header_note",
    "requestor_email",
    "po_remarks",
    "po_deadlines",
    "po_terms_of_delivery",
    "material_group",
    "service_group",
)

PO_REQUESTOR_EMAIL_MAX_LEN = 30
PO_INCOTERMS_LOCATION2_MAX_LEN = 70
# PR PurReqnDescription (max 40 on QAS). PO CorrespncInternalReference is max 12 on QAS (EKKO-VERKF).
HEADER_NOTE_PR_MAX_LEN = 40
HEADER_NOTE_PO_MAX_LEN = 12
HEADER_NOTE_MAX_LEN = HEADER_NOTE_PR_MAX_LEN  # legacy alias for PR paths
PO_TEXT_FIELD_MAX_LEN = 2500

PO_TEXT_HEADER_KEYS: tuple[str, ...] = (
    "po_remarks",
    "po_deadlines",
    "po_terms_of_delivery",
)


def _h(
    key: str,
    label: str,
    widget: Widget,
    ref: str | None = None,
    *,
    req_pr: bool = False,
    req_po: bool = False,
    help_text: str | None = None,
    max_length: int | None = None,
    ui_visible: bool = True,
    po_only: bool = False,
    pr_only: bool = False,
) -> FieldSpec:
    return FieldSpec(
        key=key,
        label=label,
        scope="header",
        widget=widget,
        reference_domain=ref,
        required_pr=req_pr,
        required_po=req_po,
        help_text=help_text,
        max_length=max_length,
        ui_visible=ui_visible,
        po_only=po_only,
        pr_only=pr_only,
    )


def _b(
    key: str,
    label: str,
    widget: Widget,
    ref: str | None = None,
    *,
    req_pr: bool = False,
    req_po: bool = False,
    help_text: str | None = None,
    ui_visible: bool = True,
) -> FieldSpec:
    return FieldSpec(
        key=key,
        label=label,
        scope="block",
        widget=widget,
        reference_domain=ref,
        required_pr=req_pr,
        required_po=req_po,
        help_text=help_text,
        ui_visible=ui_visible,
    )


def _a(
    key: str,
    label: str,
    widget: Widget,
    ref: str | None = None,
    *,
    req_pr: bool = False,
    req_po: bool = False,
    help_text: str | None = None,
) -> FieldSpec:
    return FieldSpec(
        key=key,
        label=label,
        scope="allocation",
        widget=widget,
        reference_domain=ref,
        required_pr=req_pr,
        required_po=req_po,
        help_text=help_text,
        ui_visible=True,
    )


def header_field_specs(document_type: str) -> list[FieldSpec]:
    dt = (document_type or "").upper()
    common: list[FieldSpec] = [
        _h(
            "purchasing_org",
            "Purchasing organization",
            "select",
            "purchasing_org",
            req_pr=True,
            req_po=True,
            help_text="Select the org that receives the invoice. Company code is set to match for SAP.",
        ),
        _h(
            "plant",
            "Plant",
            "select",
            "plant",
            req_pr=True,
            req_po=True,
            help_text="Plants starting with H/L/T apply to 1MGH/1LFS/1MGT; other codes apply to all orgs.",
        ),
    ]
    # Legacy: header group kept for DB / old tickets; UI uses per-line group (block_fields).
    if dt == "YSER":
        common.append(
            _h(
                "service_group",
                "Service group",
                "select",
                "service_group",
                req_pr=False,
                req_po=False,
                ui_visible=False,
            ),
        )
    if dt in ("YUNB", "YAST"):
        common.append(
            _h(
                "material_group",
                "Material group",
                "select",
                "material_group",
                req_pr=False,
                req_po=False,
                ui_visible=False,
            ),
        )
    common.extend(
        [
            _h("purchasing_group", "Purchasing group", "select", "purchasing_group", req_pr=True, req_po=True),
            _h(
                "storage_location",
                "Storage location",
                "select",
                "storage_location",
                req_pr=True,
                req_po=True,
            ),
            _h(
                "tax_code",
                "Tax code",
                "select",
                "tax_code",
                req_pr=False,
                req_po=False,
                ui_visible=False,
            ),
            _h(
                "vendor",
                "Vendor",
                "search",
                "vendor",
                req_pr=False,
                req_po=True,
                help_text="Required for purchase orders.",
            ),
            _h(
                "header_note",
                "Header note",
                "textarea",
                None,
                req_pr=False,
                req_po=False,
                help_text=(
                    f"Sent to SAP header description (PR max {HEADER_NOTE_PR_MAX_LEN}, "
                    f"PO max {HEADER_NOTE_PO_MAX_LEN} characters). "
                    "PR: PurReqnDescription; PO: CorrespncInternalReference."
                ),
                max_length=HEADER_NOTE_PR_MAX_LEN,
            ),
            _h(
                "requestor_email",
                "Requestor email id",
                "text",
                None,
                req_pr=False,
                req_po=False,
                help_text="Sent to SAP as salesperson on the PO (max 30 characters). Defaults to your login email.",
                po_only=True,
            ),
            _h(
                "po_remarks",
                "PO remarks",
                "textarea",
                None,
                req_pr=False,
                req_po=False,
                help_text=f"Long text sent to SAP Remarks (max {PO_TEXT_FIELD_MAX_LEN} characters).",
                po_only=True,
            ),
            _h(
                "po_deadlines",
                "PO deadlines",
                "textarea",
                None,
                req_pr=False,
                req_po=False,
                help_text=f"Long text sent to SAP Deadlines (max {PO_TEXT_FIELD_MAX_LEN} characters).",
                po_only=True,
            ),
            _h(
                "po_terms_of_delivery",
                "PO terms of delivery",
                "textarea",
                None,
                req_pr=False,
                req_po=False,
                help_text=f"Long text sent to SAP TermsOfDelivery (max {PO_TEXT_FIELD_MAX_LEN} characters).",
                po_only=True,
            ),
        ]
    )
    return common


def _line_tax_block_field() -> FieldSpec:
    return _b(
        "tax_code",
        "Tax code",
        "select",
        "tax_code",
        req_pr=False,
        req_po=True,
        help_text="Tax code for this line. Optional on purchase requests; required on purchase orders.",
    )


def block_field_specs(document_type: str) -> list[FieldSpec]:
    dt = (document_type or "").upper()
    if dt == "YSER":
        return [
            _b(
                "service_group",
                "Service group",
                "select",
                "service_group",
                req_pr=True,
                req_po=True,
                help_text="Narrows the service catalogue for this line.",
            ),
            _b("service", "Service", "search", "service", req_pr=True, req_po=True),
            _b(
                "short_text",
                "Service short text (SAP)",
                "textarea",
                None,
                req_pr=True,
                req_po=True,
                help_text="Filled from the service catalogue when you pick a service; shown under the service field.",
                ui_visible=False,
            ),
            _b(
                "delivery_date",
                "Delivery / service date",
                "date",
                None,
                req_pr=True,
                req_po=True,
                help_text="Approximate dates are acceptable.",
            ),
            _line_tax_block_field(),
            _b(
                "unit_price",
                "Unit price (without GST)",
                "number",
                None,
                req_pr=True,
                req_po=True,
            ),
            _b(
                "order_unit",
                "Service unit (SAP)",
                "select",
                "order_unit",
                req_pr=False,
                req_po=False,
                help_text="Set from service master or PR prefill; default EA if missing.",
                ui_visible=False,
            ),
            _b(
                "valuation_price",
                "Valuation (PR) / total basis",
                "number",
                None,
                req_pr=False,
                req_po=False,
                help_text="Calculated from unit price × allocation quantities; shown for review before send.",
            ),
        ]
    # YUNB / YAST — material blocks
    out: list[FieldSpec] = [
        _b(
            "material_group",
            "Material group",
            "select",
            "material_group",
            req_pr=True,
            req_po=True,
            help_text="Narrows materials for this line.",
        ),
        _b("material", "Material", "search", "material", req_pr=True, req_po=True),
        _b(
            "short_text",
            "Material description (SAP)",
            "textarea",
            None,
            req_pr=True,
            req_po=True,
            help_text="Filled from the material catalogue when you pick a material; shown under the material field.",
            ui_visible=False,
        ),
        _b(
            "order_unit",
            "PO unit (SAP)",
            "select",
            "order_unit",
            req_pr=False,
            req_po=False,
            help_text="Set from material master when the ticket is saved; default QT if missing.",
            ui_visible=False,
        ),
        _b(
            "delivery_date",
            "Delivery date",
            "date",
            None,
            req_pr=True,
            req_po=True,
            help_text="Approximate dates are acceptable.",
        ),
        _line_tax_block_field(),
        _b(
            "unit_price",
            "Unit price (without GST)",
            "number",
            None,
            req_pr=True,
            req_po=True,
        ),
        _b(
            "valuation_price",
            "Line total (unit × qty)",
            "number",
            None,
            req_pr=False,
            req_po=False,
            help_text="Calculated from unit price × allocation quantities; shown for review before send.",
        ),
    ]
    if dt == "YAST":
        # Line-level asset kept for SAP/API back-compat (single-asset forms);
        # UI edits assets on allocation rows ("+ Another asset").
        out.append(
            _b(
                "asset",
                "Asset",
                "search",
                "asset",
                req_pr=False,
                req_po=False,
                ui_visible=False,
                help_text=(
                    "Mirrored from the first allocation asset (SAP account assignment "
                    "category A). Prefer entering assets on allocation rows."
                ),
            )
        )
    return out


def allocation_field_specs(document_type: str) -> list[FieldSpec]:
    dt = (document_type or "").upper()
    if dt == "YAST":
        return [
            _a(
                "asset",
                "Asset",
                "search",
                "asset",
                req_pr=True,
                req_po=True,
                help_text=(
                    "Search by asset number or description. Scoped to the purchasing "
                    "organisation (company code). Each split can use a different asset."
                ),
            ),
            _a(
                "qty",
                "Quantity",
                "number",
                None,
                req_pr=True,
                req_po=True,
                help_text="Quantity for this asset split.",
            ),
        ]
    return [
        _a(
            "cost_center",
            "Cost center",
            "search",
            "cost_center",
            req_pr=True,
            req_po=True,
            help_text="Search by code or name; use Advanced search when unsure.",
        ),
        _a(
            "qty",
            "Quantity",
            "number",
            None,
            req_pr=True,
            req_po=True,
            help_text=(
                "Quantity for this cost centre (decimals allowed for SAP splits)."
                if dt == "YSER"
                else "Whole units only for this cost centre."
            ),
        ),
    ]


def field_specs(document_type: str) -> list[FieldSpec]:
    """All specs (header + block + allocation) for validation / internal use."""
    dt = (document_type or "").upper()
    return header_field_specs(dt) + block_field_specs(dt) + allocation_field_specs(dt)


def header_specs_for_schema(document_type: str, *, kind: str | None = None) -> list[FieldSpec]:
    kind_u = (kind or "").upper()
    out: list[FieldSpec] = []
    for spec in header_field_specs(document_type):
        if not spec.ui_visible:
            continue
        if spec.po_only and kind_u != "PO":
            continue
        if spec.pr_only and kind_u != "PR":
            continue
        out.append(spec)
    return out


def schema_payload() -> dict[str, Any]:
    out: dict[str, Any] = {
        "document_types": [],
        "sap_max_attempts": effective_sap_max_attempts(),
    }
    for code, label in DOCUMENT_TYPE_LABELS.items():
        pr_headers = header_specs_for_schema(code, kind="PR")
        po_headers = header_specs_for_schema(code, kind="PO")
        po_headers_out = []
        for spec in po_headers:
            d = asdict(spec)
            if spec.key == "header_note":
                d["max_length"] = HEADER_NOTE_PO_MAX_LEN
                d["help_text"] = (
                    f"Sent to SAP PO header note (max {HEADER_NOTE_PO_MAX_LEN} characters). "
                    "Mapped to CorrespncInternalReference."
                )
            po_headers_out.append(d)
        out["document_types"].append(
            {
                "code": code,
                "label": label,
                "header_fields": [asdict(s) for s in pr_headers],
                "po_header_fields": po_headers_out,
                "block_fields": [asdict(s) for s in block_field_specs(code)],
                "allocation_fields": [asdict(s) for s in allocation_field_specs(code)],
                "line_fields": [],
            }
        )
    return out


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def _is_positive_int(s: str) -> bool:
    if not s:
        return False
    try:
        n = int(s, 10)
        return n > 0
    except ValueError:
        return False


def _is_valid_requestor_email(val: str, *, max_len: int = PO_REQUESTOR_EMAIL_MAX_LEN) -> bool:
    """Optional PO requestor email — simple address shape (length checked separately)."""
    s = _norm(val)
    if not s:
        return True
    if len(s) > max_len:
        return False
    if "@" not in s:
        return False
    local, _, domain = s.partition("@")
    return bool(local and domain and "." in domain)


def _is_positive_qty(s: str) -> bool:
    """Allocation qty — positive whole or decimal (YSER SAP splits e.g. 0.999 + 2.001)."""
    if not s:
        return False
    try:
        return float(s.replace(",", ".")) > 0
    except ValueError:
        return False


def validate_form(*, kind: str, document_type: str, form: dict[str, Any]) -> list[str]:
    dt = (document_type or "").upper()
    if dt not in DOCUMENT_TYPE_LABELS:
        return [f"Unknown document type: {document_type}"]
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    lines = form.get("lines")
    if not isinstance(lines, list) or len(lines) == 0:
        return ["Add at least one material or service block."]
    kind_u = kind.upper()
    errs: list[str] = []

    for spec in header_field_specs(dt):
        if not spec.ui_visible:
            continue
        if spec.po_only and kind_u != "PO":
            continue
        if spec.pr_only and kind_u != "PR":
            continue
        val = _norm(header.get(spec.key))
        req = spec.required_pr if kind_u == "PR" else spec.required_po
        if req and not val:
            errs.append(f"Header field “{spec.label}” is required.")
        if spec.max_length is not None and len(val) > spec.max_length:
            errs.append(
                f"{spec.label} must be at most {spec.max_length} characters."
            )
        if spec.key == "header_note" and kind_u == "PO" and len(val) > HEADER_NOTE_PO_MAX_LEN:
            errs.append(
                f"{spec.label} must be at most {HEADER_NOTE_PO_MAX_LEN} characters on purchase orders."
            )

    if kind_u == "PO":
        req_email = _norm(header.get("requestor_email"))
        if req_email and not _is_valid_requestor_email(req_email):
            errs.append(
                f"Requestor email id must be a valid email address (max {PO_REQUESTOR_EMAIL_MAX_LEN} characters)."
            )
        for key in PO_TEXT_HEADER_KEYS:
            val = _norm(header.get(key))
            if len(val) > PO_TEXT_FIELD_MAX_LEN:
                label = next(
                    (s.label for s in header_field_specs(dt) if s.key == key),
                    key,
                )
                errs.append(
                    f"{label} must be at most {PO_TEXT_FIELD_MAX_LEN} characters."
                )

    plant = _norm(header.get("plant"))
    sloc = _norm(header.get("storage_location"))
    if plant and sloc and not storage_location_matches_plant(storage_location=sloc, plant=plant):
        errs.append(
            "Storage location must belong to the selected plant "
            f"(use a {plant}|… location, not {sloc})."
        )

    seen_codes: set[str] = set()
    for bi, raw in enumerate(lines):
        if not isinstance(raw, dict):
            errs.append(f"Block {bi + 1}: invalid row.")
            continue
        bix = bi + 1
        if dt == "YSER":
            for spec in block_field_specs(dt):
                if spec.key == catalog_group_field(dt):
                    val = line_catalog_group(raw, header, dt)
                elif spec.key == "tax_code":
                    val = line_tax_code(raw, header)
                else:
                    val = _norm(raw.get(spec.key))
                req = spec.required_pr if kind_u == "PR" else spec.required_po
                if req and not val:
                    errs.append(f"Block {bix}: “{spec.label}” is required.")
        else:
            code = _norm(raw.get("material"))
            if code:
                if code in seen_codes:
                    errs.append(f"Block {bix}: duplicate material is not allowed.")
                seen_codes.add(code)
            for spec in block_field_specs(dt):
                if spec.key == catalog_group_field(dt):
                    val = line_catalog_group(raw, header, dt)
                elif spec.key == "tax_code":
                    val = line_tax_code(raw, header)
                else:
                    val = _norm(raw.get(spec.key))
                req = spec.required_pr if kind_u == "PR" else spec.required_po
                if spec.key == "asset" and dt != "YAST":
                    continue
                if req and not val:
                    errs.append(f"Block {bix}: “{spec.label}” is required.")

        allocs = raw.get("allocations")
        if not isinstance(allocs, list) or len(allocs) == 0:
            if dt == "YAST":
                errs.append(f"Block {bix}: add at least one asset allocation.")
            else:
                errs.append(f"Block {bix}: add at least one cost centre allocation.")
            continue
        cc_seen: set[str] = set()
        asset_seen: set[str] = set()
        for ai, arow in enumerate(allocs):
            if not isinstance(arow, dict):
                errs.append(f"Block {bix}, allocation {ai + 1}: invalid row.")
                continue
            aix = ai + 1
            for spec in allocation_field_specs(dt):
                val = _norm(arow.get(spec.key))
                req = spec.required_pr if kind_u == "PR" else spec.required_po
                if req and not val:
                    errs.append(f"Block {bix}, allocation {aix}: “{spec.label}” is required.")
            cc = _norm(arow.get("cost_center"))
            if dt != "YAST":
                if cc and cc in cc_seen:
                    errs.append(f"Block {bix}, allocation {aix}: duplicate cost centre.")
                if cc:
                    cc_seen.add(cc)
            else:
                asset = _norm(arow.get("asset"))
                if asset and any(asset_codes_equal(asset, seen) for seen in asset_seen):
                    errs.append(f"Block {bix}, allocation {aix}: duplicate asset.")
                if asset:
                    asset_seen.add(asset)
            qv = _norm(arow.get("qty"))
            if qv and not _is_positive_qty(qv):
                errs.append(f"Block {bix}, allocation {aix}: quantity must be a positive number.")

    if dt == "YSER":
        from app.procurement.sap_pr_z_payload import validate_yser_form_service_refs_unique

        dup = validate_yser_form_service_refs_unique(form)
        if dup:
            errs.append(dup)

    return errs


def default_empty_block(document_type: str) -> dict[str, Any]:
    dt = (document_type or "").upper()
    lk: Literal["material", "service"] = "service" if dt == "YSER" else "material"
    row: dict[str, Any] = {
        "line_kind": lk,
        "material_group": "",
        "service_group": "",
        "material": "",
        "service": "",
        "short_text": "",
        "order_unit": "EA" if dt == "YSER" else "QT",
        "unit_price": "",
        "valuation_price": "",
        "delivery_date": "",
        "tax_code": "",
        "asset": "",
        "account_assignment_cat": "",
        "item_category": "",
        "net_price": "",
        "gross_price": "",
        "allocations": (
            [{"asset": "", "qty": ""}] if dt == "YAST" else [{"cost_center": "", "qty": ""}]
        ),
    }
    return row


# SAP / PR-link fields not shown in UI but required on PO payloads and YSER update merge.
_SAP_PASSTHROUGH_BLOCK_KEYS: tuple[str, ...] = (
    "purchase_requisition_item",
    "purchase_order_item",
    "sap_pr_item",
    "sap_pr_service_ref",
    "pr_acct_assgmt_number",
    "sap_po_service_ref",
    "account_assignment_cat",
    "item_category",
    "net_price",
    "gross_price",
)

_SAP_PASSTHROUGH_ALLOC_KEYS: tuple[str, ...] = (
    "pr_acct_assgmt_number",
    "po_acct_assgmt_number",
)


def _normalize_yast_allocations(
    row: dict[str, Any],
    al_in: list[Any] | None,
    keys_alloc: set[str],
) -> list[dict[str, Any]]:
    """YAST splits are asset+qty only (QA never used cost centres on YAST).

    - Keep asset/qty from each allocation row (CC keys are already out of ``keys_alloc``).
    - Promote ``line.asset`` only when allocations have no asset yet.
    - If several rows lack assets (e.g. mistaken CC-shaped data), collapse to **one**
      asset split using ``line.asset`` and the summed qty — never stamp the same asset
      onto every blank row (that invented duplicate-asset validation failures).
    - Mirror first allocation asset → line for SAP back-compat.
    """
    allocs: list[dict[str, Any]] = []
    if isinstance(al_in, list) and al_in:
        for a in al_in:
            ad: dict[str, Any] = {k: "" for k in keys_alloc}
            if isinstance(a, dict):
                for k in keys_alloc:
                    if k in a:
                        vv = a.get(k)
                        ad[k] = "" if vv is None else vv
            allocs.append(ad)
    else:
        allocs = [{k: "" for k in keys_alloc}]

    line_asset = _norm(row.get("asset"))
    any_alloc_asset = any(_norm(a.get("asset")) for a in allocs)

    if not any_alloc_asset and line_asset:
        qty_sum = 0.0
        qty_raw = ""
        saw_qty = False
        for a in allocs:
            q = _norm(a.get("qty"))
            if not q:
                continue
            saw_qty = True
            qty_raw = q if not qty_raw else qty_raw
            try:
                qty_sum += float(q.replace(",", "."))
            except ValueError:
                pass
        if saw_qty and qty_sum > 0:
            qty_out = (
                str(int(qty_sum))
                if abs(qty_sum - int(qty_sum)) < 1e-9
                else str(qty_sum)
            )
        else:
            qty_out = qty_raw
        allocs = [{"asset": line_asset, "qty": qty_out}]
    elif not any_alloc_asset and not line_asset and len(allocs) > 1:
        # No assets anywhere — keep a single empty row for the UI (never N CC ghosts).
        qty0 = _norm(allocs[0].get("qty")) if allocs else ""
        allocs = [{"asset": "", "qty": qty0}]
    else:
        # Real multi-asset (or single blank): fill blank asset cells from line only
        # when there is exactly one allocation (single-asset back-compat).
        if line_asset and len(allocs) == 1 and not _norm(allocs[0].get("asset")):
            allocs[0]["asset"] = line_asset

    first_asset = next((_norm(a.get("asset")) for a in allocs if _norm(a.get("asset"))), "")
    if first_asset:
        row["asset"] = first_asset
    return allocs


def normalize_form(document_type: str, form: dict[str, Any]) -> dict[str, Any]:
    dt = (document_type or "").upper()
    header_in = form.get("header") if isinstance(form.get("header"), dict) else {}
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []

    header: dict[str, Any] = {k: "" for k in _HEADER_KEYS_ORDER}
    for k in _HEADER_KEYS_ORDER:
        v = header_in.get(k)
        header[k] = "" if v is None else v

    lines: list[dict[str, Any]] = []
    tmpl = default_empty_block(dt)
    keys_block = {s.key for s in block_field_specs(dt)}
    keys_alloc = {s.key for s in allocation_field_specs(dt)}

    for raw in lines_in:
        row = {**tmpl}
        if isinstance(raw, dict):
            for k in keys_block:
                if k in raw:
                    v = raw.get(k)
                    row[k] = "" if v is None else v
            row["line_kind"] = str(raw.get("line_kind") or tmpl["line_kind"]).strip() or tmpl["line_kind"]
            for k in _SAP_PASSTHROUGH_BLOCK_KEYS:
                if k in raw:
                    v = raw.get(k)
                    row[k] = "" if v is None else v
            al_in = raw.get("allocations")
            if dt == "YAST":
                row["allocations"] = _normalize_yast_allocations(
                    row, al_in if isinstance(al_in, list) else None, keys_alloc
                )
            else:
                allocs: list[dict[str, Any]] = []
                if isinstance(al_in, list) and al_in:
                    for a in al_in:
                        ad = {k: "" for k in keys_alloc}
                        if isinstance(a, dict):
                            for k in keys_alloc:
                                if k in a:
                                    vv = a.get(k)
                                    ad[k] = "" if vv is None else vv
                            for k in _SAP_PASSTHROUGH_ALLOC_KEYS:
                                if k in a:
                                    vv = a.get(k)
                                    ad[k] = "" if vv is None else vv
                        allocs.append(ad)
                else:
                    allocs = [{k: "" for k in keys_alloc}]
                row["allocations"] = allocs
        lines.append(row)

    if not lines:
        lines = [default_empty_block(dt)]
    out = {"header": header, "lines": lines}
    ensure_form_line_catalog_groups(out, dt)
    ensure_form_line_tax_codes(out)
    return out
