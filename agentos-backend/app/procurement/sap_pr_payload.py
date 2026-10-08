"""Map AgentOS procurement forms to SAP API_PURCHASEREQ_PROCESS_SRV payloads."""

from __future__ import annotations  # PrItemUpdatePatch forward refs in dataclasses

import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from app.procurement.line_catalog_group import line_catalog_group
from app.procurement.sap_odata_utils import (
    is_sap_deleted_flag as _is_sap_deleted_flag,
    odata_base_root as _base_root,
    odata_entity_key,
    odata_entity_properties as _odata_entity_properties,
    odata_norm as _norm,
    odata_results_list as _odata_results_list,
    odata_text as _odata_text,
    storage_location_for_sap,
)
from app.procurement.sap_yast_acct import (
    ACCOUNT_ASSIGNMENT_ASSET,
    ACCOUNT_ASSIGNMENT_COST_CENTER,
    allocations_from_sap_acct_node,
    material_account_assignment_category,
    yast_acct_extra_fields,
    yast_acct_row_should_emit,
    yast_optional_gl_fields,
)

_PR_HEADER_KEY_RE = re.compile(
    r"A_PurchaseRequisitionHeader\('((?:''|[^'])*)'\)", re.IGNORECASE
)
_PR_HEADER_KEY_LEGACY_RE = re.compile(r"PRHeaderSet\('((?:''|[^'])*)'\)", re.IGNORECASE)

PR_SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/"
PR_HEADER_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionHeader"
PR_ITEM_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionItem"
PR_ACCT_COLLECTION = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurReqnAcctAssgmt"

def normalize_pr_item_number(raw: Any) -> str:
    """Canonical PR item number for matching (``00010`` and ``10`` → ``10``)."""
    s = _norm(raw)
    if not s:
        return ""
    if s.isdigit():
        return str(int(s))
    return s


def _norm_material_key(raw: str) -> str:
    s = _norm(raw)
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def _allocation_cc_set(block: dict[str, Any]) -> frozenset[str]:
    allocs = block.get("allocations")
    if not isinstance(allocs, list):
        return frozenset()
    return frozenset(
        _norm(a.get("cost_center"))
        for a in allocs
        if isinstance(a, dict) and _norm(a.get("cost_center"))
    )


def _pr_line_identity(document_type: str, block: dict[str, Any]) -> tuple[str, str, frozenset[str]]:
    dt = (document_type or "").upper()
    if dt == "YSER":
        return ("service", _norm(block.get("service")), _allocation_cc_set(block))
    return ("material", _norm_material_key(block.get("material")), _allocation_cc_set(block))


def _next_free_pr_item_number(occupied: set[str]) -> str:
    n = 10
    while str(n) in occupied:
        n += 10
    return str(n)


def _match_form_line_to_sap_item(
    *,
    block: dict[str, Any],
    document_type: str,
    active_by_no: dict[str, SapPrItemSnapshot],
    used_item_nos: set[str],
) -> str | None:
    """Map a form line to an existing SAP item number (explicit id, then material/service + CC)."""
    explicit = normalize_pr_item_number(block.get("purchase_requisition_item"))
    if explicit and explicit in active_by_no and explicit not in used_item_nos:
        return explicit

    kind, key, ccs = _pr_line_identity(document_type, block)
    if not key:
        return None

    candidates: list[SapPrItemSnapshot] = []
    for item_no, snap in active_by_no.items():
        if item_no in used_item_nos:
            continue
        if kind == "service":
            snap_svc = _norm(snap.service_performer)
            if snap_svc and snap_svc != key:
                continue
        else:
            snap_mat = _norm_material_key(snap.material)
            if snap_mat and snap_mat != key:
                continue
        snap_ccs = frozenset(snap.cost_centers)
        if ccs and snap_ccs and ccs != snap_ccs:
            continue
        candidates.append(snap)

    if not candidates:
        return None

    def _item_sort_key(s: SapPrItemSnapshot) -> int:
        n = normalize_pr_item_number(s.item_number)
        return int(n) if n.isdigit() else 0

    candidates.sort(key=_item_sort_key)
    if len(candidates) == 1:
        return normalize_pr_item_number(candidates[0].item_number)
    if ccs:
        for snap in candidates:
            if frozenset(snap.cost_centers) == ccs:
                return normalize_pr_item_number(snap.item_number)
    return normalize_pr_item_number(candidates[0].item_number)


def _iter_pr_form_lines(
    form: dict[str, Any],
    *,
    document_type: str,
    existing_items: list[SapPrItemSnapshot] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """(PurchaseRequisitionItem, line block) — reuse SAP item numbers on resubmit when possible."""
    dt = (document_type or "").upper()
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []
    active_by_no = {
        normalize_pr_item_number(s.item_number): s
        for s in (existing_items or [])
        if s.item_number and not s.is_deleted
    }
    used: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    yser_line_idx = 0

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

        if dt == "YSER":
            from app.procurement.sap_pr_z_payload import yser_pr_item_number_for_block

            out.append(
                (
                    yser_pr_item_number_for_block(block, line_index=yser_line_idx),
                    block,
                )
            )
            yser_line_idx += 1
            continue

        item_no = _match_form_line_to_sap_item(
            block=block,
            document_type=dt,
            active_by_no=active_by_no,
            used_item_nos=used,
        )
        if not item_no:
            unused_sap = sorted(
                (n for n in active_by_no if n not in used),
                key=lambda x: int(x) if x.isdigit() else 0,
            )
            if unused_sap:
                item_no = unused_sap[0]
            else:
                item_no = _next_free_pr_item_number(used | set(active_by_no.keys()))
        used.add(item_no)
        out.append((item_no, block))

    return out


def sap_service_root_url(base_url: str) -> str:
    """OData service root — CSRF fetch (GET + ``X-CSRF-Token: Fetch``)."""
    return f"{_base_root(base_url)}{PR_SERVICE_PATH}"


def pr_collection_url(base_url: str) -> str:
    """POST here to create a PR (deep insert on ``to_PurchaseReqnItem``)."""
    return f"{_base_root(base_url)}{PR_HEADER_COLLECTION}"


def pr_item_collection_url(base_url: str) -> str:
    """PR item entity set (recovery scan on ``PurchaseRequisitionItemText``)."""
    return f"{_base_root(base_url)}{PR_ITEM_COLLECTION}"


def pr_header_url(base_url: str) -> str:
    return pr_collection_url(base)


def pr_entity_url(base_url: str, pr_number: str) -> str:
    """GET/PATCH header: ``A_PurchaseRequisitionHeader('PR')``."""
    key = odata_entity_key(pr_number)
    if not key:
        raise ValueError("PR number is required for entity URL")
    return f"{_base_root(base_url)}{PR_HEADER_COLLECTION}('{key}')"


def pr_item_entity_url(base_url: str, *, pr_number: str, item_number: str) -> str:
    """PATCH item: ``A_PurchaseRequisitionItem(PurchaseRequisition='…',PurchaseRequisitionItem='…')``."""
    pr_key = odata_entity_key(pr_number)
    item_key = odata_entity_key(item_number)
    if not pr_key or not item_key:
        raise ValueError("PR number and item number are required")
    return (
        f"{_base_root(base_url)}{PR_ITEM_COLLECTION}"
        f"(PurchaseRequisition='{pr_key}',PurchaseRequisitionItem='{item_key}')"
    )


def pr_acct_assgmt_entity_url(
    base_url: str,
    *,
    pr_number: str,
    item_number: str,
    acct_assgmt_number: str,
) -> str:
    """PATCH account assignment segment for a PR item."""
    pr_key = odata_entity_key(pr_number)
    item_key = odata_entity_key(item_number)
    acct_key = odata_entity_key(acct_assgmt_number)
    if not pr_key or not item_key or not acct_key:
        raise ValueError("PR number, item number, and account assignment number are required")
    return (
        f"{_base_root(base_url)}{PR_ACCT_COLLECTION}"
        f"(PurchaseRequisition='{pr_key}',PurchaseRequisitionItem='{item_key}',"
        f"PurchaseReqnAcctAssgmtNumber='{acct_key}')"
    )


def pr_read_url(base_url: str, pr_number: str) -> str:
    """GET header with items and account assignments (full read for resubmit / verify)."""
    return pr_read_full_url(base_url, pr_number)


def pr_read_full_url(base_url: str, pr_number: str) -> str:
    """GET PR header with items + nested account assignments."""
    return (
        f"{pr_entity_url(base_url, pr_number)}"
        "?$expand=to_PurchaseReqnItem/to_PurchaseReqnAcctAssgmt"
    )


def pr_item_collection_post_url(base_url: str) -> str:
    """POST a new item onto an existing PR."""
    return f"{_base_root(base_url)}{PR_ITEM_COLLECTION}"


def pr_acct_collection_post_url(base_url: str) -> str:
    """POST a new account-assignment segment onto an existing PR item."""
    return f"{_base_root(base_url)}{PR_ACCT_COLLECTION}"


# SAP text fields (PurReqnDescription, Z item ShortText) — MaxLength=40 on QAS.
PUR_REQN_DESCRIPTION_MAX_LEN = 40
SAP_TEXT_FIELD_MAX_LEN = PUR_REQN_DESCRIPTION_MAX_LEN


def sap_ticket_uuid_hex(ticket_id: str) -> str:
    """32-char hex (no dashes) from ticket UUID."""
    return _norm(ticket_id).replace("-", "")


def compact_sap_ticket_ref_tag(ticket_id: str) -> str:
    """Pre-May-2026 compact tag ``[AO:8HEX]`` — recovery only."""
    raw = sap_ticket_uuid_hex(ticket_id)
    if not raw:
        return ""
    return f"[AO:{raw[:8].upper()}]"


def sap_ticket_ref_tag(ticket_id: str) -> str:
    """Full-UUID marker for idempotency (37 chars: ``[AO:32HEX]``)."""
    raw = sap_ticket_uuid_hex(ticket_id)
    if len(raw) == 32:
        return f"[AO:{raw.upper()}]"
    return compact_sap_ticket_ref_tag(ticket_id)


def legacy_sap_ticket_ref_tag(ticket_id: str) -> str:
    """Legacy tag with dashed UUID — recovery of older SAP documents."""
    tid = _norm(ticket_id)
    return f"[AgentOS:{tid}]" if tid else ""


def sap_ticket_correspnc_alnum_marker(ticket_id: str, *, compact: bool = False) -> str:
    """Alphanumeric idempotency marker (no brackets): full ``AO{32HEX}`` or compact ``AO{8HEX}``."""
    raw = sap_ticket_uuid_hex(ticket_id)
    if not raw:
        return ""
    if compact:
        return f"AO{raw[:8].upper()}"
    return f"AO{raw.upper()}"


def sap_ticket_ext_system_marker(ticket_id: str) -> str:
    """60-char SAP fields: ``PurReqnExternalSystemId``, Z ``Extsourcesystem`` (``AO`` + 32 hex)."""
    return sap_ticket_correspnc_alnum_marker(ticket_id, compact=False)


def sap_ticket_correspnc_external_marker(ticket_id: str) -> str:
    """12-char PO ``CorrespncExternalReference`` (``AO`` + 8 hex)."""
    return sap_ticket_correspnc_alnum_marker(ticket_id, compact=True)


def recovery_tags_for_ticket(ticket_id: str) -> list[str]:
    """All tag strings this ticket may have used in SAP (newest format first)."""
    tags: list[str] = []
    for t in (
        sap_ticket_ext_system_marker(ticket_id),
        sap_ticket_correspnc_external_marker(ticket_id),
        sap_ticket_ref_tag(ticket_id),
        compact_sap_ticket_ref_tag(ticket_id),
        legacy_sap_ticket_ref_tag(ticket_id),
        sap_ticket_correspnc_alnum_marker(ticket_id),
        sap_ticket_correspnc_alnum_marker(ticket_id, compact=True),
    ):
        if t and t not in tags:
            tags.append(t)
    return tags


_AO_MARKER_RE = re.compile(r"\[AO:[0-9A-F]+\]", re.IGNORECASE)
_AO_ALNUM_MARKER_RE = re.compile(r"AO[0-9A-F]{8}(?:[0-9A-F]{24})?", re.IGNORECASE)
_LEGACY_AGENTOS_MARKER_RE = re.compile(r"\[AgentOS:[^\]]+\]")


def normalize_recovery_marker(marker: str) -> str:
    """Canonical form for ``[AO:…]`` markers (uppercase hex); legacy tags unchanged."""
    m = _norm(marker)
    if not m:
        return ""
    ao = re.fullmatch(r"\[AO:([0-9A-F]+)\]", m, re.IGNORECASE)
    if ao:
        return f"[AO:{ao.group(1).upper()}]"
    return m


def agentos_markers_in_text(text: str) -> list[str]:
    """Extract complete AgentOS bracket tokens (not arbitrary substrings)."""
    if not text:
        return []
    markers: list[str] = []
    for match in _AO_MARKER_RE.finditer(text):
        markers.append(normalize_recovery_marker(match.group(0)))
    for match in _AO_ALNUM_MARKER_RE.finditer(text):
        markers.append(match.group(0).upper())
    for match in _LEGACY_AGENTOS_MARKER_RE.finditer(text):
        markers.append(match.group(0))
    return markers


def recovery_tag_set_for_ticket(ticket_id: str) -> frozenset[str]:
    """Normalized lookup set for recovery — AO tags uppercased, legacy exact."""
    out: set[str] = set()
    for t in recovery_tags_for_ticket(ticket_id):
        if t.upper().startswith("[AO:"):
            out.add(normalize_recovery_marker(t))
        elif t.upper().startswith("AO") and t[2:].isalnum():
            out.add(t.upper())
        else:
            out.add(t)
    return frozenset(out)


def _odata_result_props(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    props = row.get("properties")
    if isinstance(props, dict):
        return props
    return row


def pick_recovery_document_by_tag(
    results: list[Any],
    *,
    number_key: str,
    text_keys: tuple[str, ...],
    ticket_id: str | None = None,
    tags: list[str] | None = None,
) -> str | None:
    """Match only **complete** ``[AO:…]`` / ``[AgentOS:…]`` tokens (avoids compact-in-full false positives)."""
    if ticket_id:
        wanted = recovery_tag_set_for_ticket(ticket_id)
    elif tags:
        wanted = frozenset(
            normalize_recovery_marker(t) if t.upper().startswith("[AO:") else t
            for t in tags
            if t
        )
    else:
        return None
    if not wanted or not results:
        return None
    for row in results:
        props = _odata_result_props(row)
        if not props:
            continue
        blob = " ".join(_norm(props.get(k)) for k in text_keys)
        if not blob:
            continue
        found = _blob_matches_recovery_tags(blob, wanted)
        if not found:
            continue
        doc_no = _norm(props.get(number_key))
        if doc_no and not is_placeholder_pr_number(doc_no):
            return doc_no
    return None


def pick_recovery_document_by_field(
    results: list[Any],
    *,
    number_key: str,
    field_key: str,
    ticket_id: str,
) -> str | None:
    """Exact match on a dedicated SAP external/trace field (not free text)."""
    wanted = {
        sap_ticket_ext_system_marker(ticket_id).upper(),
        sap_ticket_correspnc_external_marker(ticket_id).upper(),
    }
    wanted.discard("")
    if not wanted or not results:
        return None
    for row in results:
        props = _odata_result_props(row)
        val = _norm(props.get(field_key)).upper()
        if val not in wanted:
            continue
        doc_no = _norm(props.get(number_key))
        if doc_no and not is_placeholder_pr_number(doc_no):
            return doc_no
    return None


def build_sap_text_with_ticket_tag(
    text: str, *, ticket_id: str | None, max_len: int = SAP_TEXT_FIELD_MAX_LEN
) -> str:
    """User text plus full ticket tag; tag is never truncated (remaining room for note)."""
    note = _norm(text)
    if not ticket_id:
        return note[:max_len]
    tag = sap_ticket_ref_tag(ticket_id)
    if not tag:
        return note[:max_len]
    if any(m in recovery_tag_set_for_ticket(ticket_id) for m in agentos_markers_in_text(note)):
        return note[:max_len]
    if not note:
        return tag[:max_len]
    sep = " "
    room = max_len - len(sep) - len(tag)
    if room <= 0:
        return tag[:max_len]
    return f"{note[:room]}{sep}{tag}"


def strip_agentos_bracket_markers_from_text(text: str) -> str:
    """Remove ``[AO:…]`` / ``[AgentOS:…]`` tokens from user-visible header text."""
    if not text:
        return ""
    out = _LEGACY_AGENTOS_MARKER_RE.sub("", text)
    out = _AO_MARKER_RE.sub("", out)
    return " ".join(out.split()).strip()


def strip_pr_item_text_marker(text: str) -> str:
    """Remove trailing compact ``AO{HEX}`` idempotency marker from PR/PO item text."""
    c = _norm(text)
    if not c:
        return ""
    m = re.match(r"^(.*)(AO[0-9A-F]{8}(?:[0-9A-F]{24})?)$", c, re.IGNORECASE)
    if m and m.group(1):
        return m.group(1).rstrip()
    if re.fullmatch(r"AO[0-9A-F]{8}(?:[0-9A-F]{24})?", c, re.IGNORECASE):
        return ""
    return c


def pr_item_trace_marker(ticket_id: str) -> str:
    """Legacy PR line-text suffix (``AO`` + 32 hex) — recovery of pre-external-field creates."""
    return sap_ticket_ext_system_marker(ticket_id)


def _blob_matches_recovery_tags(blob: str, wanted: frozenset[str]) -> bool:
    """Bracket tags: complete tokens; ``AO{hex}`` suffixes: end of field only (PR item text)."""
    if not blob or not wanted:
        return False
    upper = blob.upper()
    bracket_found = {
        m.upper()
        for m in agentos_markers_in_text(blob)
        if m.upper().startswith("[AO:") or m.upper().startswith("[AGENTOS:")
    }
    if bracket_found & {w.upper() for w in wanted if w.upper().startswith("[")}:
        return True
    for w in wanted:
        wu = w.upper()
        if wu.startswith("AO") and not wu.startswith("[") and upper.endswith(wu):
            return True
    return False


def build_pr_item_text_for_sap(
    short_text: str,
    *,
    ticket_id: str | None = None,
    include_trace: bool = False,
) -> str:
    """``PurchaseRequisitionItemText`` — OPS short text (legacy ``include_trace`` for recovery tests)."""
    base = _norm(short_text) or "PR"
    if not include_trace or not ticket_id:
        return base[:SAP_TEXT_FIELD_MAX_LEN]
    marker = pr_item_trace_marker(ticket_id)
    if not marker:
        return base[:SAP_TEXT_FIELD_MAX_LEN]
    if any(m in recovery_tag_set_for_ticket(ticket_id) for m in agentos_markers_in_text(base)):
        return base[:SAP_TEXT_FIELD_MAX_LEN]
    room = SAP_TEXT_FIELD_MAX_LEN - len(marker)
    if room <= 0:
        return marker[:SAP_TEXT_FIELD_MAX_LEN]
    return f"{base[:room]}{marker}"


def build_pur_reqn_description(header_note: str, *, ticket_id: str | None = None) -> str:
    """YUNB/YAST ``PurReqnDescription`` — OPS header note only (max 40); trace is on line 1 item text."""
    _ = ticket_id
    return _norm(header_note)[:PUR_REQN_DESCRIPTION_MAX_LEN]


def is_placeholder_pr_number(pr_number: str | None) -> bool:
    """SAP sometimes returns ``#       1`` on HTTP 201 when lines fail validation."""
    s = _norm(pr_number)
    if not s:
        return False
    return s.startswith("#") or s.replace(" ", "").replace("#", "") in ("", "1")


def _sap_date(raw: str) -> str | None:
    """OData V2 date: ``/Date(ms)/`` when parseable, else ``None`` (omit field)."""
    from app.procurement.sap_odata_utils import form_delivery_date_is_valid

    s = _norm(raw)
    if not form_delivery_date_is_valid(s):
        return None
    try:
        if "-" in s:
            dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(s[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        ms = int(dt.timestamp() * 1000)
        if ms <= 0:
            return None
        return f"/Date({ms})/"
    except ValueError:
        return None


def _allocation_qty_sum(allocs: list[Any]) -> float:
    """Sum allocation qty values (decimals allowed — see PR All multi-CC doc e.g. 9.200 + 13.800)."""
    total = 0.0
    for alloc in allocs:
        if not isinstance(alloc, dict):
            continue
        q = _norm(alloc.get("qty"))
        if not q:
            continue
        try:
            v = float(q.replace(",", "."))
        except ValueError:
            continue
        if v > 0:
            total += v
    return total


def _requested_quantity_from_allocs(allocs: list[Any]) -> str:
    """Item ``RequestedQuantity`` = sum of allocation qtys when multiple cost centres."""
    total = _allocation_qty_sum(allocs)
    if total > 0:
        return _sap_quantity(str(total)) or "1.000"
    return "1.000"


def _sap_quantity(qty: str) -> str:
    s = _norm(qty)
    if not s:
        return ""
    try:
        return f"{float(s):.3f}"
    except ValueError:
        return s



def _material_group(header: dict[str, Any], document_type: str) -> str:
    """Legacy header-only group (prefer ``line_catalog_group`` for outbound items)."""
    dt = (document_type or "").upper()
    if dt == "YSER":
        return _norm(header.get("service_group"))
    return _norm(header.get("material_group"))


def _allocation_qty_total_float(block: dict[str, Any]) -> float:
    allocs = block.get("allocations")
    if not isinstance(allocs, list):
        return 0.0
    return _allocation_qty_sum(allocs)


def _format_pr_unit_price(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{round(value, 2):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _purchase_requisition_price_for_sap(block: dict[str, Any]) -> str:
    """SAP ``PurchaseRequisitionPrice`` (BAPRE) — valuation price per price unit, not line total."""
    for key in ("unit_price", "gross_price"):
        v = _norm(block.get(key))
        if v:
            return v
    val = _norm(block.get("valuation_price"))
    if not val:
        return ""
    qty = _allocation_qty_total_float(block)
    if qty > 0:
        try:
            return _format_pr_unit_price(float(val.replace(",", ".")) / qty)
        except ValueError:
            pass
    return val


def _fixed_supplier(header: dict[str, Any]) -> str:
    v = _norm(header.get("vendor"))
    if "|" in v:
        return v.split("|", 1)[0].strip()
    return v


def _company_code(header: dict[str, Any], pur_org: str) -> str:
    cc = _norm(header.get("company_code"))
    return cc or pur_org


def _item_category(document_type: str, item_category: str) -> str:
    """YSER on standard API used cat 9 historically; production YSER uses Z API (cat ``D``)."""
    if (document_type or "").upper() == "YSER":
        return "9"
    if _norm(item_category):
        return _norm(item_category)
    return "0"


def _account_assignment_category(document_type: str, form_value: str) -> str:
    return material_account_assignment_category(document_type, form_value)


def _base_unit(block: dict[str, Any]) -> str:
    """``order_unit`` on the line (from UI / reference master); default QT when missing."""
    u = _norm(block.get("order_unit"))
    return u or "QT"


def order_unit_from_reference_extra(extra: Any) -> str:
    """Read UoM from reference ``extra`` (material/service master import)."""
    if not isinstance(extra, dict):
        return ""
    for key in (
        "base_unit",
        "order_unit",
        "Order Unit",
        "Base Unit of Measure",
        "Base unit",
        "Unit",
    ):
        v = extra.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _build_acct_assgmts(
    *,
    pr_number: str,
    item_number: str,
    block: dict[str, Any],
    document_type: str,
    allocs: list[dict[str, Any]],
    header: dict[str, Any] | None = None,
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
            "PurchaseRequisition": _norm(pr_number),
            "PurchaseRequisitionItem": item_number,
            "PurchaseReqnAcctAssgmtNumber": str(seq),
            "Quantity": qty,
        }
        if dt == "YAST":
            row.update(yast_acct_extra_fields(asset=row_asset))
            row.update(yast_optional_gl_fields(header=header, block=block))
        elif cc:
            row["CostCenter"] = cc
        rows.append(row)
        seq += 1
    if dt == "YAST" and line_asset and not rows:
        qty = _sap_quantity(
            _requested_quantity_from_allocs(allocs if isinstance(allocs, list) else [])
        ) or "1.000"
        rows.append(
            {
                "PurchaseRequisition": _norm(pr_number),
                "PurchaseRequisitionItem": item_number,
                "PurchaseReqnAcctAssgmtNumber": "1",
                "Quantity": qty,
                **yast_acct_extra_fields(asset=line_asset),
                **yast_optional_gl_fields(header=header, block=block),
            }
        )
    return rows


def _build_item_row(
    *,
    block: dict[str, Any],
    document_type: str,
    header: dict[str, Any],
    item_number: int,
    pr_number: str,
    mat_grp: str,
    fixed_supplier: str,
) -> dict[str, Any]:
    dt = (document_type or "").upper()
    pur_org = _norm(header.get("purchasing_org"))
    pur_group = _norm(header.get("purchasing_group"))
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(
        _norm(header.get("storage_location")), plant=plant
    )
    company = _company_code(header, pur_org)

    material = _norm(block.get("material")) if dt != "YSER" else ""
    service_no = _norm(block.get("service")) if dt == "YSER" else ""
    short_text = build_pr_item_text_for_sap(_norm(block.get("short_text")))
    delivery = _sap_date(_norm(block.get("delivery_date")))
    act_cat = _account_assignment_category(dt, _norm(block.get("account_assignment_cat")))
    item_cat = _item_category(dt, _norm(block.get("item_category")))
    pr_unit_price = _purchase_requisition_price_for_sap(block)
    base_unit = _base_unit(block)
    product_type = "2" if dt == "YSER" else "1"

    allocs = block.get("allocations")
    if not isinstance(allocs, list) or not allocs:
        allocs = [{}]

    item_no_str = str(item_number)
    row: dict[str, Any] = {
        "PurchaseRequisition": _norm(pr_number),
        "PurchaseRequisitionItem": item_no_str,
        "PurchaseRequisitionType": dt,
        "PurchasingOrganization": pur_org,
        "PurchasingGroup": pur_group,
        "Plant": plant,
        "CompanyCode": company,
        "AccountAssignmentCategory": act_cat,
        "PurchasingDocumentItemCategory": item_cat,
        "ProductType": product_type,
        "Material": material,
        "MaterialGroup": mat_grp,
        "StorageLocation": sloc,
        "RequestedQuantity": _requested_quantity_from_allocs(allocs),
        "BaseUnit": base_unit,
        "PurchaseRequisitionPrice": pr_unit_price,
        "PurchaseRequisitionItemText": short_text,
        "FixedSupplier": fixed_supplier,
    }
    if dt == "YSER" and service_no:
        row["ServicePerformer"] = service_no
    if delivery:
        row["DeliveryDate"] = delivery

    acct_rows = _build_acct_assgmts(
        pr_number=pr_number,
        item_number=item_no_str,
        block=block,
        document_type=dt,
        allocs=allocs,
        header=header,
    )
    if acct_rows:
        row["to_PurchaseReqnAcctAssgmt"] = acct_rows
        row["MultipleAcctAssgmtDistribution"] = "1" if len(acct_rows) > 1 else ""

    return row


def build_pr_payload(
    *,
    form: dict[str, Any],
    document_type: str,
    pr_number: str = "",
    ticket_id: str | None = None,
) -> dict[str, Any]:
    """Build POST body for ``A_PurchaseRequisitionHeader`` (deep insert)."""
    dt = (document_type or "").upper()
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []

    header_note = build_pur_reqn_description(
        _norm(header.get("header_note")), ticket_id=ticket_id
    )
    fixed_supplier = _fixed_supplier(header)
    pr_key = _norm(pr_number)

    items: list[dict[str, Any]] = []
    line_pairs = list(_iter_pr_form_lines(form, document_type=dt))
    trace_item_no = line_pairs[0][0] if line_pairs else ""
    for item_no_str, block in line_pairs:
        row = _build_item_row(
            block=block,
            document_type=dt,
            header=header,
            item_number=int(item_no_str),
            pr_number=pr_key,
            mat_grp=line_catalog_group(block, header, dt),
            fixed_supplier=fixed_supplier,
        )
        if ticket_id and item_no_str == trace_item_no:
            marker = sap_ticket_ext_system_marker(ticket_id)
            if marker:
                row["PurReqnExternalSystemId"] = marker
        items.append(row)

    if not items:
        raise ValueError("No PR line items to send to SAP (empty blocks or allocations).")

    payload: dict[str, Any] = {
        "PurchaseRequisition": pr_key,
        "PurchaseRequisitionType": dt,
        "PurReqnDescription": header_note,
        "SourceDetermination": False,
        "to_PurchaseReqnItem": items,
    }
    return payload


@dataclass
class SapAcctSegment:
    seq: str
    cost_center: str
    quantity: str = ""
    master_asset: str = ""
    purg_doc_net_amount: str = ""


def _pr_delivery_date_from_props(props: dict[str, Any]) -> str:
    from app.procurement.sap_odata_utils import odata_date_to_form

    return odata_date_to_form(props.get("DeliveryDate"))


@dataclass
class SapPrItemSnapshot:
    item_number: str
    is_deleted: bool
    material: str = ""
    service_performer: str = ""
    requested_quantity: str = ""
    item_text: str = ""
    delivery_date: str = ""
    plant: str = ""
    storage_location: str = ""
    unit_price: str = ""
    material_group: str = ""
    base_unit: str = ""
    purchasing_group: str = ""
    purchasing_organization: str = ""
    company_code: str = ""
    fixed_supplier: str = ""
    cost_centers: list[str] = field(default_factory=list)
    acct_qty_by_cc: dict[str, str] = field(default_factory=dict)
    acct_segments: list[SapAcctSegment] = field(default_factory=list)


def parse_pr_items_from_read(body: dict[str, Any]) -> tuple[str, list[SapPrItemSnapshot]]:
    """Parse OData GET ``A_PurchaseRequisitionHeader`` (+ expanded items)."""
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return "", []
    header_note = _odata_text(root.get("PurReqnDescription"))
    items_node = root.get("to_PurchaseReqnItem")
    snapshots: list[SapPrItemSnapshot] = []
    for entry in _odata_results_list(items_node):
        props = _odata_entity_properties(entry)
        item_no = normalize_pr_item_number(
            _odata_text(props.get("PurchaseRequisitionItem"))
        )
        if not item_no:
            continue
        deleted = _is_sap_deleted_flag(_odata_text(props.get("IsDeleted")))
        acct_node = props.get("to_PurchaseReqnAcctAssgmt")
        if acct_node is None and isinstance(entry.get("to_PurchaseReqnAcctAssgmt"), dict):
            acct_node = entry.get("to_PurchaseReqnAcctAssgmt")
        ccs: list[str] = []
        acct_qty: dict[str, str] = {}
        acct_segments: list[SapAcctSegment] = []
        for acct_entry in _odata_results_list(acct_node):
            ap = _odata_entity_properties(acct_entry)
            seq = _odata_text(ap.get("PurchaseReqnAcctAssgmtNumber"))
            cc = _odata_text(ap.get("CostCenter"))
            qty = _odata_text(ap.get("Quantity"))
            asset = _odata_text(ap.get("MasterFixedAsset"))
            if seq and (cc or qty or asset):
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
            SapPrItemSnapshot(
                item_number=item_no,
                is_deleted=deleted,
                material=_odata_text(props.get("Material")),
                service_performer=_odata_text(props.get("ServicePerformer")),
                requested_quantity=_odata_text(props.get("RequestedQuantity")),
                item_text=_odata_text(props.get("PurchaseRequisitionItemText")),
                delivery_date=_pr_delivery_date_from_props(props),
                plant=_odata_text(props.get("Plant")),
                storage_location=_odata_text(props.get("StorageLocation")),
                unit_price=_odata_text(props.get("PurchaseRequisitionPrice")),
                material_group=_odata_text(props.get("MaterialGroup")),
                base_unit=_odata_text(props.get("BaseUnit")),
                purchasing_group=_odata_text(props.get("PurchasingGroup")),
                purchasing_organization=_odata_text(
                    props.get("PurchasingOrganization")
                ),
                company_code=_odata_text(props.get("CompanyCode")),
                fixed_supplier=_odata_text(props.get("FixedSupplier")),
                cost_centers=ccs,
                acct_qty_by_cc=acct_qty,
                acct_segments=acct_segments,
            )
        )
    return header_note, snapshots


def build_pr_item_delete_patch() -> dict[str, str]:
    """PR API: items are not HTTP-deleted; mark ``IsDeleted`` (PR All / PROCESS_SRV)."""
    return {"IsDeleted": "X"}


def build_pr_acct_delete_patch() -> dict[str, str]:
    """Account assignment removal — same pattern as line items when DELETE is not supported."""
    return {"IsDeleted": "X"}


def _max_acct_seq_number(segments: list[SapAcctSegment]) -> int:
    mx = 0
    for seg in segments:
        if seg.seq.isdigit():
            mx = max(mx, int(seg.seq))
    return mx


def _acct_patch_body(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in acct.items()
        if k
        not in (
            "PurchaseRequisition",
            "PurchaseRequisitionItem",
            "PurchaseReqnAcctAssgmtNumber",
        )
    }


def _acct_post_body(acct: dict[str, Any], *, seq: int) -> dict[str, Any]:
    body = _acct_patch_body(acct)
    body["PurchaseReqnAcctAssgmtNumber"] = str(seq)
    return body


@dataclass
class PrResubmitPlan:
    header_patch: dict[str, Any]
    item_patches: list[PrItemUpdatePatch]
    items_to_mark_deleted: list[str]
    items_to_create: list[dict[str, Any]]
    desired_item_numbers: list[str]


def build_pr_resubmit_plan(
    *,
    form: dict[str, Any],
    document_type: str,
    pr_number: str,
    existing_items: list[SapPrItemSnapshot],
    ticket_id: str | None = None,
) -> PrResubmitPlan:
    """Diff form lines vs SAP items: PATCH / mark-deleted / POST new lines."""
    header_patch = build_pr_header_patch(form=form, ticket_id=ticket_id)
    item_patches = build_pr_item_patches(
        form=form,
        document_type=document_type,
        pr_number=pr_number,
        existing_items=existing_items,
        ticket_id=ticket_id,
    )
    desired_nos = [p[0] for p in item_patches]
    active_sap = {
        normalize_pr_item_number(s.item_number)
        for s in existing_items
        if s.item_number and not s.is_deleted
    }
    desired_set = set(desired_nos)
    to_delete = sorted(
        active_sap - desired_set,
        key=lambda x: int(x) if x.isdigit() else 0,
        reverse=True,
    )

    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    fixed_supplier = _fixed_supplier(header)
    pr_key = _norm(pr_number)
    to_create: list[dict[str, Any]] = []
    line_pairs = list(
        _iter_pr_form_lines(
            form, document_type=document_type, existing_items=existing_items
        )
    )
    for item_no_str, block in line_pairs:
        if item_no_str not in active_sap:
            to_create.append(
                _build_item_row(
                    block=block,
                    document_type=(document_type or "").upper(),
                    header=header,
                    item_number=int(item_no_str),
                    pr_number=pr_key,
                    mat_grp=line_catalog_group(block, header, document_type),
                    fixed_supplier=fixed_supplier,
                )
            )

    return PrResubmitPlan(
        header_patch=header_patch,
        item_patches=item_patches,
        items_to_mark_deleted=to_delete,
        items_to_create=to_create,
        desired_item_numbers=desired_nos,
    )


def verify_pr_read_against_form(
    body: dict[str, Any],
    *,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str | None = None,
) -> list[str]:
    """Compare SAP GET (full expand) to normalized form; return human-readable mismatches."""
    header_note, snapshots = parse_pr_items_from_read(body)
    mismatches: list[str] = []
    hdr = form.get("header") if isinstance(form.get("header"), dict) else {}
    expected_note = build_pur_reqn_description(
        _norm(hdr.get("header_note")), ticket_id=ticket_id
    )
    sap_header_clean = strip_agentos_bracket_markers_from_text(header_note)
    if expected_note and sap_header_clean != expected_note:
        legacy_tag_only_header = (
            ticket_id
            and not sap_header_clean
            and bool(agentos_markers_in_text(header_note))
        )
        if not legacy_tag_only_header:
            mismatches.append(
                f"header_note: SAP {header_note!r} != form {expected_note!r}"
            )

    dt = (document_type or "").upper()
    active_by_no = {
        normalize_pr_item_number(s.item_number): s
        for s in snapshots
        if s.item_number and not s.is_deleted
    }
    used_sap: set[str] = set()
    line_pairs = list(_iter_pr_form_lines(form, document_type=dt, existing_items=snapshots))

    for item_no, block in line_pairs:
        sap = active_by_no.get(item_no)
        if not sap:
            mismatches.append(f"item {item_no}: missing on SAP (or marked deleted)")
            continue
        used_sap.add(item_no)

        if dt == "YSER":
            exp_id = _norm(block.get("service"))
            if exp_id and _norm(sap.service_performer) and sap.service_performer != exp_id:
                mismatches.append(
                    f"item {item_no}: ServicePerformer SAP {sap.service_performer!r} != {exp_id!r}"
                )
        else:
            exp_mat = _norm_material_key(block.get("material"))
            sap_mat = _norm_material_key(sap.material)
            if exp_mat and sap_mat and sap_mat != exp_mat:
                mismatches.append(
                    f"item {item_no}: Material SAP {sap.material!r} != {exp_mat!r}"
                )

        if dt != "YAST":
            exp_ccs = sorted(_allocation_cc_set(block))
            sap_ccs = sorted(sap.cost_centers)
            if exp_ccs and sap_ccs and sap_ccs != exp_ccs:
                mismatches.append(
                    f"item {item_no}: cost centers SAP {sap_ccs!r} != form {exp_ccs!r}"
                )

        exp_short = _norm(block.get("short_text"))
        sap_text = sap.item_text or ""
        if exp_short:
            sap_plain = strip_pr_item_text_marker(sap_text)
            exp_plain = exp_short[:SAP_TEXT_FIELD_MAX_LEN]
            if sap_plain and sap_plain != exp_plain:
                mismatches.append(
                    f"item {item_no}: item text SAP {sap_text!r} != form {exp_short!r}"
                )

        from app.procurement.sap_odata_utils import sanitize_form_delivery_date

        exp_dd = sanitize_form_delivery_date(_norm(block.get("delivery_date")))
        sap_dd = sanitize_form_delivery_date(sap.delivery_date)
        if exp_dd and sap_dd and exp_dd != sap_dd:
            mismatches.append(
                f"item {item_no}: delivery_date SAP {sap_dd!r} != form {exp_dd!r}"
            )

    for item_no in sorted(active_by_no.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        if item_no not in used_sap:
            mismatches.append(f"item {item_no}: extra active line on SAP not in form")

    return mismatches


def build_pr_header_patch(*, form: dict[str, Any], ticket_id: str | None) -> dict[str, Any]:
    """PATCH header — only ``PurReqnDescription`` is writable per PR All API doc."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    return {
        "PurReqnDescription": build_pur_reqn_description(
            _norm(header.get("header_note")), ticket_id=ticket_id
        )
    }


# Resubmit: (item_no, item_patch, acct PATCHs, acct POSTs, acct delete seqs)
PrItemUpdatePatch = tuple[
    str,
    dict[str, Any],
    list[tuple[str, dict[str, Any]]],
    list[dict[str, Any]],
    list[str],
]


def build_pr_item_patches(
    *,
    form: dict[str, Any],
    document_type: str,
    pr_number: str,
    existing_items: list[SapPrItemSnapshot] | None = None,
    ticket_id: str | None = None,
) -> list[PrItemUpdatePatch]:
    """Item + account-assignment diff per line: match SAP segments by cost centre, not form seq."""
    dt = (document_type or "").upper()
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    fixed_supplier = _fixed_supplier(header)
    pr_key = _norm(pr_number)
    existing_by_item = {
        normalize_pr_item_number(s.item_number): s
        for s in (existing_items or [])
        if s.item_number and not s.is_deleted
    }
    patches: list[PrItemUpdatePatch] = []
    line_pairs = list(
        _iter_pr_form_lines(form, document_type=dt, existing_items=existing_items)
    )
    for item_no, block in line_pairs:
        allocs = block.get("allocations")
        if not isinstance(allocs, list):
            allocs = []
        acct_rows = _build_acct_assgmts(
            pr_number=pr_key,
            item_number=item_no,
            block=block,
            document_type=dt,
            allocs=allocs,
            header=header,
        )
        item_row = _build_item_row(
            block=block,
            document_type=dt,
            header=header,
            item_number=int(item_no),
            pr_number=pr_key,
            mat_grp=line_catalog_group(block, header, dt),
            fixed_supplier=fixed_supplier,
        )
        item_body = {
            k: v for k, v in item_row.items() if k != "to_PurchaseReqnAcctAssgmt"
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
                from app.procurement.sap_yast_acct import asset_codes_equal

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
                    # Sequential fallback when SAP omitted MasterFixedAsset on a segment.
                    sap_seg = next(
                        (s for s in sap_segments if s.seq not in matched_seqs),
                        None,
                    )
            elif dt == "YAST":
                sap_seg = next(
                    (s for s in sap_segments if s.seq not in matched_seqs),
                    None,
                )
            if sap_seg:
                matched_seqs.add(sap_seg.seq)
                acct_patch = _acct_patch_body(acct)
                qty = _norm(acct_patch.get("Quantity"))
                unchanged = True
                if cc and cc != sap_seg.cost_center:
                    unchanged = False
                asset_changed = False
                if dt == "YAST" and asset:
                    from app.procurement.sap_yast_acct import asset_codes_equal

                    if sap_seg.master_asset and not asset_codes_equal(
                        asset, sap_seg.master_asset
                    ):
                        unchanged = False
                        asset_changed = True
                    elif not sap_seg.master_asset:
                        unchanged = False
                        asset_changed = True
                if qty and sap_seg.quantity:
                    try:
                        qty_same = float(qty.replace(",", ".")) == float(
                            sap_seg.quantity.replace(",", ".")
                        )
                    except ValueError:
                        qty_same = qty == sap_seg.quantity
                    if not qty_same:
                        unchanged = False
                elif qty and not sap_seg.quantity:
                    unchanged = False
                if unchanged:
                    continue
                if dt == "YAST" and asset and not asset_changed:
                    # Asset identity already matches SAP — patch qty only.
                    acct_patch = {
                        k: v
                        for k, v in acct_patch.items()
                        if k in ("Quantity", "BaseUnit", "PurchaseOrderQuantityUnit")
                    }
                if acct_patch:
                    acct_patches.append((sap_seg.seq, acct_patch))
            else:
                acct_creates.append(_acct_post_body(acct, seq=next_seq))
                next_seq += 1

        for seg in sap_segments:
            if seg.seq and seg.seq not in matched_seqs:
                acct_deletes.append(seg.seq)

        patches.append((item_no, item_body, acct_patches, acct_creates, acct_deletes))
    return patches


def _pr_number_from_metadata(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    for key in ("id", "uri"):
        raw = meta.get(key)
        if not isinstance(raw, str):
            continue
        for pattern in (_PR_HEADER_KEY_RE, _PR_HEADER_KEY_LEGACY_RE):
            m = pattern.search(raw)
            if m:
                return m.group(1).replace("''", "'").strip() or None
    return None


def parse_pr_number_from_response(body: Any) -> str | None:
    """Extract ``PurchaseRequisition`` / ``PRNumber`` from OData JSON."""
    if not isinstance(body, dict):
        return None

    def _from_props(props: Any) -> str | None:
        if not isinstance(props, dict):
            return None
        for field in ("PurchaseRequisition", "PRNumber"):
            raw = props.get(field)
            if isinstance(raw, dict):
                t = raw.get("__text") or raw.get("#text") or raw.get("value")
                if t is not None:
                    s = str(t).strip()
                    return s or None
            if isinstance(raw, str):
                s = raw.strip()
                return s or None
        return None

    entry = body.get("entry")
    if isinstance(entry, dict):
        content = entry.get("content")
        if isinstance(content, dict):
            found = _from_props(content.get("properties"))
            if found:
                return found
        found = _from_props(entry.get("properties"))
        if found:
            return found

    d = body.get("d")
    if isinstance(d, dict):
        found = _from_props(d)
        if found:
            return found
        found = _pr_number_from_metadata(d.get("__metadata"))
        if found:
            return found

    found = _from_props(body)
    if found:
        return found

    for field in ("PurchaseRequisition", "PRNumber"):
        s = _norm(body.get(field))
        if s:
            return s
    return None
