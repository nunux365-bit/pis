"""Map AgentOS forms to SAP ``ZAPI_PURCHASEORDER_PROCESS_SRV`` (YSER service PO).

Form-driven like YUNB/YAST: units, payment terms, tax, and acct cat come from the
normalized ticket form (reference master / PR prefill / user edits). Z API shape
(``to_Services``, service-level account assignment) is YSER-specific.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.procurement.sap_odata_utils import (
    is_sap_deleted_flag,
    odata_date_to_form,
    odata_entity_properties,
    odata_norm as _norm,
    odata_results_list,
    odata_text,
    storage_location_for_sap,
)
from app.procurement.field_schema import PO_INCOTERMS_LOCATION2_MAX_LEN
from app.procurement.line_catalog_group import line_catalog_group, sync_header_catalog_group
from app.procurement.line_tax_code import (
    line_tax_code,
    sync_header_tax_code,
    yser_po_group_item_tax_code,
    yser_po_group_tax_differs_from_sap,
)
from app.procurement.sap_po_payload import (
    apply_po_correspnc_internal_reference,
    build_po_correspnc_internal_reference,
    build_po_item_text_for_sap,
    format_po_purchase_requisition_item,
    po_requestor_email_for_sap,
    _iter_po_form_lines,
    _net_price_amount,
    _payment_terms_for_payload,
    _tax_jurisdiction_for_po_item,
)
from app.procurement.sap_pr_payload import (
    SapAcctSegment,
    _company_code,
    _fixed_supplier,
    _sap_date,
    normalize_pr_item_number,
    sap_ticket_ext_system_marker,
)
from app.procurement.sap_pr_z_payload import (
    _format_z_qty,
    _item_total_quantity,
    _service_uom,
    cluster_yser_pr_service_entries,
    normalize_service_performer_code,
    yser_effective_line_blocks,
    yser_group_blocks_by_service_group,
    yser_sap_layout_is_grouped,
)

Z_PO_SERVICE_PATH = "/sap/opu/odata/sap/ZAPI_PURCHASEORDER_PROCESS_SRV/"
Z_PO_HEADER_COLLECTION = "/sap/opu/odata/sap/ZAPI_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"

PO_SERVICES_NAV = "to_Services"
PO_SERVICE_ACCT_NAV = "to_AccountAssignment"

# UI ``delivery_date`` maps to PO item ``DeliveryDate`` (QAS: ``DD.MM.YYYY`` string).
# ``to_Services[].ServicePerformanceDate`` is also sent when set (often empty on QAS read).
# Material PO uses ``to_ScheduleLine``; Z YSER PR uses item ``DeliveryDate`` YYYYMMDD.


def _yser_line_unit(block: dict[str, Any]) -> str:
    """Line UoM from ``order_unit`` (service master / PR prefill), default EA like Z PR."""
    return _service_uom(block)


def _z_po_item_delivery_date_to_form(raw: Any) -> str:
    """SAP item ``DeliveryDate`` (QAS ``DD.MM.YYYY`` or OData) → form ``YYYY-MM-DD``."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return odata_date_to_form(raw)
    s = _norm(raw)
    if not s:
        return ""
    if len(s) == 10 and s[2] == "." and s[5] == ".":
        dd, mm, yyyy = s.split(".")
        if len(yyyy) == 4 and yyyy.isdigit() and mm.isdigit() and dd.isdigit():
            return f"{yyyy}-{mm}-{dd}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return odata_date_to_form(s)


def _z_po_item_delivery_date_for_sap(raw: str) -> str:
    """Form ``YYYY-MM-DD`` → SAP item ``DeliveryDate`` ``DD.MM.YYYY`` (max 10)."""
    s = _norm(raw)
    if not s:
        return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}.{s[5:7]}.{s[:4]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[6:8]}.{s[4:6]}.{s[:4]}"
    if len(s) == 10 and s[2] == "." and s[5] == ".":
        return s
    return s[:10]


def _z_po_line_delivery_from_snap(
    snap: "SapZPoItemSnapshot",
    svc: "SapZPoServiceSnapshot",
) -> str:
    """Prefer item ``DeliveryDate``; fall back to service ``ServicePerformanceDate``."""
    item_dd = _norm(snap.delivery_date)
    if item_dd:
        return item_dd
    return _norm(svc.service_performance_date)


def _form_allocation_qty(qty: str | float) -> str:
    """Map SAP allocation qty to UI form (whole or decimal — same rules as YSER PR read)."""
    try:
        v = float(str(qty).strip().replace(",", "."))
    except ValueError:
        return "1"
    if v <= 0:
        return "1"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return text or "1"


def _yser_po_acct_cat(block: dict[str, Any]) -> str:
    """Account assignment category from form or ``K`` when cost centres are present."""
    explicit = _norm(block.get("account_assignment_cat"))
    if explicit:
        return explicit
    allocs = block.get("allocations")
    if isinstance(allocs, list) and any(
        isinstance(a, dict) and _norm(a.get("cost_center")) for a in allocs
    ):
        return "K"
    return ""


def z_po_service_root_url(base_url: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root

    return f"{odata_base_root(base_url)}{Z_PO_SERVICE_PATH}"


def z_po_collection_url(base_url: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root

    return f"{odata_base_root(base_url)}{Z_PO_HEADER_COLLECTION}"


def z_po_entity_url(base_url: str, po_number: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root, odata_entity_key

    key = odata_entity_key(po_number)
    if not key:
        raise ValueError("PO number is required for Z PO entity URL")
    return f"{odata_base_root(base_url)}{Z_PO_HEADER_COLLECTION}('{key}')"


def z_po_read_url(base_url: str, po_number: str) -> str:
    expand = (
        f"to_PurchaseOrderItem/{PO_SERVICES_NAV}/{PO_SERVICE_ACCT_NAV}"
    )
    return f"{z_po_entity_url(base_url, po_number)}?$expand={expand}&$format=json"


def z_po_header_read_url(base_url: str, po_number: str) -> str:
    """Lightweight Z GET for header long texts (Remarks / Deadlines / TermsOfDelivery)."""
    return f"{z_po_entity_url(base_url, po_number)}?$format=json"


# Form header key → SAP Z PO header field.
PO_TEXT_SAP_FIELDS: tuple[tuple[str, str], ...] = (
    ("po_remarks", "Remarks"),
    ("po_deadlines", "Deadlines"),
    ("po_terms_of_delivery", "TermsOfDelivery"),
)


def po_creator_mail_id_for_sap(creator_email: str | None) -> str:
    """YSER ticket creator email on header ``CreatorMailId`` (max 70)."""
    return _norm(creator_email)[:PO_INCOTERMS_LOCATION2_MAX_LEN]


def _apply_yser_po_creator_header_field(
    inner: dict[str, Any],
    *,
    creator_email: str | None = None,
) -> None:
    """Header only — never on ``to_PurchaseOrderItem``."""
    mail_id = po_creator_mail_id_for_sap(creator_email)
    if mail_id:
        inner["CreatorMailId"] = mail_id


def _po_text_max_len() -> int:
    from app.procurement.field_schema import PO_TEXT_FIELD_MAX_LEN

    return PO_TEXT_FIELD_MAX_LEN


def po_text_fields_for_sap(header: dict[str, Any]) -> dict[str, str]:
    """Map form header → SAP Z text fields (non-empty only)."""
    max_len = _po_text_max_len()
    out: dict[str, str] = {}
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        val = _norm(header.get(form_key))[:max_len]
        if val:
            out[sap_key] = val
    return out


def apply_yser_po_texts_to_z_inner(inner: dict[str, Any], header: dict[str, Any]) -> None:
    """YSER Z PO long texts + ``header_note`` → ``CorrespncInternalReference``."""
    from app.procurement.sap_po_payload import apply_po_correspnc_internal_reference

    max_len = _po_text_max_len()
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        inner[sap_key] = _norm(header.get(form_key))[:max_len]
    apply_po_correspnc_internal_reference(inner, header)


def po_text_fields_from_sap_root(root: dict[str, Any]) -> dict[str, str]:
    """Map SAP Z header → form header keys."""
    out: dict[str, str] = {}
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        val = _norm(odata_text(root.get(sap_key)))
        if val:
            out[form_key] = val
    return out


def po_texts_differ(header: dict[str, Any], sap_root: dict[str, Any], *, document_type: str = "") -> bool:
    del document_type
    max_len = _po_text_max_len()
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        exp = _norm(header.get(form_key))[:max_len]
        sap_val = _norm(odata_text(sap_root.get(sap_key)))
        if exp != sap_val:
            return True
    return False


def any_po_text_in_form(header: dict[str, Any]) -> bool:
    return bool(po_text_fields_for_sap(header))


def apply_po_texts_to_z_inner(inner: dict[str, Any], header: dict[str, Any]) -> None:
    max_len = _po_text_max_len()
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        inner[sap_key] = _norm(header.get(form_key))[:max_len]


def build_z_po_texts_post_body(
    *,
    po_number: str,
    header: dict[str, Any],
) -> dict[str, Any]:
    """Z POST body for PO long texts (YUNB/YAST follow-up or texts-only update)."""
    po_key = _norm(po_number)
    if not po_key:
        raise ValueError("PO number is required for Z PO texts POST")
    max_len = _po_text_max_len()
    inner: dict[str, Any] = {
        "PurchaseOrder": po_key,
        "to_PurchaseOrderItem": [],
    }
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        inner[sap_key] = _norm(header.get(form_key))[:max_len]
    return _wrap_z_po_nav_lists(inner)


def verify_po_texts_against_form(
    root: dict[str, Any] | None,
    *,
    header: dict[str, Any],
    document_type: str = "",
) -> list[str]:
    del document_type
    if not isinstance(root, dict):
        return []
    max_len = _po_text_max_len()
    mismatches: list[str] = []
    for form_key, sap_key in PO_TEXT_SAP_FIELDS:
        exp = _norm(header.get(form_key))[:max_len]
        sap_val = _norm(odata_text(root.get(sap_key)))
        if sap_val != exp:
            mismatches.append(f"{sap_key} SAP {sap_val!r} != form {exp!r}")
    return mismatches


def format_z_po_item_number(item_number: int | str) -> str:
    try:
        return str(int(str(item_number).lstrip("0") or "0")).zfill(5)
    except ValueError:
        return str(item_number).zfill(5)


def _z_po_payment_terms(header: dict[str, Any]) -> str:
    """Same rule as YUNB/YAST — only when the form (or PR prefill) has a value."""
    return _payment_terms_for_payload(header)


def _unit_price_from_block(block: dict[str, Any]) -> float:
    for key in ("unit_price", "net_price", "valuation_price", "gross_price"):
        raw = _norm(block.get(key))
        if not raw:
            continue
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            continue
    return 0.0


def _line_total_amount(block: dict[str, Any], *, qty_total: float) -> str:
    unit = _unit_price_from_block(block)
    if unit <= 0:
        raw = _net_price_amount(block, kind="PO")
        if raw:
            try:
                return f"{float(raw.replace(',', '.')):.3f}"
            except ValueError:
                return raw
        return "0.000"
    total = unit * (qty_total if qty_total > 0 else 1.0)
    return f"{total:.3f}"


def _format_po_z_price(value: float | str) -> str:
    try:
        v = float(str(value).replace(",", "."))
    except ValueError:
        return str(value)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}.000"
    return f"{v:.3f}"


def _alloc_purg_amount(*, unit: float, alloc_qty: float) -> str:
    return _format_po_z_price(unit * alloc_qty if unit > 0 else 0.0)


YSER_PO_SERVICE_EXT_REF_MAX_LEN = 35


def _yser_po_service_external_ref(block: dict[str, Any]) -> str:
    return _norm(block.get("sap_po_service_ref"))[:YSER_PO_SERVICE_EXT_REF_MAX_LEN]


def _ensure_yser_po_service_refs(
    form: dict[str, Any],
    *,
    ticket_id: str | None,
    min_ref_seq: int = 0,
) -> None:
    """Assign stable ``sap_po_service_ref`` per UI line when missing (POST/UPDATE)."""
    import re

    seq = max(0, int(min_ref_seq or 0))
    for block in yser_effective_line_blocks(form):
        if not isinstance(block, dict):
            continue
        ref = _yser_po_service_external_ref(block)
        if ref:
            m = re.search(r"-(\d+)$", ref)
            if m:
                seq = max(seq, int(m.group(1)))
    for block in yser_effective_line_blocks(form):
        if not isinstance(block, dict):
            continue
        if _yser_po_service_external_ref(block):
            continue
        if not ticket_id:
            continue
        seq += 1
        marker = sap_ticket_ext_system_marker(ticket_id) or _norm(ticket_id)
        suffix = f"-{seq:03d}"
        prefix = "AOLINE-"
        max_marker = max(
            1, YSER_PO_SERVICE_EXT_REF_MAX_LEN - len(prefix) - len(suffix)
        )
        block["sap_po_service_ref"] = f"{prefix}{marker[:max_marker]}{suffix}"


def validate_yser_po_service_refs_unique(form: dict[str, Any]) -> str | None:
    """Fail fast when duplicate ``sap_po_service_ref`` would break update identity."""
    seen: dict[str, int] = {}
    for idx, block in enumerate(yser_effective_line_blocks(form), start=1):
        ref = _yser_po_service_external_ref(block)
        if not ref:
            continue
        if ref in seen:
            return (
                f"duplicate sap_po_service_ref {ref!r} on lines {seen[ref]} and {idx}"
            )
        seen[ref] = idx
    return None


def yser_po_max_service_ref_seq_from_form(form: dict[str, Any]) -> int:
    import re

    seq = 0
    for block in yser_effective_line_blocks(form):
        if not isinstance(block, dict):
            continue
        ref = _yser_po_service_external_ref(block)
        if not ref:
            continue
        m = re.search(r"-(\d+)$", ref)
        if m:
            seq = max(seq, int(m.group(1)))
    return seq


def yser_po_item_number_for_block(
    block: dict[str, Any],
    *,
    line_index: int,
) -> str:
    """Canonical SAP ``PurchaseOrderItem`` for one UI service line."""
    raw = _norm(block.get("purchase_order_item"))
    if raw:
        return format_z_po_item_number(raw)
    return format_z_po_item_number((line_index + 1) * 10)


def stamp_yser_po_grouped_item_numbers(
    form: dict[str, Any],
    *,
    sap_form: dict[str, Any] | None = None,
    force: bool = False,
) -> None:
    """Stamp ``purchase_order_item`` by ``service_group`` (matches Z create layout).

    When ``sap_form`` is provided, reuse hydrated item numbers per group from SAP GET.
    """
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    blocks = yser_effective_line_blocks(form)
    if not blocks:
        return

    item_by_group: dict[str, str] = {}
    if isinstance(sap_form, dict):
        sap_header = (
            sap_form.get("header") if isinstance(sap_form.get("header"), dict) else {}
        )
        for idx, block in enumerate(yser_effective_line_blocks(sap_form)):
            grp = line_catalog_group(block, sap_header, "YSER") or "__default"
            item = yser_po_item_number_for_block(block, line_index=idx)
            if grp not in item_by_group:
                item_by_group[grp] = item

    item_no = 10
    for raw in item_by_group.values():
        try:
            item_no = max(item_no, int(str(raw).lstrip("0") or "0") + 10)
        except ValueError:
            pass

    for group in yser_group_blocks_by_service_group(blocks, header):
        first_block = group[0][1]
        grp = line_catalog_group(first_block, header, "YSER") or "__default"
        if grp in item_by_group:
            assigned = item_by_group[grp]
        else:
            assigned = str(item_no)
            item_no += 10
        for _, block in group:
            if force or not _norm(block.get("purchase_order_item")):
                block["purchase_order_item"] = format_z_po_item_number(assigned)


def _yser_po_bucket_lines(
    form: dict[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group UI lines by ``(purchase_order_item, service_group)`` preserving order."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for block in yser_effective_line_blocks(form):
        po_item = yser_po_item_number_for_block(block, line_index=0)
        grp = line_catalog_group(block, header, "YSER") or _norm(block.get("service_group"))
        key = (po_item, grp)
        buckets.setdefault(key, []).append(block)
    return buckets


def _yser_po_pair_bucket_lines(
    *,
    sub_lines: list[dict[str, Any]],
    sap_lines: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair submitted lines to SAP lines within one PO item + service group bucket."""
    used_sap: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    def _svc_text_key(line: dict[str, Any]) -> tuple[str, str]:
        return (
            normalize_service_performer_code(_norm(line.get("service"))),
            _norm(line.get("short_text"))[:40],
        )

    for sub_line in sub_lines:
        matched: int | None = None
        sub_ref = _yser_po_service_external_ref(sub_line)
        if sub_ref:
            for i, sap_line in enumerate(sap_lines):
                if i in used_sap:
                    continue
                if _yser_po_service_external_ref(sap_line) == sub_ref:
                    matched = i
                    break

        if matched is None:
            st_key = _svc_text_key(sub_line)
            candidates = [
                i
                for i, sap_line in enumerate(sap_lines)
                if i not in used_sap and _svc_text_key(sap_line) == st_key
            ]
            if len(candidates) == 1:
                matched = candidates[0]

        if matched is None:
            sub_key = _yser_po_block_match_key(sub_line)
            if not (len(sub_key) == 1 and sub_ref):
                for i, sap_line in enumerate(sap_lines):
                    if i in used_sap:
                        continue
                    if _yser_po_block_match_key(sap_line) == sub_key:
                        matched = i
                        break

        if matched is None:
            sub_svc = normalize_service_performer_code(_norm(sub_line.get("service")))
            same_sub_svc = sum(
                1
                for row in sub_lines
                if normalize_service_performer_code(_norm(row.get("service"))) == sub_svc
            )
            if sub_svc and same_sub_svc == 1:
                candidates = [
                    i
                    for i, sap_line in enumerate(sap_lines)
                    if i not in used_sap
                    and normalize_service_performer_code(_norm(sap_line.get("service")))
                    == sub_svc
                ]
                if len(candidates) == 1:
                    matched = candidates[0]

        sap_line: dict[str, Any] | None = None
        if matched is not None:
            used_sap.add(matched)
            sap_line = sap_lines[matched]
        pairs.append((sub_line, sap_line))

    return pairs


def prepare_yser_po_create_reconcile_forms(
    submitted: dict[str, Any],
    *,
    sap_form: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Align submitted + SAP read forms for post-create reconcile.

    Stamps grouped ``purchase_order_item`` numbers and pairs services by external ref
    (or service+text+amount within the same PO item and service group). Duplicate service
    codes in one group remain distinct via ``sap_po_service_ref``.
    """
    import copy

    sub = copy.deepcopy(submitted)
    sap = copy.deepcopy(sap_form)
    stamp_yser_po_grouped_item_numbers(sub, sap_form=sap, force=True)
    stamp_yser_po_grouped_item_numbers(sap, sap_form=sap, force=False)

    sub_buckets = _yser_po_bucket_lines(sub)
    sap_buckets = _yser_po_bucket_lines(sap)
    for key, sub_lines in sub_buckets.items():
        sap_lines = sap_buckets.get(key, [])
        for sub_line, sap_line in _yser_po_pair_bucket_lines(
            sub_lines=sub_lines, sap_lines=sap_lines
        ):
            po_item = yser_po_item_number_for_block(sub_line, line_index=0)
            sub_line["purchase_order_item"] = po_item
            sub_ref = _yser_po_service_external_ref(sub_line)
            if sap_line is None:
                continue
            sap_line["purchase_order_item"] = po_item
            if sub_ref:
                sap_line["sap_po_service_ref"] = sub_ref
            else:
                sap_ref = _yser_po_service_external_ref(sap_line)
                if sap_ref:
                    sub_line["sap_po_service_ref"] = sap_ref

    return sub, sap


def count_z_po_sap_items(body: dict[str, Any]) -> int:
    _, snapshots = parse_z_po_items_from_read(body)
    return sum(1 for s in snapshots if not s.is_deleted)


def merge_yser_po_update_form_with_sap(
    submitted: dict[str, Any],
    *,
    sap_form: dict[str, Any],
) -> dict[str, Any]:
    """Apply UI structural edits on top of SAP-hydrated amounts (Z PO update POST)."""
    import copy

    out = copy.deepcopy(submitted)
    sap_lines = sap_form.get("lines") if isinstance(sap_form.get("lines"), list) else []
    sap_by_ref: dict[str, dict[str, Any]] = {}
    sap_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in sap_lines:
        if not isinstance(row, dict):
            continue
        ref = _yser_po_service_external_ref(row)
        if ref:
            sap_by_ref[ref] = row
        svc = normalize_service_performer_code(_norm(row.get("service")))
        po_item = format_z_po_item_number(_norm(row.get("purchase_order_item")) or "10")
        grp = _norm(row.get("service_group"))
        if svc:
            sap_by_key[(po_item, svc, grp)] = row

    merged_lines: list[dict[str, Any]] = []
    used_ext_refs: set[str] = set()
    for block in yser_effective_line_blocks(submitted):
        sap_row: dict[str, Any] | None = None
        ref = _yser_po_service_external_ref(block)
        if ref:
            sap_row = sap_by_ref.get(ref)
        if sap_row is None:
            po_item = format_z_po_item_number(_norm(block.get("purchase_order_item")) or "10")
            svc = normalize_service_performer_code(_norm(block.get("service")))
            grp = _norm(block.get("service_group"))
            sap_row = sap_by_key.get((po_item, svc, grp))
        if sap_row is None:
            svc = normalize_service_performer_code(_norm(block.get("service")))
            if svc:
                matches = [
                    sap_line
                    for sap_line in sap_lines
                    if isinstance(sap_line, dict)
                    and normalize_service_performer_code(_norm(sap_line.get("service"))) == svc
                ]
                if len(matches) == 1:
                    candidate = matches[0]
                    cref = _yser_po_service_external_ref(candidate)
                    if not cref or cref not in used_ext_refs:
                        sap_row = candidate
        line = copy.deepcopy(sap_row) if sap_row else copy.deepcopy(block)
        if sap_row:
            for field in ("purchase_order_item", "sap_pr_item", "purchase_requisition_item"):
                if _norm(sap_row.get(field)):
                    line[field] = sap_row[field]
        if _norm(block.get("service")):
            line["service"] = block.get("service")
        if sap_row:
            sap_ref = _norm(sap_row.get("sap_po_service_ref"))
            sap_svc = normalize_service_performer_code(_norm(sap_row.get("service")))
            sub_svc = normalize_service_performer_code(_norm(line.get("service")))
            if sap_ref and sap_svc and sub_svc and sap_svc == sub_svc:
                if sap_ref not in used_ext_refs:
                    line["sap_po_service_ref"] = sap_ref
                    used_ext_refs.add(sap_ref)
                else:
                    line.pop("sap_po_service_ref", None)
            elif sap_svc and sub_svc and sap_svc != sub_svc:
                line.pop("sap_po_service_ref", None)
        for field in ("short_text", "delivery_date", "order_unit", "service_group"):
            if _norm(block.get(field)):
                line[field] = block[field]
        for field in ("unit_price", "valuation_price", "gross_price", "net_price"):
            if _norm(block.get(field)):
                line[field] = block[field]
        if _norm(block.get("tax_code")):
            line["tax_code"] = block["tax_code"]
        sub_allocs = block.get("allocations")
        if not isinstance(sub_allocs, list):
            sub_allocs = []
        sap_allocs = line.get("allocations") if isinstance(line.get("allocations"), list) else []
        sap_cc_map = {
            _norm(a.get("cost_center")): a
            for a in sap_allocs
            if isinstance(a, dict) and _norm(a.get("cost_center"))
        }
        new_allocs: list[dict[str, Any]] = []
        for alloc in sub_allocs:
            if not isinstance(alloc, dict):
                continue
            cc = _norm(alloc.get("cost_center"))
            if not cc:
                continue
            src = sap_cc_map.get(cc, alloc)
            qty = _norm(src.get("qty")) or _norm(alloc.get("qty")) or "1"
            row: dict[str, Any] = {"cost_center": cc, "qty": qty}
            acct = _norm(src.get("po_acct_assgmt_number")) or _norm(
                alloc.get("po_acct_assgmt_number")
            )
            if acct:
                row["po_acct_assgmt_number"] = acct
            new_allocs.append(row)
        line["allocations"] = new_allocs
        merged_lines.append(line)

    out["lines"] = merged_lines
    return out


def reconcile_yser_po_verify_form_with_sap_read(
    verify_form: dict[str, Any],
    after_body: dict[str, Any],
) -> dict[str, Any]:
    """Align verify form allocations/prices with SAP GET after grouped-item mutations.

    SAP may normalize CC or consolidated qty on sibling services; POST still succeeds.
    """
    import copy

    out = copy.deepcopy(verify_form)
    after_form = form_from_z_po_read(after_body, seed_form=verify_form)
    after_lines = [
        row for row in (after_form.get("lines") or []) if isinstance(row, dict)
    ]
    verify_lines = [row for row in (out.get("lines") or []) if isinstance(row, dict)]
    if not after_lines or len(after_lines) != len(verify_lines):
        return out

    after_by_ref = {
        _yser_po_service_external_ref(row): row
        for row in after_lines
        if _yser_po_service_external_ref(row)
    }

    for line in verify_lines:
        ref = _yser_po_service_external_ref(line)
        after_line = after_by_ref.get(ref) if ref else None
        if after_line is None:
            svc = normalize_service_performer_code(_norm(line.get("service")))
            for candidate in after_lines:
                if normalize_service_performer_code(_norm(candidate.get("service"))) == svc:
                    after_line = candidate
                    break
        if after_line is None:
            continue
        line["allocations"] = copy.deepcopy(after_line.get("allocations") or [])
    return out


def _build_z_po_service_acct_rows(
    *,
    po_number: str,
    item_number: str,
    service_no: str,
    allocs: list[dict[str, Any]],
    unit: float,
    line_total: str,
) -> list[dict[str, Any]]:
    svc_no = normalize_service_performer_code(service_no)
    valid = [a for a in allocs if isinstance(a, dict) and _norm(a.get("cost_center"))]
    if not valid:
        qty = _format_z_qty(1.0)
        return [
            {
                "CostCenter": "",
                "IsDeleted": False,
                "PurchaseOrder": _norm(po_number),
                "PurchaseOrderItem": item_number,
                "Quantity": qty,
                "PurgDocNetAmount": line_total,
                "ServiceNumber": svc_no,
            }
        ]

    rows: list[dict[str, Any]] = []
    for alloc in valid:
        cc = _norm(alloc.get("cost_center"))
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qf = float(qty_raw.replace(",", "."))
        except ValueError:
            qf = 1.0
        qty = _format_z_qty(qf)
        rows.append(
            {
                "CostCenter": cc,
                "IsDeleted": False,
                "PurchaseOrder": _norm(po_number),
                "PurchaseOrderItem": item_number,
                "Quantity": qty,
                "PurgDocNetAmount": _alloc_purg_amount(unit=unit, alloc_qty=qf),
                "ServiceNumber": svc_no,
            }
        )
    return rows


def _build_z_po_service_row(
    *,
    po_number: str,
    item_number: str,
    block: dict[str, Any],
    allocs: list[dict[str, Any]],
    tax_code: str,
    tax_jurisdiction: str,
    multi_cc: bool,
) -> dict[str, Any]:
    service_no = normalize_service_performer_code(_norm(block.get("service")))
    short_text = _norm(block.get("short_text"))
    qty_total = _item_total_quantity(allocs)
    unit = _unit_price_from_block(block)
    line_total = _line_total_amount(block, qty_total=qty_total)
    unit_str = _format_po_z_price(unit) if unit > 0 else line_total
    line_uom = _yser_line_unit(block)

    svc: dict[str, Any] = {
        "AccountAssignmentCategory": "",
        "ConfirmedQuantity": _format_z_qty(qty_total),
        "IsDeleted": "",
        "NetAmount": unit_str,
        "NetPriceAmount": line_total,
        "Plant": "",
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_number,
        "QuantityUnit": line_uom,
        "Service": service_no,
        "ServiceEntrySheetItemDesc": short_text,
        "ServicePerformer": "",
        PO_SERVICE_ACCT_NAV: _build_z_po_service_acct_rows(
            po_number=po_number,
            item_number=item_number,
            service_no=service_no,
            allocs=allocs,
            unit=unit,
            line_total=line_total,
        ),
    }
    if multi_cc:
        svc["MultipleAcctAssgmtDistribution"] = "1"
    ext_ref = _yser_po_service_external_ref(block)
    if ext_ref:
        svc["PurgDocItemExternalReference"] = ext_ref
    perf_date = _sap_date(_norm(block.get("delivery_date")))
    if perf_date:
        svc["ServicePerformanceDate"] = perf_date
    return svc


def _iter_yser_po_item_groups(
    form: dict[str, Any],
    *,
    grouped_layout: bool,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """(PO item number, blocks in group) — one group per service_group when grouped."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    if grouped_layout:
        blocks = yser_effective_line_blocks(form)
        out: list[tuple[str, list[dict[str, Any]]]] = []
        item_no = 10
        for group in yser_group_blocks_by_service_group(blocks, header):
            group_blocks = [block for _, block in group]
            if group_blocks:
                out.append((str(item_no), group_blocks))
                item_no += 10
        return out
    return [(item_no, [block]) for item_no, block in _iter_po_form_lines(form, document_type="YSER")]


def _linked_pr_item_number(block: dict[str, Any]) -> str:
    """SAP PR item for PO link — grouped docs use ``sap_pr_item``."""
    return (
        _norm(block.get("sap_pr_item"))
        or _norm(block.get("purchase_requisition_item"))
        or "10"
    )


def _build_z_po_item_row(
    *,
    blocks: list[dict[str, Any]],
    header: dict[str, Any],
    item_number: str,
    po_number: str,
    parent_pr_number: str | None,
    tax_code: str,
    ticket_id: str | None,
    mark_deleted: bool = False,
) -> dict[str, Any]:
    if not blocks:
        raise ValueError("PO item row requires at least one service block")
    block = blocks[0]
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(_norm(header.get("storage_location")), plant=plant)
    mat_grp = line_catalog_group(block, header, "YSER")
    item_no = format_z_po_item_number(item_number)
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    line_uom = _yser_line_unit(block)
    acct_cat = _yser_po_acct_cat(block)
    item_text = build_po_item_text_for_sap(
        _norm(block.get("short_text")) or mat_grp
    )

    service_rows: list[dict[str, Any]] = []
    net_total = 0.0
    any_multi_cc = False
    if not mark_deleted:
        for svc_block in blocks:
            allocs = svc_block.get("allocations")
            if not isinstance(allocs, list) or not allocs:
                allocs = [{}]
            valid_allocs = [a for a in allocs if isinstance(a, dict)]
            multi_cc = len([a for a in valid_allocs if _norm(a.get("cost_center"))]) > 1
            any_multi_cc = any_multi_cc or multi_cc
            qty_total = _item_total_quantity(valid_allocs)
            line_total = _line_total_amount(svc_block, qty_total=qty_total)
            try:
                net_total += float(str(line_total).replace(",", "."))
            except ValueError:
                pass
            service_rows.append(
                _build_z_po_service_row(
                    po_number=po_number,
                    item_number=item_no,
                    block=svc_block,
                    allocs=valid_allocs,
                    tax_code=tax_code,
                    tax_jurisdiction=tax_jurisdiction,
                    multi_cc=multi_cc,
                )
            )
    net_price_amount = f"{net_total:.2f}" if net_total > 0 else _line_total_amount(block, qty_total=1.0)

    row: dict[str, Any] = {
        "IsReturnsItem": mark_deleted,
        "MaterialGroup": mat_grp,
        "NetPriceAmount": net_price_amount,
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

    if not mark_deleted:
        row[PO_SERVICES_NAV] = service_rows
    return row


def _build_z_po_delete_item_stub(
    *,
    snap: SapZPoItemSnapshot,
    header: dict[str, Any],
    po_number: str,
    tax_code: str,
) -> dict[str, Any]:
    """Doc delete shape: item stub with ``IsReturnsItem`` — no ``to_Services`` nav."""
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(_norm(header.get("storage_location")), plant=plant)
    mat_grp = line_catalog_group({"service_group": snap.material_group}, header, "YSER")
    item_no = format_z_po_item_number(snap.item_number)
    tax_jurisdiction = _tax_jurisdiction_for_po_item(header, tax_code)
    line_uom = (
        (snap.services[0].quantity_unit if snap.services else "")
        or snap.quantity_unit
        or _service_uom({})
    )
    svc = snap.services[0] if snap.services else None
    line_total = _format_po_z_price(
        svc.net_price_amount if svc and svc.net_price_amount else snap.net_price_amount or "0"
    )
    acct_segments = [seg for svc in snap.services for seg in svc.acct_segments if seg.cost_center]
    multi_cc = len(acct_segments) > 1

    row: dict[str, Any] = {
        "IsReturnsItem": True,
        "MaterialGroup": mat_grp,
        "NetPriceAmount": line_total,
        "NetPriceQuantity": "1",
        "OrderPriceUnit": line_uom,
        "Plant": plant,
        "PurchaseOrder": _norm(po_number),
        "PurchaseOrderItem": item_no,
        "PurchaseOrderItemCategory": "9",
        "PurchaseOrderItemText": snap.item_text,
        "PurchaseOrderQuantityUnit": line_uom,
        "StorageLocation": sloc,
    }
    if snap.account_assignment_cat:
        row["AccountAssignmentCategory"] = snap.account_assignment_cat
    if snap.purchase_requisition:
        row["PurchaseRequisition"] = snap.purchase_requisition
        row["PurchaseRequisitionItem"] = format_po_purchase_requisition_item(
            snap.purchase_requisition_item or snap.item_number
        )
    if tax_code:
        row["TaxCode"] = tax_code
    if tax_jurisdiction:
        row["TaxJurisdiction"] = tax_jurisdiction
    if multi_cc:
        row["MultipleAcctAssgmtDistribution"] = "1"
    return row


def _norm_alloc_qty(qty: str) -> str:
    raw = _norm(qty) or "1"
    try:
        return _format_z_qty(float(raw.replace(",", ".")))
    except ValueError:
        return raw


def _form_alloc_pairs(block: dict[str, Any]) -> list[tuple[str, str]]:
    allocs = block.get("allocations") if isinstance(block.get("allocations"), list) else []
    pairs: list[tuple[str, str]] = []
    for alloc in allocs:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        pairs.append((cc, _norm_alloc_qty(_norm(alloc.get("qty")) or "1")))
    return sorted(pairs)


def _service_alloc_pairs(svc: SapZPoServiceSnapshot) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for seg in svc.acct_segments:
        cc = _norm(seg.cost_center)
        if not cc:
            continue
        pairs.append((cc, _norm_alloc_qty(_norm(seg.quantity) or "1")))
    return sorted(pairs)


def _yser_po_snap_has_multiple_services(snap: SapZPoItemSnapshot) -> bool:
    """True when one PO item carries more than one ``to_Services`` row."""
    codes = [
        normalize_service_performer_code(s.service)
        for s in snap.services
        if normalize_service_performer_code(s.service)
    ]
    return len(codes) > 1


def _yser_po_service_has_multi_cc_splits(svc: SapZPoServiceSnapshot) -> bool:
    """Single-service item with multiple cost-center splits (use acct tab for CC qty only)."""
    ccs = {_norm(seg.cost_center) for seg in svc.acct_segments if _norm(seg.cost_center)}
    return len(ccs) > 1


def _yser_po_snap_has_duplicate_service_codes(snap: SapZPoItemSnapshot) -> bool:
    codes = [
        normalize_service_performer_code(s.service)
        for s in snap.services
        if normalize_service_performer_code(s.service)
    ]
    return len(codes) > 1 and len(codes) != len(set(codes))


def _sap_po_amounts_equal(a: str, b: str) -> bool:
    left = _norm(a).replace(",", ".")
    right = _norm(b).replace(",", ".")
    if not left or not right:
        return False
    try:
        return abs(float(left) - float(right)) < 0.02
    except ValueError:
        return left == right


def _yser_po_service_unit_price(svc: SapZPoServiceSnapshot) -> float:
    """Implied unit price from service line total / confirmed quantity."""
    net_s = svc.net_price_amount or ""
    try:
        net = float(str(net_s).replace(",", "."))
    except ValueError:
        net = 0.0
    if net <= 0:
        try:
            net = float(str(svc.net_amount or "0").replace(",", "."))
        except ValueError:
            net = 0.0
    try:
        qty = float(str(_norm(svc.confirmed_quantity) or "1").replace(",", "."))
    except ValueError:
        qty = 1.0
    return net / qty if qty > 0 else 0.0


def _yser_po_acct_segment_dedupe_key(seg: SapAcctSegment) -> tuple[str, str, str, str]:
    return (
        _norm(seg.seq),
        _norm(seg.cost_center),
        _norm(seg.quantity),
        _norm(seg.purg_doc_net_amount),
    )


def _yser_po_dedupe_acct_segments(
    segments: list[SapAcctSegment],
) -> list[SapAcctSegment]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[SapAcctSegment] = []
    for seg in segments:
        key = _yser_po_acct_segment_dedupe_key(seg)
        if key in seen:
            continue
        seen.add(key)
        out.append(seg)
    return out


def _yser_po_acct_matches_service_unit(seg: SapAcctSegment, unit: float) -> bool:
    if unit <= 0:
        return True
    try:
        qty = float(str(_norm(seg.quantity) or "1").replace(",", "."))
        purg = float(str(_norm(seg.purg_doc_net_amount) or "0").replace(",", "."))
    except ValueError:
        return False
    return abs(purg - unit * qty) < 0.02


def _yser_po_qty_values_equal(a: str, b: str) -> bool:
    try:
        return abs(float(str(a).replace(",", ".")) - float(str(b).replace(",", "."))) < 0.001
    except ValueError:
        return _norm(a) == _norm(b)


def _yser_po_refine_dup_service_acct_segments(
    svc: SapZPoServiceSnapshot,
    snap: SapZPoItemSnapshot,
    segments: list[SapAcctSegment],
) -> list[SapAcctSegment]:
    """Dup-service fan-out: narrow unit-filtered rows to this service line."""
    if not _yser_po_snap_has_duplicate_service_codes(snap) or len(segments) <= 1:
        return segments

    svc_qty = _norm(svc.confirmed_quantity)
    by_qty = [seg for seg in segments if _yser_po_qty_values_equal(seg.quantity or "", svc_qty)]
    if len(by_qty) == 1:
        return by_qty

    net_s = svc.net_price_amount or ""
    if net_s:
        by_purg = [
            seg
            for seg in segments
            if seg.purg_doc_net_amount and _sap_po_amounts_equal(seg.purg_doc_net_amount, net_s)
        ]
        if len(by_purg) == 1:
            return by_purg

    return segments


def _yser_po_allocations_from_acct_segments(
    svc: SapZPoServiceSnapshot,
    snap: SapZPoItemSnapshot | None = None,
    *,
    amount_filter: bool = False,
) -> list[dict[str, Any]]:
    segments = _yser_po_dedupe_acct_segments(svc.acct_segments)
    unit = _yser_po_service_unit_price(svc) if amount_filter else 0.0
    matched: list[SapAcctSegment] = []
    for seg in segments:
        cc = _norm(seg.cost_center)
        if not cc:
            continue
        if amount_filter and not _yser_po_acct_matches_service_unit(seg, unit):
            continue
        matched.append(seg)

    if amount_filter and snap is not None and len(matched) > 1:
        matched = _yser_po_refine_dup_service_acct_segments(svc, snap, matched)

    return [
        {
            "cost_center": _norm(seg.cost_center),
            "qty": _form_allocation_qty(seg.quantity or "1"),
            **(
                {"po_acct_assgmt_number": _norm(seg.seq)}
                if _norm(seg.seq)
                else {}
            ),
        }
        for seg in matched
        if _norm(seg.cost_center)
    ]


def _yser_po_use_acct_assignment_tab(snap: SapZPoItemSnapshot, svc: SapZPoServiceSnapshot) -> bool:
    """True when verify should compare full per-CC qty pairs from nested acct (single-service multi-CC)."""
    if _yser_po_snap_has_multiple_services(snap):
        return False
    return _yser_po_service_has_multi_cc_splits(svc)


def _yser_po_seed_cc_rows(
    seed_line: dict[str, Any] | None,
    *,
    svc_qty: str,
) -> list[dict[str, Any]]:
    seed_allocs = (
        seed_line.get("allocations")
        if isinstance(seed_line, dict) and isinstance(seed_line.get("allocations"), list)
        else []
    )
    rows: list[dict[str, Any]] = []
    for alloc in seed_allocs:
        if not isinstance(alloc, dict):
            continue
        cc = _norm(alloc.get("cost_center"))
        if not cc:
            continue
        rows.append(
            {
                "cost_center": cc,
                "qty": _form_allocation_qty(_norm(alloc.get("qty")) or svc_qty),
            }
        )
    return rows


def _yser_po_service_match_key(svc: SapZPoServiceSnapshot) -> tuple[str, ...]:
    """Match one UI line to one SAP service row (ext ref first, then service+text+net)."""
    ref = _norm(svc.purg_doc_item_external_reference)
    if ref:
        return (ref,)
    return (
        normalize_service_performer_code(svc.service),
        _norm(svc.short_text)[:40],
        _norm(svc.net_price_amount),
    )


def _yser_po_block_match_key(block: dict[str, Any]) -> tuple[str, ...]:
    ref = _yser_po_service_external_ref(block)
    if ref:
        return (ref,)
    allocs = block.get("allocations") if isinstance(block.get("allocations"), list) else []
    qty_total = _item_total_quantity(
        [a for a in allocs if isinstance(a, dict)]
    )
    return (
        normalize_service_performer_code(_norm(block.get("service"))),
        _norm(block.get("short_text"))[:40],
        _line_total_amount(block, qty_total=qty_total),
    )


def _yser_po_match_seed_line(
    seed_lines: list[dict[str, Any]],
    svc: SapZPoServiceSnapshot,
    *,
    line_index: int,
    po_item: str | None = None,
    material_group: str | None = None,
    used: set[int] | None = None,
) -> dict[str, Any]:
    used_indices = used if used is not None else set()

    def _take(index: int) -> dict[str, Any]:
        used_indices.add(index)
        row = seed_lines[index]
        return row if isinstance(row, dict) else {}

    sap_ref = _norm(svc.purg_doc_item_external_reference)
    if sap_ref:
        for i, row in enumerate(seed_lines):
            if i in used_indices or not isinstance(row, dict):
                continue
            if _yser_po_service_external_ref(row) == sap_ref:
                return _take(i)

    key = _yser_po_service_match_key(svc)
    po_item_fmt = format_z_po_item_number(po_item) if po_item else ""
    grp = _norm(material_group)

    for i, row in enumerate(seed_lines):
        if i in used_indices or not isinstance(row, dict):
            continue
        if po_item_fmt:
            row_item = yser_po_item_number_for_block(row, line_index=i)
            if row_item and row_item != po_item_fmt:
                continue
        if grp:
            row_grp = _norm(row.get("service_group"))
            if row_grp and row_grp != grp:
                continue
        if _yser_po_block_match_key(row) == key:
            return _take(i)

    for i, row in enumerate(seed_lines):
        if i in used_indices or not isinstance(row, dict):
            continue
        if _yser_po_block_match_key(row) == key:
            return _take(i)

    want = normalize_service_performer_code(svc.service)
    if want:
        candidates = [
            i
            for i, row in enumerate(seed_lines)
            if i not in used_indices
            and isinstance(row, dict)
            and normalize_service_performer_code(_norm(row.get("service"))) == want
        ]
        if len(candidates) == 1:
            return _take(candidates[0])

    if line_index < len(seed_lines) and line_index not in used_indices:
        row = seed_lines[line_index]
        if isinstance(row, dict):
            return _take(line_index)
    return {}


def _yser_po_allocations_from_read(
    svc: SapZPoServiceSnapshot,
    snap: SapZPoItemSnapshot,
    seed_line: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Hydrate allocations from nested acct rows (amount filter when nav fans out)."""
    svc_qty = _form_allocation_qty(svc.confirmed_quantity or "1")
    deduped = _yser_po_dedupe_acct_segments(svc.acct_segments)
    has_purg = any(_norm(seg.purg_doc_net_amount) for seg in deduped)
    unit = _yser_po_service_unit_price(svc)
    amount_filter = len(deduped) > 1 and unit > 0 and has_purg
    acct_rows = _yser_po_allocations_from_acct_segments(
        svc, snap, amount_filter=amount_filter
    )

    if len(acct_rows) > 1:
        return acct_rows

    if len(acct_rows) == 1:
        if not amount_filter:
            acct_rows[0]["qty"] = svc_qty
        return acct_rows

    seed_cc_rows = _yser_po_seed_cc_rows(seed_line, svc_qty=svc_qty)
    if len(seed_cc_rows) == 1:
        seed_cc_rows[0]["qty"] = svc_qty
        return seed_cc_rows
    if seed_cc_rows:
        return seed_cc_rows

    cc_on_svc = _norm(svc.cost_center)
    return [{"cost_center": cc_on_svc, "qty": svc_qty}]


def _yser_po_read_alloc_pairs(
    svc: SapZPoServiceSnapshot,
    snap: SapZPoItemSnapshot,
) -> list[tuple[str, str]]:
    return sorted(
        (
            _norm(row["cost_center"]),
            _norm_alloc_qty(_norm(row.get("qty")) or "1"),
        )
        for row in _yser_po_allocations_from_read(svc, snap, None)
        if _norm(row.get("cost_center"))
    )


def _yser_po_form_service_qty_pair(block: dict[str, Any]) -> tuple[str, str]:
    allocs = block.get("allocations") if isinstance(block.get("allocations"), list) else []
    qty_total = _item_total_quantity(
        [a for a in allocs if isinstance(a, dict)]
    )
    qty = qty_total if qty_total > 0 else 1.0
    return ("", _norm_alloc_qty(str(qty)))


def _yser_po_service_qty_pair(svc: SapZPoServiceSnapshot) -> tuple[str, str]:
    return ("", _norm_alloc_qty(_norm(svc.confirmed_quantity) or "1"))


def _yser_po_alloc_pairs_match(
    block: dict[str, Any],
    svc: SapZPoServiceSnapshot,
    snap: SapZPoItemSnapshot,
) -> bool:
    read_pairs = _yser_po_read_alloc_pairs(svc, snap)
    form_ccs = _form_alloc_pairs(block)
    if not form_ccs and not read_pairs:
        return True
    if _yser_po_use_acct_assignment_tab(snap, svc):
        return form_ccs == read_pairs
    if _yser_po_form_service_qty_pair(block) != _yser_po_service_qty_pair(svc):
        return False
    return form_ccs == read_pairs


def _snap_alloc_pairs(snap: SapZPoItemSnapshot) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for svc in snap.services:
        pairs.extend(_service_alloc_pairs(svc))
    return sorted(pairs)


def _find_z_po_service_snap(
    snap: SapZPoItemSnapshot, *, service_code: str
) -> SapZPoServiceSnapshot | None:
    want = normalize_service_performer_code(service_code)
    if not want:
        return snap.services[0] if snap.services else None
    for svc in snap.services:
        if normalize_service_performer_code(svc.service) == want:
            return svc
    return None


def _yser_po_find_sap_service_for_block(
    snapshots: list[SapZPoItemSnapshot],
    block: dict[str, Any],
) -> tuple[str, SapZPoServiceSnapshot, SapZPoItemSnapshot] | None:
    """Match one form line to one SAP service row (ext ref, then service+net)."""
    want_ref = _yser_po_service_external_ref(block)
    if want_ref:
        for snap in snapshots:
            if snap.is_deleted:
                continue
            for svc in snap.services:
                if _norm(svc.purg_doc_item_external_reference) == want_ref:
                    return snap.item_number, svc, snap

    key = _yser_po_block_match_key(block)
    want = normalize_service_performer_code(_norm(block.get("service")))
    allocs = block.get("allocations") if isinstance(block.get("allocations"), list) else []
    qty_total = _item_total_quantity([a for a in allocs if isinstance(a, dict)])
    want_net = _line_total_amount(block, qty_total=qty_total or 1.0)

    for snap in snapshots:
        if snap.is_deleted:
            continue
        for svc in snap.services:
            if _yser_po_service_match_key(svc) == key:
                return snap.item_number, svc, snap

    if want and want_net:
        hits: list[tuple[str, SapZPoServiceSnapshot, SapZPoItemSnapshot]] = []
        for snap in snapshots:
            if snap.is_deleted:
                continue
            for svc in snap.services:
                if normalize_service_performer_code(svc.service) != want:
                    continue
                sap_net = svc.net_price_amount or svc.net_amount or ""
                if _sap_po_amounts_equal(sap_net, want_net):
                    hits.append((snap.item_number, svc, snap))
        if len(hits) == 1:
            return hits[0]

    if want:
        for snap in snapshots:
            if snap.is_deleted:
                continue
            for svc in snap.services:
                if normalize_service_performer_code(svc.service) == want:
                    return snap.item_number, svc, snap
    return None


def _yser_sap_services_index(
    snapshots: list[SapZPoItemSnapshot],
) -> dict[str, tuple[str, SapZPoServiceSnapshot]]:
    """Normalized service code → (PO item number, service snapshot)."""
    out: dict[str, tuple[str, SapZPoServiceSnapshot]] = {}
    for snap in snapshots:
        if snap.is_deleted:
            continue
        for svc in snap.services:
            code = normalize_service_performer_code(svc.service)
            if code:
                out[code] = (snap.item_number, svc)
    return out


def _yser_form_service_codes(form: dict[str, Any]) -> list[str]:
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    codes: list[str] = []
    for block in lines:
        if not isinstance(block, dict):
            continue
        code = normalize_service_performer_code(_norm(block.get("service")))
        if code:
            codes.append(code)
    return codes


def _resolve_yser_po_item_number(
    *,
    block: dict[str, Any],
    fallback_item_no: str,
    existing_items: list[SapZPoItemSnapshot],
) -> str:
    """Map a form line to the SAP PO item that already carries this service."""
    po_item = _norm(block.get("purchase_order_item"))
    if po_item:
        want = po_item.lstrip("0") or po_item
        for snap in existing_items:
            if snap.is_deleted:
                continue
            snap_no = snap.item_number.lstrip("0") or snap.item_number
            if snap_no == want:
                return snap.item_number
        return po_item

    want = normalize_service_performer_code(_norm(block.get("service")))
    line_pri = normalize_pr_item_number(_norm(block.get("purchase_requisition_item")))
    if want:
        matches: list[SapZPoItemSnapshot] = []
        for snap in existing_items:
            if snap.is_deleted:
                continue
            for svc in snap.services:
                if normalize_service_performer_code(svc.service) == want:
                    matches.append(snap)
                    break
        if len(matches) == 1:
            return matches[0].item_number
        if len(matches) > 1 and line_pri:
            for snap in matches:
                pr_item = snap.purchase_requisition_item.lstrip("0") or ""
                if pr_item == line_pri:
                    return snap.item_number
            return matches[0].item_number
    return fallback_item_no


def _z_po_form_service_differs(
    block: dict[str, Any],
    sap_svc: SapZPoServiceSnapshot,
    *,
    snap: SapZPoItemSnapshot,
    header: dict[str, Any],
) -> bool:
    exp_service = normalize_service_performer_code(_norm(block.get("service")))
    sap_service = normalize_service_performer_code(sap_svc.service)
    if exp_service and sap_service and exp_service != sap_service:
        return True
    if not _yser_po_alloc_pairs_match(block, sap_svc, snap):
        return True
    qty_total = _item_total_quantity(
        [a for a in (block.get("allocations") or []) if isinstance(a, dict)]
    )
    exp_total = _line_total_amount(block, qty_total=qty_total)
    sap_total = _format_po_z_price(sap_svc.net_price_amount or snap.net_price_amount or "0")
    try:
        if abs(float(exp_total) - float(sap_total)) > 0.01:
            return True
    except ValueError:
        if exp_total != sap_total:
            return True
    exp_delivery = _norm(block.get("delivery_date"))
    sap_delivery = _z_po_line_delivery_from_snap(snap, sap_svc)
    if exp_delivery != sap_delivery and (exp_delivery or sap_delivery):
        return True
    exp_unit = _format_po_z_price(_unit_price_from_block(block))
    sap_unit = _format_po_z_price(sap_svc.net_amount or "")
    if exp_unit and sap_unit:
        try:
            if abs(float(exp_unit) - float(sap_unit)) > 0.01:
                return True
        except ValueError:
            if exp_unit != sap_unit:
                return True
    return False


def _z_po_form_group_differs(
    blocks: list[dict[str, Any]],
    snap: SapZPoItemSnapshot,
    *,
    header: dict[str, Any],
    ticket_id: str | None,
) -> bool:
    if len(blocks) != len(snap.services):
        return True
    for block, sap_svc in zip(blocks, snap.services):
        if _z_po_form_service_differs(block, sap_svc, snap=snap, header=header):
            return True
    first = blocks[0]
    exp_grp = line_catalog_group(first, header, "YSER")
    sap_grp = _norm(snap.material_group)
    block_grp = _norm(first.get("service_group"))
    if block_grp and sap_grp and block_grp != sap_grp:
        return True
    if block_grp and not sap_grp:
        return True
    if exp_grp and sap_grp and exp_grp != sap_grp and not block_grp:
        return True
    if yser_po_group_tax_differs_from_sap(blocks, header, sap_tax=_norm(snap.tax_code)):
        return True
    return False


def _z_po_form_item_differs(
    block: dict[str, Any],
    snap: SapZPoItemSnapshot,
    *,
    header: dict[str, Any],
    ticket_id: str | None,
) -> bool:
    exp_service = normalize_service_performer_code(_norm(block.get("service")))
    sap_svc = _find_z_po_service_snap(snap, service_code=exp_service)
    if sap_svc is None:
        return True
    if _z_po_form_service_differs(block, sap_svc, snap=snap, header=header):
        return True
    exp_grp = line_catalog_group(block, header, "YSER")
    sap_grp = _norm(snap.material_group)
    block_grp = _norm(block.get("service_group"))
    if block_grp and sap_grp and block_grp != sap_grp:
        return True
    if block_grp and not sap_grp:
        return True
    if exp_grp and sap_grp and exp_grp != sap_grp and not block_grp:
        return True
    exp_tax = line_tax_code(block, header)
    sap_tax = _norm(snap.tax_code)
    if sap_tax and exp_tax != sap_tax:
        return True
    # Service short_text is often SAP master data on create — not a resubmit delta.
    return False


def _wrap_z_po_nav_lists(inner: dict[str, Any]) -> dict[str, Any]:
    """Wrap item/service/acct lists in OData ``results`` for POST ``d`` body."""
    out = dict(inner)
    items = out.get("to_PurchaseOrderItem")
    if isinstance(items, list):
        wrapped_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            services = row.get(PO_SERVICES_NAV)
            if isinstance(services, list):
                svc_wrapped: list[dict[str, Any]] = []
                for svc in services:
                    if not isinstance(svc, dict):
                        continue
                    srow = dict(svc)
                    accts = srow.get(PO_SERVICE_ACCT_NAV)
                    if isinstance(accts, list):
                        srow[PO_SERVICE_ACCT_NAV] = {"results": accts}
                    svc_wrapped.append(srow)
                row[PO_SERVICES_NAV] = {"results": svc_wrapped}
            wrapped_items.append(row)
        out["to_PurchaseOrderItem"] = {"results": wrapped_items}
    return {"d": out}


def build_z_yser_po_inner_payload(
    *,
    form: dict[str, Any],
    po_number: str = "",
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
    sap_item_count: int | None = None,
    for_update: bool = False,
) -> dict[str, Any]:
    """Unwrapped header + item list for YSER PO (tests + resubmit planning)."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    pur_org = _norm(header.get("purchasing_org"))
    supplier = _fixed_supplier(header)
    if not supplier:
        raise ValueError("Vendor (Supplier) is required for SAP PO create")

    po_key = _norm(po_number)
    pr_no = _norm(parent_pr_number)

    _ensure_yser_po_service_refs(form, ticket_id=ticket_id)
    blocks = yser_effective_line_blocks(form)
    grouped_layout = not for_update
    if for_update and sap_item_count is not None:
        grouped_layout = yser_sap_layout_is_grouped(
            sap_item_count=sap_item_count,
            form_line_count=len(blocks),
        )

    items: list[dict[str, Any]] = []
    for item_no_str, group_blocks in _iter_yser_po_item_groups(
        form, grouped_layout=grouped_layout
    ):
        items.append(
            _build_z_po_item_row(
                blocks=group_blocks,
                header=header,
                item_number=item_no_str,
                po_number=po_key,
                parent_pr_number=pr_no or None,
                tax_code=yser_po_group_item_tax_code(group_blocks, header),
                ticket_id=ticket_id,
            )
        )

    if not items:
        raise ValueError("No PO line items to send to SAP (empty blocks or allocations).")

    inner: dict[str, Any] = {
        "PurchaseOrder": po_key,
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
    requestor_email = po_requestor_email_for_sap(header)
    if requestor_email:
        inner["SalesPerson"] = requestor_email
    _apply_yser_po_creator_header_field(inner, creator_email=creator_email)
    apply_yser_po_texts_to_z_inner(inner, header)
    if ticket_id and not po_key:
        marker = sap_ticket_ext_system_marker(ticket_id)
        if marker:
            inner["Extsourcesystem"] = marker
    return inner


def build_z_yser_po_post_body(
    *,
    form: dict[str, Any],
    po_number: str = "",
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
) -> dict[str, Any]:
    """Full ``POST A_PurchaseOrder`` body for YSER (``d`` wrapper + ``results`` nav)."""
    inner = build_z_yser_po_inner_payload(
        form=form,
        po_number=po_number,
        ticket_id=ticket_id,
        parent_pr_number=parent_pr_number,
        creator_email=creator_email,
    )
    return _wrap_z_po_nav_lists(inner)


@dataclass
class SapZPoServiceSnapshot:
    service: str = ""
    confirmed_quantity: str = ""
    net_price_amount: str = ""
    net_amount: str = ""
    short_text: str = ""
    quantity_unit: str = ""
    service_performance_date: str = ""
    cost_center: str = ""
    purg_doc_item_external_reference: str = ""
    acct_segments: list[SapAcctSegment] = field(default_factory=list)


@dataclass
class SapZPoItemSnapshot:
    item_number: str
    is_deleted: bool
    order_quantity: str = ""
    quantity_unit: str = ""
    net_price_amount: str = ""
    item_text: str = ""
    account_assignment_cat: str = ""
    purchase_requisition: str = ""
    purchase_requisition_item: str = ""
    delivery_date: str = ""
    material_group: str = ""
    tax_code: str = ""
    services: list[SapZPoServiceSnapshot] = field(default_factory=list)


def _po_item_deleted(props: dict[str, Any]) -> bool:
    del_code = odata_text(props.get("PurchasingDocumentDeletionCode"))
    if del_code:
        return True
    return bool(
        props.get("IsReturnsItem") is True
        or odata_text(props.get("IsReturnsItem")).upper() in ("X", "TRUE", "1")
    )


def _service_acct_segments(svc_entry: dict[str, Any]) -> list[SapAcctSegment]:
    sp = odata_entity_properties(svc_entry)
    acct_node = svc_entry.get(PO_SERVICE_ACCT_NAV)
    if acct_node is None:
        acct_node = sp.get(PO_SERVICE_ACCT_NAV)
    segments: list[SapAcctSegment] = []
    for acct_entry in odata_results_list(acct_node):
        ap = odata_entity_properties(acct_entry)
        if is_sap_deleted_flag(odata_text(ap.get("IsDeleted"))):
            continue
        cc = odata_text(ap.get("CostCenter"))
        qty = odata_text(ap.get("Quantity"))
        try:
            if float(str(qty or "0").replace(",", ".")) == 0:
                continue
        except ValueError:
            pass
        seq = odata_text(ap.get("AccountAssignmentNumber"))
        purg = odata_text(ap.get("PurgDocNetAmount"))
        if cc or qty:
            segments.append(
                SapAcctSegment(
                    seq=seq,
                    cost_center=cc,
                    quantity=qty,
                    purg_doc_net_amount=purg,
                )
            )
    return segments


def parse_z_po_items_from_read(body: dict[str, Any]) -> tuple[str, list[SapZPoItemSnapshot]]:
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return "", []
    header_ref = odata_text(root.get("CorrespncInternalReference"))
    snapshots: list[SapZPoItemSnapshot] = []
    for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
        props = odata_entity_properties(entry)
        item_no = odata_text(props.get("PurchaseOrderItem"))
        if not item_no:
            continue
        deleted = _po_item_deleted(props)
        services: list[SapZPoServiceSnapshot] = []
        svc_node = entry.get(PO_SERVICES_NAV)
        if svc_node is None:
            svc_node = props.get(PO_SERVICES_NAV)
        for svc_entry in odata_results_list(svc_node):
            sp = odata_entity_properties(svc_entry)
            if is_sap_deleted_flag(odata_text(sp.get("IsDeleted"))):
                continue
            code = odata_text(sp.get("Service")) or odata_text(sp.get("ServiceNumber"))
            services.append(
                SapZPoServiceSnapshot(
                    service=code,
                    confirmed_quantity=odata_text(sp.get("ConfirmedQuantity")),
                    net_price_amount=odata_text(sp.get("NetPriceAmount")),
                    net_amount=odata_text(sp.get("NetAmount")),
                    short_text=odata_text(sp.get("ServiceEntrySheetItemDesc")),
                    quantity_unit=odata_text(sp.get("QuantityUnit")),
                    service_performance_date=odata_date_to_form(
                        sp.get("ServicePerformanceDate")
                    ),
                    cost_center=odata_text(sp.get("CostCenter")),
                    purg_doc_item_external_reference=odata_text(
                        sp.get("PurgDocItemExternalReference")
                    ),
                    acct_segments=_service_acct_segments(svc_entry),
                )
            )
        snapshots.append(
            SapZPoItemSnapshot(
                item_number=item_no,
                is_deleted=deleted,
                order_quantity=odata_text(props.get("OrderQuantity")),
                quantity_unit=odata_text(props.get("PurchaseOrderQuantityUnit"))
                or odata_text(props.get("OrderPriceUnit")),
                net_price_amount=odata_text(props.get("NetPriceAmount")),
                item_text=odata_text(props.get("PurchaseOrderItemText")),
                account_assignment_cat=odata_text(props.get("AccountAssignmentCategory")),
                purchase_requisition=odata_text(props.get("PurchaseRequisition")),
                purchase_requisition_item=odata_text(props.get("PurchaseRequisitionItem")),
                delivery_date=_z_po_item_delivery_date_to_form(props.get("DeliveryDate")),
                material_group=odata_text(props.get("MaterialGroup")),
                tax_code=odata_text(props.get("TaxCode")),
                services=services,
            )
        )
    return header_ref, snapshots


def form_from_z_po_read(
    body: dict[str, Any],
    *,
    seed_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map Z ``GET A_PurchaseOrder`` (+ services expand) → AgentOS PO form."""
    import copy

    from app.procurement.sap_ticket_form_read import _po_header_note

    seed_h: dict[str, Any] = {}
    seed_lines: list[dict[str, Any]] = []
    if isinstance(seed_form, dict):
        if isinstance(seed_form.get("header"), dict):
            seed_h = copy.deepcopy(seed_form["header"])
        if isinstance(seed_form.get("lines"), list):
            seed_lines = [
                copy.deepcopy(row) for row in seed_form["lines"] if isinstance(row, dict)
            ]

    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return {"header": seed_h, "lines": []}

    correspnc = odata_text(root.get("CorrespncInternalReference"))
    sap_texts = po_text_fields_from_sap_root(root)
    resolved_header_note = _po_header_note(correspnc, _norm(seed_h.get("header_note")))
    item_tax = ""
    for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
        props = odata_entity_properties(entry)
        if _po_item_deleted(props):
            continue
        item_tax = odata_text(props.get("TaxCode"))
        if item_tax:
            break

    header = {
        **seed_h,
        "vendor": odata_text(root.get("Supplier")) or seed_h.get("vendor", ""),
        "purchasing_org": odata_text(root.get("PurchasingOrganization"))
        or seed_h.get("purchasing_org", ""),
        "purchasing_group": odata_text(root.get("PurchasingGroup"))
        or seed_h.get("purchasing_group", ""),
        "payment_terms": odata_text(root.get("PaymentTerms"))
        or seed_h.get("payment_terms", ""),
        "header_note": resolved_header_note,
        "tax_code": item_tax or odata_text(root.get("TaxCode")) or seed_h.get("tax_code", ""),
        "requestor_email": odata_text(root.get("SalesPerson"))
        or _norm(seed_h.get("requestor_email")),
        **sap_texts,
    }

    lines: list[dict[str, Any]] = []
    _, snapshots = parse_z_po_items_from_read(body)
    ui_line_no = 10
    hydrated_index = 0
    used_seed_indices: set[int] = set()
    for snap in snapshots:
        if snap.is_deleted:
            continue
        if not snap.services:
            continue
        po_item_no = snap.item_number.lstrip("0") or snap.item_number
        for svc in snap.services:
            seed_line = _yser_po_match_seed_line(
                seed_lines,
                svc,
                line_index=hydrated_index,
                po_item=po_item_no,
                material_group=snap.material_group,
                used=used_seed_indices,
            )
            allocations = _yser_po_allocations_from_read(svc, snap, seed_line)

            line_total = ""
            unit = ""
            try:
                lt = float((svc.net_price_amount or snap.net_price_amount or "0").replace(",", "."))
                if lt > 0:
                    line_total = f"{lt:.2f}"
            except ValueError:
                line_total = svc.net_price_amount or snap.net_price_amount or ""
            try:
                u = float((svc.net_amount or "0").replace(",", "."))
                if u > 0:
                    unit = f"{u:.2f}"
            except ValueError:
                unit = svc.net_amount or ""
            if not unit and line_total and allocations:
                qty_sum = 0.0
                for a in allocations:
                    try:
                        qty_sum += float(str(a.get("qty") or "0").replace(",", "."))
                    except ValueError:
                        pass
                if qty_sum > 0:
                    try:
                        unit = f"{float(line_total) / qty_sum:.2f}"
                    except ValueError:
                        pass

            line_uom = (
                svc.quantity_unit
                or snap.quantity_unit
                or seed_h.get("order_unit")
                or ""
            )
            line: dict[str, Any] = {
                "service": svc.service,
                "short_text": svc.short_text or snap.item_text,
                "delivery_date": _z_po_line_delivery_from_snap(snap, svc),
                "tax_code": _norm(snap.tax_code),
                "unit_price": unit or line_total,
                "net_price": line_total or unit,
                "gross_price": line_total or unit,
                "valuation_price": line_total or unit,
                "order_unit": line_uom,
                "account_assignment_cat": snap.account_assignment_cat,
                "purchase_requisition_item": str(ui_line_no),
                "purchase_order_item": po_item_no,
                "sap_pr_item": snap.purchase_requisition_item.lstrip("0")
                or snap.purchase_requisition_item
                or "",
                "allocations": allocations,
                "_sort_item": int(po_item_no or "0") * 1000 + ui_line_no,
            }
            ext_ref = _norm(svc.purg_doc_item_external_reference)
            if ext_ref:
                line["sap_po_service_ref"] = ext_ref
            elif isinstance(seed_line, dict):
                seed_ref = _yser_po_service_external_ref(seed_line)
                if seed_ref:
                    line["sap_po_service_ref"] = seed_ref
            if snap.material_group:
                line["service_group"] = snap.material_group
            if snap.purchase_requisition:
                line["_sap_purchase_requisition"] = snap.purchase_requisition
            lines.append(line)
            ui_line_no += 10
            hydrated_index += 1

            for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
                props = odata_entity_properties(entry)
                if odata_text(props.get("PurchaseOrderItem")) != snap.item_number:
                    continue
                if not line.get("service_group"):
                    mg = odata_text(props.get("MaterialGroup"))
                    if mg:
                        line["service_group"] = mg
                if not _norm(header.get("plant")):
                    plant = odata_text(props.get("Plant"))
                    if plant:
                        header["plant"] = plant
                if not _norm(header.get("storage_location")):
                    sloc = odata_text(props.get("StorageLocation"))
                    if sloc:
                        header["storage_location"] = sloc
                break

    lines.sort(key=lambda r: int(str(r.get("_sort_item") or "0") or "0"))
    second_pass_used: set[int] = set()
    for i, row in enumerate(lines):
        row.pop("_sort_item", None)
        seed_line = _yser_po_match_seed_line(
            seed_lines,
            SapZPoServiceSnapshot(
                service=_norm(row.get("service")),
                short_text=_norm(row.get("short_text")),
                net_price_amount=_norm(row.get("net_price")),
            ),
            line_index=i,
            po_item=_norm(row.get("purchase_order_item")),
            material_group=_norm(row.get("service_group")),
            used=second_pass_used,
        )
        if not _norm(row.get("delivery_date")):
            row["delivery_date"] = _norm(seed_line.get("delivery_date"))
        if not _norm(row.get("tax_code")):
            row["tax_code"] = _norm(seed_line.get("tax_code"))
    sync_header_catalog_group(header, lines, "YSER")
    sync_header_tax_code(header, lines)
    return {"header": header, "lines": lines}


@dataclass
class ZPoResubmitPlan:
    post_body: dict[str, Any]
    desired_item_numbers: list[str]


def build_z_yser_po_resubmit_plan(
    *,
    form: dict[str, Any],
    po_number: str,
    existing_items: list[SapZPoItemSnapshot],
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    existing_sales_person: str = "",
    existing_po_texts: dict[str, str] | None = None,
    existing_creator_mail_id: str = "",
    existing_correspnc_internal_reference: str = "",
    creator_email: str | None = None,
) -> ZPoResubmitPlan:
    """Build Z POST body for YSER change/delete — delta items and/or header fields."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    pur_org = _norm(header.get("purchasing_org"))
    supplier = _fixed_supplier(header)
    if not supplier:
        raise ValueError("Vendor (Supplier) is required for SAP PO update")
    po_key = _norm(po_number)
    if not po_key:
        raise ValueError("PO number is required for YSER resubmit")

    _ensure_yser_po_service_refs(form, ticket_id=ticket_id)

    active_sap = {
        format_z_po_item_number(s.item_number): s
        for s in existing_items
        if s.item_number and not s.is_deleted
    }

    blocks = yser_effective_line_blocks(form)
    grouped_layout = yser_sap_layout_is_grouped(
        sap_item_count=len(active_sap),
        form_line_count=len(blocks),
    )

    post_items: list[dict[str, Any]] = []
    desired_nos: list[str] = []
    for item_no_str, group_blocks in _iter_yser_po_item_groups(
        form, grouped_layout=grouped_layout
    ):
        resolved_no = _resolve_yser_po_item_number(
            block=group_blocks[0],
            fallback_item_no=item_no_str,
            existing_items=existing_items,
        )
        sap_no = format_z_po_item_number(resolved_no)
        desired_nos.append(sap_no)
        snap = active_sap.get(sap_no)
        line_tax = yser_po_group_item_tax_code(group_blocks, header)
        if snap is None or _z_po_form_group_differs(
            group_blocks, snap, header=header, ticket_id=ticket_id
        ):
            post_items.append(
                _build_z_po_item_row(
                    blocks=group_blocks,
                    header=header,
                    item_number=resolved_no,
                    po_number=po_key,
                    parent_pr_number=parent_pr_number,
                    tax_code=line_tax,
                    ticket_id=None,
                )
            )

    for sap_no in sorted(set(active_sap) - set(desired_nos)):
        snap = active_sap[sap_no]
        delete_tax = _norm(snap.tax_code) or line_tax_code({}, header)
        post_items.append(
            _build_z_po_delete_item_stub(
                snap=snap,
                header=header,
                po_number=po_key,
                tax_code=delete_tax,
            )
        )

    if not post_items:
        from app.procurement.sap_po_payload import build_po_correspnc_internal_reference

        requestor_email = po_requestor_email_for_sap(header)
        sap_texts = existing_po_texts if existing_po_texts is not None else {}
        texts_changed = any(
            _norm(header.get(form_key))[: _po_text_max_len()]
            != _norm(sap_texts.get(sap_key, ""))
            for form_key, sap_key in PO_TEXT_SAP_FIELDS
        )
        email_changed = bool(
            requestor_email and requestor_email != _norm(existing_sales_person)
        )
        _ = existing_creator_mail_id, creator_email  # CreatorMailId is create-only on YSER PO.
        exp_note = build_po_correspnc_internal_reference(_norm(header.get("header_note")))
        note_changed = exp_note != _norm(existing_correspnc_internal_reference)
        if not email_changed and not texts_changed and not note_changed:
            raise ValueError("No YSER PO line changes to submit to SAP")
        # Z API returns 501 without ``to_PurchaseOrderItem`` — resend one unchanged line.
        for item_no_str, group_blocks in _iter_yser_po_item_groups(
            form, grouped_layout=grouped_layout
        ):
            resolved_no = _resolve_yser_po_item_number(
                block=group_blocks[0],
                fallback_item_no=item_no_str,
                existing_items=existing_items,
            )
            sap_no = format_z_po_item_number(resolved_no)
            if sap_no not in active_sap:
                continue
            if sap_no not in desired_nos:
                desired_nos.append(sap_no)
            post_items.append(
                _build_z_po_item_row(
                    blocks=group_blocks,
                    header=header,
                    item_number=resolved_no,
                    po_number=po_key,
                    parent_pr_number=parent_pr_number,
                    tax_code=yser_po_group_item_tax_code(
                        group_blocks,
                        header,
                        sap_tax=_norm(active_sap.get(sap_no).tax_code)
                        if sap_no in active_sap
                        else "",
                    ),
                    ticket_id=None,
                )
            )
            break
        else:
            raise ValueError("No YSER PO line changes to submit to SAP")

    inner: dict[str, Any] = {
        "PurchaseOrder": po_key,
        "PurchaseOrderType": "YSER",
        "CompanyCode": _company_code(header, pur_org),
        "PurchasingGroup": _norm(header.get("purchasing_group")),
        "PurchasingOrganization": pur_org,
        "Supplier": supplier,
    }
    if post_items:
        inner["to_PurchaseOrderItem"] = post_items
    pay_terms = _z_po_payment_terms(header)
    if pay_terms:
        inner["PaymentTerms"] = pay_terms
    requestor_email = po_requestor_email_for_sap(header)
    if requestor_email:
        inner["SalesPerson"] = requestor_email
    apply_yser_po_texts_to_z_inner(inner, header)

    return ZPoResubmitPlan(
        post_body=_wrap_z_po_nav_lists(inner),
        desired_item_numbers=desired_nos,
    )


def verify_z_po_read_against_form(
    body: dict[str, Any],
    *,
    form: dict[str, Any],
    ticket_id: str | None = None,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
) -> list[str]:
    del parent_pr_number  # PR link checked on payload create, not on field-level GET verify.
    mismatches: list[str] = []
    hdr = form.get("header") if isinstance(form.get("header"), dict) else {}
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    exp_email = po_requestor_email_for_sap(hdr)
    if exp_email and isinstance(root, dict):
        sap_email = _norm(odata_text(root.get("SalesPerson")))
        if sap_email and sap_email != exp_email:
            mismatches.append(f"SalesPerson SAP {sap_email!r} != form {exp_email!r}")
    exp_creator = po_creator_mail_id_for_sap(creator_email)
    if exp_creator and isinstance(root, dict):
        sap_creator = _norm(odata_text(root.get("CreatorMailId")))
        if sap_creator and sap_creator != exp_creator:
            mismatches.append(
                f"CreatorMailId SAP {sap_creator!r} != creator {exp_creator!r}"
            )
    if isinstance(root, dict):
        mismatches.extend(verify_po_texts_against_form(root, header=hdr, document_type="YSER"))
        from app.procurement.sap_po_payload import build_po_correspnc_internal_reference
        from app.procurement.sap_ticket_form_read import _po_header_note

        exp_note = build_po_correspnc_internal_reference(_norm(hdr.get("header_note")))
        sap_note = _po_header_note(odata_text(root.get("CorrespncInternalReference")), exp_note)
        # QAS Z GET may omit CorrespncInternalReference — do not fail verify on empty SAP.
        if sap_note and exp_note and sap_note != exp_note:
            mismatches.append(
                f"CorrespncInternalReference SAP {sap_note!r} != form {exp_note!r}"
            )
    _, snapshots = parse_z_po_items_from_read(body)

    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    matched_tokens: set[tuple[str, str, str, str]] = set()
    for block in lines:
        if not isinstance(block, dict):
            continue
        exp_service = normalize_service_performer_code(_norm(block.get("service")))
        if not exp_service:
            continue
        hit = _yser_po_find_sap_service_for_block(snapshots, block)
        if not hit:
            mismatches.append(f"service {exp_service}: missing on SAP")
            continue
        item_no, sap_svc, sap_snap = hit
        matched_tokens.add(
            (format_z_po_item_number(item_no), *_yser_po_service_match_key(sap_svc))
        )
        if not _yser_po_alloc_pairs_match(block, sap_svc, sap_snap):
            mismatches.append(
                f"service {exp_service} (item {format_z_po_item_number(item_no)}): "
                f"allocations SAP {_yser_po_read_alloc_pairs(sap_svc, sap_snap)!r} "
                f"!= form {_form_alloc_pairs(block)!r}"
            )
        exp_delivery = _norm(block.get("delivery_date"))
        sap_delivery = (
            _z_po_line_delivery_from_snap(sap_snap, sap_svc) if sap_snap else ""
        )
        if exp_delivery != sap_delivery and (exp_delivery or sap_delivery):
            mismatches.append(
                f"service {exp_service}: delivery SAP {sap_delivery!r} != {exp_delivery!r}"
            )
        exp_grp = line_catalog_group(block, hdr, "YSER")
        sap_grp = _norm(sap_snap.material_group) if sap_snap else ""
        if exp_grp and sap_grp and exp_grp != sap_grp:
            mismatches.append(
                f"service {exp_service}: service_group SAP {sap_grp!r} != {exp_grp!r}"
            )

    for snap in snapshots:
        if snap.is_deleted:
            continue
        item_no = format_z_po_item_number(snap.item_number)
        for svc in snap.services:
            token = (item_no, *_yser_po_service_match_key(svc))
            if token in matched_tokens:
                continue
            code = normalize_service_performer_code(svc.service)
            mismatches.append(
                f"service {code}: extra on SAP item {item_no} ({svc.short_text!r}, {svc.net_price_amount!r}) not in form"
            )
    return mismatches
