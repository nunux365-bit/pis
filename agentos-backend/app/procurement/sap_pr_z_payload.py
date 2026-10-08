"""Map AgentOS forms to SAP ``Z_PURCHASE_REQUISITION_SRV`` (YSER Service PR).

**Numbering contract (Service PR doc + SAP EBKN / DZEBKN):**

- ``PRItem`` (``00010``) — item-overview / ``service_group`` bucket (``BNFPO``). Not the same as
  ``PRAcctAssgmtNumber``.
- ``PRAcctAssgmtNumber`` — account-assignment **serial** (``ZEBKN`` / DZEBKN) within one ``PRItem``.
  One serial per **CC split row**, restarting at ``01`` for each service (``01``, ``02`` per service).
  Same shape on CREATE, GET, and UPDATE — SAP normalizes all accepted create variants to this.
- Service identity — ``Service`` field (create) / ``ServiceNumber`` (some GET shapes); not encoded in
  ``PRAcctAssgmtNumber``.

**Price / qty contract:** create — service ``GrossPrice`` (unit), item ``ValuationPrice`` empty,
``DistrQuantity`` = absolute allocation qty (same as update). Update — item ``ValuationPrice`` (sum of
line totals), service ``GrossPrice`` (per-service line total on every CC row), ``DistrQuantity`` =
absolute allocation qty. **Create / grouped update:** one SAP item per ``service_group``; each UI
service line → one ``to_Services`` row (CC splits = extra rows on that line). **Legacy SAP docs** (one
item per UI line) stay on per-line layout on update. Read — qty from ``DistrQuantity``; amounts from
``ValuationPrice`` or ``GrossPrice``.

**Update (Jul 2026):** incremental Z POSTs via :mod:`app.procurement.sap_pr_z_update` — one mutation per
call; ``PurDocItemExternalReference`` identifies services; ``IsDeleted`` / ``IsAcctAssgmtDeleted`` for
service/CC delete. Create remains one batch POST.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.procurement.line_catalog_group import line_catalog_group, sync_header_catalog_group
from app.procurement.sap_odata_utils import (
    is_sap_deleted_flag,
    odata_date_to_form,
    odata_entity_properties,
    odata_norm as _norm,
    odata_results_list,
    odata_text,
    storage_location_for_sap,
)
from app.procurement.sap_pr_payload import (
    SAP_TEXT_FIELD_MAX_LEN,
    normalize_pr_item_number,
    parse_pr_number_from_response,
    sap_ticket_ext_system_marker,
    strip_agentos_bracket_markers_from_text,
    strip_pr_item_text_marker,
)


def build_z_header_note(header_note: str, *, ticket_id: str | None = None) -> str:
    """YSER Z header ``PurReqnDescription`` — OPS note only (max 40); trace is on line 1 ``Extsourcesystem``."""
    _ = ticket_id
    return _norm(header_note)[:SAP_TEXT_FIELD_MAX_LEN]

# Hardcoded per integration spec (Service PR.docx).
Z_PR_SERVICE_PATH = "/sap/opu/odata/sap/Z_PURCHASE_REQUISITION_SRV/"
Z_PR_HEADER_COLLECTION = "/sap/opu/odata/sap/Z_PURCHASE_REQUISITION_SRV/PRHeaderSet"
Z_PR_ITEM_COLLECTION = "/sap/opu/odata/sap/Z_PURCHASE_REQUISITION_SRV/PRItemSet"


def z_pr_item_collection_url(base_url: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root

    return f"{odata_base_root(base_url)}{Z_PR_ITEM_COLLECTION}"


Z_ITEM_CATEGORY_SERVICE = "D"
Z_ACCOUNT_ASSIGNMENT_CAT = "K"
# QAS Z service PR: header item qty is one package unit; splits use absolute ``DistrQuantity``.
Z_ITEM_HEADER_QUANTITY = "1.000"
YSER_PR_SERVICE_EXT_REF_MAX_LEN = 35


def _yser_pr_service_external_ref(block: dict[str, Any]) -> str:
    return _norm(block.get("sap_pr_service_ref"))[:YSER_PR_SERVICE_EXT_REF_MAX_LEN]


def _ensure_yser_pr_service_refs(
    form: dict[str, Any],
    *,
    ticket_id: str | None,
    only_without_item: bool = False,
    min_ref_seq: int = 0,
) -> None:
    """Assign stable ``sap_pr_service_ref`` per UI line when missing (create / new lines on update)."""
    import re

    seq = max(0, int(min_ref_seq or 0))
    for block in yser_effective_line_blocks(form):
        if not isinstance(block, dict):
            continue
        ref = _yser_pr_service_external_ref(block)
        if ref:
            m = re.search(r"-(\d+)$", ref)
            if m:
                seq = max(seq, int(m.group(1)))
    for block in yser_effective_line_blocks(form):
        if not isinstance(block, dict):
            continue
        if _yser_pr_service_external_ref(block):
            continue
        if not ticket_id:
            continue
        seq += 1
        marker = sap_ticket_ext_system_marker(ticket_id) or _norm(ticket_id)
        suffix = f"-{seq:03d}"
        prefix = "AOLINE-"
        max_marker = max(1, YSER_PR_SERVICE_EXT_REF_MAX_LEN - len(prefix) - len(suffix))
        block["sap_pr_service_ref"] = f"{prefix}{marker[:max_marker]}{suffix}"


def validate_yser_form_service_refs_unique(form: dict[str, Any]) -> str | None:
    """Fail fast when duplicate ``sap_pr_service_ref`` would break update identity."""
    seen: dict[str, int] = {}
    for idx, block in enumerate(yser_effective_line_blocks(form), start=1):
        ref = _yser_pr_service_external_ref(block)
        if not ref:
            continue
        if ref in seen:
            return (
                f"duplicate sap_pr_service_ref {ref!r} on lines {seen[ref]} and {idx}"
            )
        seen[ref] = idx
    return None


def format_z_pr_acct_assgmt_number(row_index: int) -> str:
    """Format SAP account-assignment serial (``01``, ``02``, …) per CC split within a service."""
    if row_index < 1:
        row_index = 1
    return str(row_index).zfill(2)


def normalize_pr_acct_assgmt_number(raw: str) -> str:
    s = _norm(raw)
    if not s or s == "00":
        return ""
    return s.zfill(2) if s.isdigit() else s


def resolve_block_pr_acct_assgmt_number(
    block: dict[str, Any], *, service_index: int
) -> str:
    """First allocation seq on the service line (UI identity / verify)."""
    allocs = block.get("allocations")
    if isinstance(allocs, list):
        for alloc in allocs:
            if not isinstance(alloc, dict):
                continue
            acct = normalize_pr_acct_assgmt_number(
                _norm(alloc.get("pr_acct_assgmt_number"))
            )
            if acct:
                return acct
    raw = _norm(block.get("pr_acct_assgmt_number"))
    if raw and raw != "00":
        return normalize_pr_acct_assgmt_number(raw)
    return format_z_pr_acct_assgmt_number(service_index)


def _alloc_pr_acct_assgmt_number(
    alloc: dict[str, Any], *, fallback_seq: int
) -> tuple[str, int]:
    """Assign the next free SAP seq when no explicit number is on the allocation."""
    acct = normalize_pr_acct_assgmt_number(_norm(alloc.get("pr_acct_assgmt_number")))
    if acct:
        return acct, fallback_seq
    return format_z_pr_acct_assgmt_number(fallback_seq), fallback_seq + 1


@dataclass
class SapZServiceRow:
    """One inline ``to_Services`` row on a Z YSER PR item."""

    pr_acct_assgmt_number: str
    service: str
    cost_center: str
    quantity: str = ""
    is_deleted: bool = False


def count_z_pr_sap_items(body: dict[str, Any] | None) -> int:
    """Count active (non-deleted) ``to_Items`` rows on a resolved Z PR read body."""
    if not isinstance(body, dict):
        return 0
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return 0
    n = 0
    for entry in odata_results_list(root.get("to_PRItemSet") or root.get("to_Items")):
        props = odata_entity_properties(entry)
        if is_sap_deleted_flag(odata_text(props.get("IsDeleted"))):
            continue
        n += 1
    return n


def yser_max_service_ref_seq_from_z_read(body: dict[str, Any] | None) -> int:
    """Highest ``-NNN`` suffix on ``PurDocItemExternalReference`` from a Z PR GET."""
    import re

    if not isinstance(body, dict):
        return 0
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return 0
    seq = 0
    for item in odata_results_list(root.get("to_PRItemSet") or root.get("to_Items")):
        if not isinstance(item, dict):
            continue
        ip = odata_entity_properties(item)
        for svc in odata_results_list(ip.get("to_Services")):
            if not isinstance(svc, dict):
                continue
            sp = odata_entity_properties(svc)
            ref = odata_text(sp.get("PurDocItemExternalReference"))
            if not ref:
                continue
            m = re.search(r"-(\d+)$", ref)
            if m:
                seq = max(seq, int(m.group(1)))
    return seq


def yser_sap_layout_is_grouped(*, sap_item_count: int, form_line_count: int) -> bool:
    """Grouped layout: multiple UI services on a single SAP ``PRItem``."""
    if form_line_count <= 1:
        return False
    return sap_item_count == 1


def yser_group_blocks_by_service_group(
    blocks: list[dict[str, Any]],
    header: dict[str, Any],
) -> list[list[tuple[int, dict[str, Any]]]]:
    """Preserve UI order; bucket lines by ``service_group`` (line value, else header)."""
    groups: OrderedDict[str, list[tuple[int, dict[str, Any]]]] = OrderedDict()
    for idx, block in enumerate(blocks):
        grp = line_catalog_group(block, header, "YSER") or f"__ungrouped_{idx}"
        groups.setdefault(grp, []).append((idx, block))
    return list(groups.values())


def _yser_pr_service_cluster_key(sp: dict[str, Any]) -> tuple[str, str, str, str]:
    """Cluster identity: service+text+qty+ext ref.

    CC splits share one ``PurDocItemExternalReference``; duplicate service codes on
    separate UI lines have distinct refs and must not merge.
    """
    return (
        _service_code_from_row(sp),
        odata_text(sp.get("ShortText")),
        odata_text(sp.get("Quantity")),
        odata_text(sp.get("PurDocItemExternalReference")),
    )


def cluster_yser_pr_service_entries(
    svc_entries: list[Any],
) -> list[list[dict[str, Any]]]:
    """Cluster ``to_Services`` rows into UI lines.

    Rows with the same service+text+qty+ext ref and *distinct* cost centers are one line
    (CC split). Otherwise each SAP row is its own UI line (supports duplicate service codes).
    """
    buckets: OrderedDict[tuple[str, str, str, str], list[dict[str, Any]]] = OrderedDict()
    for svc_entry in svc_entries:
        if not isinstance(svc_entry, dict):
            continue
        sp = odata_entity_properties(svc_entry)
        key = _yser_pr_service_cluster_key(sp)
        if not key[0]:
            continue
        buckets.setdefault(key, []).append(svc_entry)

    clusters: list[list[dict[str, Any]]] = []
    for rows in buckets.values():
        if len(rows) == 1:
            clusters.append(rows)
            continue
        ccs = [
            _norm(odata_entity_properties(row).get("CostCenter"))
            for row in rows
        ]
        grosses = {
            _norm(odata_entity_properties(row).get("GrossPrice")) for row in rows
        }
        grosses.discard("")
        # Legacy rows without ext ref: same service+qty+CC pattern but different unit
        # prices are separate UI lines, not a CC split.
        if len(grosses) > 1:
            for row in rows:
                clusters.append([row])
            continue
        if all(ccs) and len(set(ccs)) == len(ccs):
            clusters.append(rows)
            continue
        for row in rows:
            clusters.append([row])
    return clusters


def yser_line_identity_key(block: dict[str, Any]) -> tuple[str, str, str]:
    """Stable UI line match for update merge (supports duplicate service codes)."""
    item_no = normalize_pr_item_number(_norm(block.get("purchase_requisition_item")))
    svc = normalize_service_performer_code(_norm(block.get("service")))
    acct = normalize_pr_acct_assgmt_number(_norm(block.get("pr_acct_assgmt_number")))
    return (item_no, svc, acct)


def yser_effective_line_blocks(form: dict[str, Any]) -> list[dict[str, Any]]:
    """UI service lines that would be sent on Z create/update (non-empty line or CC)."""
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    user_header_note = _norm(header.get("header_note"))
    blocks: list[dict[str, Any]] = []
    for block in lines_in:
        if not isinstance(block, dict):
            continue
        allocs = block.get("allocations")
        if not isinstance(allocs, list) or not allocs:
            allocs = [{}]
        service_no = normalize_service_performer_code(_norm(block.get("service")))
        short_text = _norm(block.get("short_text")) or user_header_note[:SAP_TEXT_FIELD_MAX_LEN]
        has_line = bool(service_no or short_text)
        has_alloc = any(
            isinstance(a, dict) and _norm(a.get("cost_center")) for a in allocs
        )
        if has_line or has_alloc:
            blocks.append(block)
    return blocks


def yser_pr_item_number_for_block(
    block: dict[str, Any],
    *,
    line_index: int,
) -> str:
    """Canonical SAP ``PRItem`` digits for one UI service line (``10``, ``20``, …).

    Prefer hydrated ``sap_pr_item`` / ``purchase_requisition_item`` from GET on update.
    """
    for key in ("sap_pr_item", "purchase_requisition_item"):
        raw = _norm(block.get(key))
        if raw:
            return normalize_pr_item_number(raw)
    return str((line_index + 1) * 10)


def yser_sap_item_numbers(sap_form: dict[str, Any]) -> set[str]:
    """Hydrated SAP ``PRItem`` numbers on a read form (not inferred from line index)."""
    out: set[str] = set()
    for idx, block in enumerate(yser_effective_line_blocks(sap_form)):
        raw = _norm(block.get("sap_pr_item") or block.get("purchase_requisition_item"))
        if raw:
            out.add(normalize_pr_item_number(raw))
    return out


def yser_assign_update_item_numbers(
    form: dict[str, Any],
    *,
    sap_form: dict[str, Any],
    sap_item_count: int,
) -> None:
    """Stamp ``purchase_requisition_item`` on lines missing it (update mutations).

    Grouped SAP layout (fewer items than UI lines): new services share the existing
  ``PRItem`` for their ``service_group``. Legacy layout: assign next ``+10`` item.
    """
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    sap_blocks = yser_effective_line_blocks(sap_form)
    sub_blocks = yser_effective_line_blocks(form)
    grouped = yser_sap_layout_is_grouped(
        sap_item_count=sap_item_count,
        form_line_count=max(len(sap_blocks), len(sub_blocks)),
    )

    item_by_group: dict[str, str] = {}
    for idx, block in enumerate(sap_blocks):
        grp = line_catalog_group(block, header, "YSER") or "__default"
        item = yser_pr_item_number_for_block(block, line_index=idx)
        if item and grp not in item_by_group:
            item_by_group[grp] = item
    default_item = (
        yser_pr_item_number_for_block(sap_blocks[0], line_index=0) if sap_blocks else ""
    )

    max_sap_item = 0
    for raw in yser_sap_item_numbers(sap_form):
        try:
            max_sap_item = max(max_sap_item, int(raw))
        except ValueError:
            pass
    if sap_item_count > 0:
        max_sap_item = max(max_sap_item, sap_item_count * 10)

    for idx, block in enumerate(sub_blocks):
        if _norm(block.get("sap_pr_item") or block.get("purchase_requisition_item")):
            continue
        if grouped:
            grp = line_catalog_group(block, header, "YSER") or "__default"
            item = item_by_group.get(grp) or default_item
        else:
            item = str(max_sap_item + 10) if max_sap_item else str((idx + 1) * 10)
        if item:
            block["purchase_requisition_item"] = item
            block["sap_pr_item"] = item


def yser_form_item_numbers(form: dict[str, Any]) -> set[str]:
    """Item numbers inferred from the form (hydrated or line-index fallback)."""
    blocks = yser_effective_line_blocks(form)
    return {
        yser_pr_item_number_for_block(block, line_index=idx)
        for idx, block in enumerate(blocks)
    }


def yser_resolved_form_item_numbers(
    form: dict[str, Any],
    *,
    sap_form: dict[str, Any] | None = None,
) -> tuple[set[str], str | None]:
    """SAP ``PRItem`` numbers to keep after line delete.

  Prefer hydrated ``sap_pr_item`` / ``purchase_requisition_item``. When multiple SAP items
  exist, resolve missing numbers from ``sap_form`` (service + CC match) or fail fast.
    """
    blocks = yser_effective_line_blocks(form)
    if not blocks:
        return set(), None

    sap_lines = (
        sap_form.get("lines")
        if isinstance(sap_form, dict) and isinstance(sap_form.get("lines"), list)
        else []
    )

    multi_item = len(sap_lines) > 1
    keep: set[str] = set()
    for idx, block in enumerate(blocks):
        resolved = ""
        for key in ("sap_pr_item", "purchase_requisition_item"):
            raw = _norm(block.get(key))
            if raw:
                resolved = normalize_pr_item_number(raw)
                break
        if not resolved and sap_lines:
            svc = normalize_service_performer_code(_norm(block.get("service")))
            exp_ccs = _yser_line_cc_set(block)
            if svc:
                for sap_row in sap_lines:
                    if not isinstance(sap_row, dict):
                        continue
                    if normalize_service_performer_code(_norm(sap_row.get("service"))) != svc:
                        continue
                    if _yser_line_cc_set(sap_row) != exp_ccs:
                        continue
                    resolved = normalize_pr_item_number(
                        _norm(sap_row.get("purchase_requisition_item"))
                        or _norm(sap_row.get("sap_pr_item"))
                    )
                    if resolved:
                        break
                if not resolved:
                    sap_items = yser_sap_item_numbers(sap_form) if sap_form else set()
                    grouped = yser_sap_layout_is_grouped(
                        sap_item_count=len(sap_items),
                        form_line_count=max(len(sap_lines), len(blocks)),
                    )
                    if grouped or len(sap_items) <= 1:
                        for sap_row in sap_lines:
                            if not isinstance(sap_row, dict):
                                continue
                            if normalize_service_performer_code(_norm(sap_row.get("service"))) != svc:
                                continue
                            resolved = normalize_pr_item_number(
                                _norm(sap_row.get("purchase_requisition_item"))
                                or _norm(sap_row.get("sap_pr_item"))
                            )
                            if resolved:
                                break
        if resolved:
            keep.add(resolved)
            continue
        if multi_item:
            svc = normalize_service_performer_code(_norm(block.get("service")))
            return set(), (
                f"Cannot resolve SAP item number for service line {svc or idx + 1}: "
                "hydrate from SAP (sap_pr_item) before deleting lines."
            )
        keep.add(yser_pr_item_number_for_block(block, line_index=idx))
    return keep, None


def yser_shared_pr_item_number(
    form: dict[str, Any],
    *,
    existing_item_numbers: list[str] | None = None,
) -> str:
    """First YSER item number on the form (legacy helper)."""
    blocks = yser_effective_line_blocks(form)
    if blocks:
        return yser_pr_item_number_for_block(blocks[0], line_index=0)
    for raw_item in existing_item_numbers or []:
        item_no = normalize_pr_item_number(raw_item)
        if item_no:
            return item_no
    return "10"


def _z_pr_item_key_for_block(block: dict[str, Any], *, line_index: int) -> str:
    """SAP Z 5-digit item number (``00010``, ``00020``, …) for one service line."""
    digits = yser_pr_item_number_for_block(block, line_index=line_index)
    return format_z_pr_item_number(int(digits))


def _yser_line_cc_set(block: dict[str, Any]) -> frozenset[str]:
    allocs = block.get("allocations")
    if not isinstance(allocs, list):
        return frozenset()
    return frozenset(
        _norm(a.get("cost_center"))
        for a in allocs
        if isinstance(a, dict) and _norm(a.get("cost_center"))
    )


def _yser_group_service_map(
    form: dict[str, Any],
) -> dict[str, set[str]]:
    """``service_group`` → set of normalized service performer codes."""
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    out: dict[str, set[str]] = {}
    for block in yser_effective_line_blocks(form):
        grp = line_catalog_group(block, header, "YSER") or "__default"
        svc = normalize_service_performer_code(_norm(block.get("service")))
        if svc:
            out.setdefault(grp, set()).add(svc)
    return out


def _yser_service_cc_map(form: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Normalized service code → cost-center set (one entry per UI line)."""
    out: dict[str, frozenset[str]] = {}
    for block in yser_effective_line_blocks(form):
        svc = normalize_service_performer_code(_norm(block.get("service")))
        if not svc:
            continue
        ccs = _yser_line_cc_set(block)
        if svc in out and out[svc] != ccs:
            # duplicate performer lines — union CCs for comparison
            out[svc] = frozenset(set(out[svc]) | set(ccs))
        else:
            out[svc] = ccs
    return out


def merge_yser_update_form_with_sap(
    submitted: dict[str, Any],
    *,
    sap_form: dict[str, Any],
) -> dict[str, Any]:
    """Apply UI structural edits on top of SAP-hydrated amounts (Z update POST)."""
    import copy

    out = copy.deepcopy(submitted)
    sap_lines = sap_form.get("lines") if isinstance(sap_form.get("lines"), list) else []
    sap_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    sap_by_ref: dict[str, dict[str, Any]] = {}
    sap_by_svc_grp: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sap_lines:
        if not isinstance(row, dict):
            continue
        ref = _yser_pr_service_external_ref(row)
        if ref:
            sap_by_ref[ref] = row
        sap_by_key[yser_line_identity_key(row)] = row
        svc = normalize_service_performer_code(_norm(row.get("service")))
        grp = _norm(row.get("service_group"))
        if svc and grp:
            sap_by_svc_grp[(svc, grp)] = row

    merged_lines: list[dict[str, Any]] = []
    used_ext_refs: set[str] = set()
    for idx, block in enumerate(yser_effective_line_blocks(submitted), start=1):
        sap_row: dict[str, Any] | None = None
        ref = _yser_pr_service_external_ref(block)
        if ref:
            sap_row = sap_by_ref.get(ref)
        if sap_row is None:
            key = yser_line_identity_key(block)
            sap_row = sap_by_key.get(key)
        if sap_row is None:
            svc = normalize_service_performer_code(_norm(block.get("service")))
            grp = _norm(block.get("service_group"))
            if svc and grp:
                candidate = sap_by_svc_grp.get((svc, grp))
                if candidate is not None:
                    cref = _yser_pr_service_external_ref(candidate)
                    if not cref or cref not in used_ext_refs:
                        sap_row = candidate
        if sap_row is None and _norm(block.get("purchase_requisition_item")):
            # Fallback: same PRItem + service (duplicate CC splits on one service).
            pri = normalize_pr_item_number(_norm(block.get("purchase_requisition_item")))
            svc = normalize_service_performer_code(_norm(block.get("service")))
            for sap_line in sap_lines:
                if not isinstance(sap_line, dict):
                    continue
                if normalize_pr_item_number(_norm(sap_line.get("purchase_requisition_item"))) != pri:
                    continue
                if svc and normalize_service_performer_code(_norm(sap_line.get("service"))) != svc:
                    continue
                sap_row = sap_line
                break
        if sap_row is None:
            svc = normalize_service_performer_code(_norm(block.get("service")))
            if svc:
                svc_matches = [
                    sap_line
                    for sap_line in sap_lines
                    if isinstance(sap_line, dict)
                    and normalize_service_performer_code(_norm(sap_line.get("service"))) == svc
                ]
                if len(svc_matches) == 1:
                    candidate = svc_matches[0]
                    cref = _yser_pr_service_external_ref(candidate)
                    if not cref or cref not in used_ext_refs:
                        sap_row = candidate
        if (
            sap_row is None
            and len(sap_lines) == 1
            and len(yser_effective_line_blocks(submitted)) == 1
            and isinstance(sap_lines[0], dict)
        ):
            sap_row = sap_lines[0]
        line = copy.deepcopy(sap_row) if sap_row else copy.deepcopy(block)
        if sap_row:
            for field in ("purchase_requisition_item", "sap_pr_item"):
                if _norm(sap_row.get(field)):
                    line[field] = sap_row[field]
        if _norm(block.get("service")):
            line["service"] = block.get("service")
        if sap_row:
            sap_ref = _norm(sap_row.get("sap_pr_service_ref"))
            sap_svc = normalize_service_performer_code(_norm(sap_row.get("service")))
            sub_svc = normalize_service_performer_code(_norm(line.get("service")))
            if sap_ref and sap_svc and sub_svc and sap_svc == sub_svc:
                if sap_ref not in used_ext_refs:
                    line["sap_pr_service_ref"] = sap_ref
                    used_ext_refs.add(sap_ref)
                else:
                    line.pop("sap_pr_service_ref", None)
            elif sap_svc and sub_svc and sap_svc != sub_svc:
                line.pop("sap_pr_service_ref", None)
        for field in ("short_text", "delivery_date", "order_unit", "service_group"):
            if _norm(block.get(field)):
                line[field] = block[field]
        for field in ("unit_price", "valuation_price", "gross_price"):
            if _norm(block.get(field)):
                line[field] = block[field]
        line["pr_acct_assgmt_number"] = resolve_block_pr_acct_assgmt_number(
            {**line, **block}, service_index=idx
        )
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
            # Resubmit: keep SAP-persisted split qty (e.g. 0.999+2.001), not UI integers.
            qty = _norm(src.get("qty")) or _norm(alloc.get("qty")) or "1"
            row: dict[str, Any] = {"cost_center": cc, "qty": qty}
            acct = normalize_pr_acct_assgmt_number(
                _norm(src.get("pr_acct_assgmt_number"))
                or _norm(alloc.get("pr_acct_assgmt_number"))
            )
            if acct:
                row["pr_acct_assgmt_number"] = acct
            new_allocs.append(row)
        line["allocations"] = new_allocs
        line["pr_acct_assgmt_number"] = resolve_block_pr_acct_assgmt_number(
            line, service_index=idx
        )
        merged_lines.append(line)

    out["lines"] = merged_lines
    return out


def _match_yser_read_row_for_block(
    block: dict[str, Any],
    read_lines: list[dict[str, Any]],
    read_by_item_service: dict[tuple[str, str], dict[str, Any]],
    *,
    line_index: int,
) -> dict[str, Any] | None:
    """Match one UI line to a hydrated SAP row (grouped items share ``PRItem``)."""
    svc = normalize_service_performer_code(_norm(block.get("service")))
    exp_ccs = _yser_line_cc_set(block)
    item_no = yser_pr_item_number_for_block(block, line_index=line_index)
    if item_no and svc:
        row = read_by_item_service.get((item_no, svc))
        if row is not None and _yser_line_cc_set(row) == exp_ccs:
            return row
    for row in read_lines:
        if not isinstance(row, dict):
            continue
        if normalize_service_performer_code(_norm(row.get("service"))) != svc:
            continue
        if _yser_line_cc_set(row) == exp_ccs:
            return row
    if line_index < len(read_lines) and isinstance(read_lines[line_index], dict):
        return read_lines[line_index]
    return None


def verify_yser_read_matches_form(
    read_body: dict[str, Any],
    *,
    form: dict[str, Any],
    document_type: str = "YSER",
) -> list[str]:
    """Compare Z GET hydrate to submitted form (services + CC rows)."""
    read_form = form_from_z_pr_read(
        read_body, document_type=document_type, seed_form=form
    )
    mismatches: list[str] = []
    expected_blocks = yser_effective_line_blocks(form)
    read_lines = read_form.get("lines") if isinstance(read_form.get("lines"), list) else []
    if len(read_lines) != len(expected_blocks):
        mismatches.append(
            f"service lines: SAP {len(read_lines)} != form {len(expected_blocks)} "
            "(SAP may have a deleted item stub with active services — hydrate from SAP "
            "or remove orphan lines from the form)"
        )

    read_by_item_service: dict[tuple[str, str], dict[str, Any]] = {}
    for row in read_lines:
        if not isinstance(row, dict):
            continue
        svc = normalize_service_performer_code(_norm(row.get("service")))
        item_no = normalize_pr_item_number(_norm(row.get("purchase_requisition_item")))
        if item_no and svc:
            read_by_item_service[(item_no, svc)] = row

    for idx, block in enumerate(expected_blocks):
        sap_row = _match_yser_read_row_for_block(
            block, read_lines, read_by_item_service, line_index=idx
        )
        svc = normalize_service_performer_code(_norm(block.get("service")))
        item_no = yser_pr_item_number_for_block(block, line_index=idx)
        if not sap_row:
            mismatches.append(f"service {svc or item_no}: missing on SAP read")
            continue
        exp_ccs = _yser_line_cc_set(block)
        sap_ccs = _yser_line_cc_set(sap_row)
        if exp_ccs != sap_ccs:
            mismatches.append(
                f"service {svc or item_no}: CCs SAP {sorted(sap_ccs)!r} != form {sorted(exp_ccs)!r}"
            )
    return mismatches


def z_pr_service_root_url(base_url: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root

    return f"{odata_base_root(base_url)}{Z_PR_SERVICE_PATH}"


def z_pr_collection_url(base_url: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root

    return f"{odata_base_root(base_url)}{Z_PR_HEADER_COLLECTION}"


def z_pr_entity_url(base_url: str, pr_number: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root, odata_entity_key

    key = odata_entity_key(pr_number)
    if not key:
        raise ValueError("PR number is required for Z PR entity URL")
    return f"{odata_base_root(base_url)}{Z_PR_HEADER_COLLECTION}('{key}')"


def z_pr_read_url(base_url: str, pr_number: str) -> str:
    """Header GET; follow ``__deferred`` links for items/services."""
    return z_pr_entity_url(base_url, pr_number)


def z_pr_item_entity_url(base_url: str, *, pr_number: str, pr_item: str) -> str:
    from app.procurement.sap_odata_utils import odata_base_root, odata_entity_key

    pr_key = odata_entity_key(pr_number)
    item_key = odata_entity_key(pr_item)
    if not pr_key or not item_key:
        raise ValueError("PR number and PR item are required")
    return (
        f"{odata_base_root(base_url)}{Z_PR_ITEM_COLLECTION}"
        f"(PRNumber='{pr_key}',PRItem='{item_key}')"
    )


def format_z_pr_item_number(item_number: int) -> str:
    """SAP Z API uses 5-digit item numbers (e.g. ``00010``)."""
    return str(item_number).zfill(5)


def _z_pr_item_from_blocks(blocks: list[dict[str, Any]]) -> str:
    """Deprecated — use :func:`_z_pr_item_key_for_block` per UI line."""
    if blocks:
        return _z_pr_item_key_for_block(blocks[0], line_index=0)
    return format_z_pr_item_number(10)


def _z_delivery_date(raw: str) -> str:
    """Z API expects ``YYYYMMDD`` (Service PR doc)."""
    s = _norm(raw)
    if not s:
        return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{s[:4]}{s[5:7]}{s[8:10]}"
    return s


def _z_delivery_date_to_form(raw: Any) -> str:
    s = _norm(raw) if isinstance(raw, str) else odata_text(raw)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return odata_date_to_form(raw)


def _fixed_vendor(header: dict[str, Any]) -> str:
    v = _norm(header.get("vendor"))
    if "|" in v:
        return v.split("|", 1)[0].strip()
    return v


def _valuation_price(block: dict[str, Any]) -> str:
    for key in ("valuation_price", "gross_price", "unit_price"):
        v = _norm(block.get(key))
        if v:
            return v
    return ""


def _gross_price_for_z(block: dict[str, Any], *, service_qty: float) -> str:
    """Service-row ``GrossPrice`` — SAP unit price (× quantity for line total)."""
    for key in ("unit_price", "gross_price"):
        raw = _norm(block.get(key))
        if raw:
            try:
                return str(round(float(raw.replace(",", ".")), 2))
            except ValueError:
                return raw
    val = _norm(block.get("valuation_price"))
    if val and service_qty > 0:
        try:
            return str(round(float(val.replace(",", ".")) / service_qty, 2))
        except ValueError:
            return val
    return ""


def _item_total_quantity(allocs: list[dict[str, Any]]) -> float:
    total = 0.0
    for alloc in allocs:
        if not isinstance(alloc, dict):
            continue
        q = _norm(alloc.get("qty"))
        if not q:
            continue
        try:
            total += float(q)
        except ValueError:
            pass
    return total if total > 0 else 1.0


def _format_z_qty(qty: float | str) -> str:
    try:
        v = float(str(qty).strip())
    except ValueError:
        v = 1.0
    return f"{v:.3f}"


def _service_uom(block: dict[str, Any]) -> str:
    u = _norm(block.get("order_unit"))
    return u or "EA"


def normalize_service_performer_code(raw: str) -> str:
    """Map catalogue short codes to SAP internal service number (18-digit ``00000001…``)."""
    s = _norm(raw)
    if not s or not s.isdigit():
        return s
    if len(s) >= 18:
        return s
    # QAS pattern: ``00000001`` + 10-digit service (e.g. ``10000000006`` → ``000000010000000006``).
    tail = s[-10:].zfill(10)
    return f"00000001{tail}"


def _format_price_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{round(value, 2):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _allocation_qty_total(allocs: list[dict[str, Any]]) -> float:
    return _item_total_quantity([a for a in allocs if isinstance(a, dict)])


def _grand_total_alloc_qty(blocks: list[dict[str, Any]]) -> float:
    """Sum of all allocation qty values across every UI line (one Z PR item)."""
    total = 0.0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        allocs = block.get("allocations")
        if not isinstance(allocs, list):
            continue
        total += _item_total_quantity([a for a in allocs if isinstance(a, dict)])
    return total if total > 0 else 1.0


def _z_distr_share(alloc_qty: float, *, grand_total: float) -> float:
    if grand_total <= 0:
        return alloc_qty if alloc_qty > 0 else 1.0
    return alloc_qty / grand_total


def _z_ui_qty_from_distr(
    distr: float, *, distr_sum: float, service_qty: float
) -> str:
    """Invert SAP split shares back to UI allocation qty."""
    if distr_sum <= 0 or service_qty <= 0:
        return _qty_for_ui_display(distr if distr > 0 else 1.0)
    return _qty_for_ui_display(distr / distr_sum * service_qty)


def _qty_for_ui_display(qty: float) -> str:
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    text = f"{qty:.3f}".rstrip("0").rstrip(".")
    return text or "1"


def _z_block_line_total(block: dict[str, Any], *, item_qty: float) -> float | None:
    if not isinstance(block, dict):
        return None
    val_raw = _norm(block.get("valuation_price"))
    if val_raw:
        try:
            return float(val_raw.replace(",", "."))
        except ValueError:
            return None
    qty = item_qty if item_qty > 0 else 1.0
    up_raw = _norm(block.get("unit_price"))
    if up_raw:
        try:
            return float(up_raw.replace(",", ".")) * qty
        except ValueError:
            return None
    gross_raw = _norm(block.get("gross_price"))
    if gross_raw:
        try:
            return float(gross_raw.replace(",", "."))
        except ValueError:
            return None
    return None


def _z_item_valuation_price_line_total(
    blocks: list[dict[str, Any]], *, item_qty: float
) -> str:
    """Item ``ValuationPrice`` on update — sum of UI line totals across services."""
    totals: list[float] = []
    for block in blocks:
        line_total = _z_block_line_total(block, item_qty=item_qty)
        if line_total is not None:
            totals.append(line_total)
    if totals:
        return _format_price_number(sum(totals))
    return ""


def _z_sap_line_total_from_read(
    *,
    item_valuation: str,
    service_gross: str,
    service_qty: str = "",
    multi_service_item: bool = False,
) -> str:
    """Hydrate UI amounts: item total for single-service rows; unit×qty per service when grouped."""
    if not multi_service_item:
        val = _norm(item_valuation)
        if val:
            try:
                if float(val.replace(",", ".")) > 0:
                    return val
            except ValueError:
                return val
    gross = _norm(service_gross)
    sq_raw = _norm(service_qty)
    if gross:
        try:
            unit = float(gross.replace(",", "."))
            sq = float(sq_raw.replace(",", ".")) if sq_raw else 1.0
            if sq <= 0:
                sq = 1.0
            return _format_price_number(unit * sq)
        except ValueError:
            pass
    val = _norm(item_valuation)
    return gross or val


def _yser_form_prices_from_z_line_total(
    line_total_raw: str, allocs: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """Z ``GrossPrice`` / item ``ValuationPrice`` are line totals; UI keeps unit + valuation."""
    line_total_s = _norm(line_total_raw)
    if not line_total_s:
        return "", "", ""
    try:
        line_total = float(line_total_s.replace(",", "."))
    except ValueError:
        return line_total_s, line_total_s, line_total_s
    qty = _allocation_qty_total(allocs)
    if qty <= 0:
        qty = 1.0
    unit = line_total / qty
    val = _format_price_number(line_total)
    return _format_price_number(unit), val, val


def _z_alloc_qty_to_form(distr_qty: str, *, service_qty: str = "") -> str:
    """Map Z ``DistrQuantity`` to UI qty (new PRs often return millis: 1000 → 1)."""
    raw = _norm(distr_qty)
    if not raw:
        return "1"
    try:
        d = float(raw.replace(",", "."))
    except ValueError:
        return raw
    sq: float | None = None
    if service_qty:
        try:
            sq = float(_norm(service_qty).replace(",", "."))
        except ValueError:
            sq = None
    if sq is not None and sq >= 100.0:
        d = d / 1000.0
    if abs(d - round(d)) < 1e-9:
        return str(int(round(d)))
    text = f"{d:.3f}".rstrip("0").rstrip(".")
    return text or "1"


def _inline_service_rows(
    *,
    pr_key: str,
    pr_item: str,
    service_no: str,
    short_text: str,
    block: dict[str, Any],
    allocs: list[dict[str, Any]],
    next_acct_seq: int,
    grand_total_qty: float = 1.0,
    for_update: bool = False,
    use_absolute_distr: bool = False,
    ext_ref: str = "",
    service_is_deleted: bool = False,
    acct_delete_cc: str | None = None,
    acct_delete_keys: frozenset[tuple[str, str]] | None = None,
    omit_service_short_text: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Build flat ``to_Services`` rows (field ``Service``, not ``ServiceNumber``).

    One ``PRAcctAssgmtNumber`` per CC row (GET-aligned). Reuse hydrated serials when present;
    otherwise assign ``01``, ``02``, … within the service (``next_acct_seq`` starts at ``1`` per service).
    """
    valid = [a for a in allocs if isinstance(a, dict) and _norm(a.get("cost_center"))]
    total = _item_total_quantity(valid)
    uom = _service_uom(block)
    qty_str = _format_z_qty(total)
    if for_update:
        gross_price = _z_item_valuation_price_line_total(
            [block], item_qty=total if total > 0 else 1.0
        )
    else:
        gross_price = _gross_price_for_z(block, service_qty=total)

    seq = next_acct_seq
    multi_cc = len(valid) > 1
    kept_count = sum(
        1
        for a in valid
        if not (acct_delete_keys and (cc := _norm(a.get("cost_center")))
        and (acct := normalize_pr_acct_assgmt_number(_norm(a.get("pr_acct_assgmt_number"))))
        and (cc, acct) in acct_delete_keys)
    )

    def _service_row(pr_acct_no: str, **fields: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "PRNumber": pr_key if for_update else "",
            "PRItem": pr_item,
            "Service": service_no,
            "Quantity": qty_str,
            "UOM": uom,
            "PRAcctAssgmtNumber": pr_acct_no,
            **fields,
        }
        if not omit_service_short_text and short_text:
            row["ShortText"] = short_text
        ref = ext_ref or _yser_pr_service_external_ref(block)
        if ref:
            row["PurDocItemExternalReference"] = ref[:YSER_PR_SERVICE_EXT_REF_MAX_LEN]
        if service_is_deleted:
            row["IsDeleted"] = "X"
        if gross_price:
            row["GrossPrice"] = gross_price
        return row

    if not valid:
        acct_no, seq = _alloc_pr_acct_assgmt_number({}, fallback_seq=seq)
        return (
            [
                _service_row(
                    acct_no,
                    CostCenter="",
                    DistrPercentage="100.0",
                    DistrQuantity=qty_str,
                )
            ],
            seq,
        )

    rows: list[dict[str, Any]] = []
    for alloc in valid:
        cc = _norm(alloc.get("cost_center"))
        acct_no, seq = _alloc_pr_acct_assgmt_number(alloc, fallback_seq=seq)
        acct_key = normalize_pr_acct_assgmt_number(acct_no)
        is_acct_delete = bool(
            acct_delete_keys and cc and acct_key and (cc, acct_key) in acct_delete_keys
        )
        if acct_delete_cc and cc == acct_delete_cc:
            is_acct_delete = True
        if multi_cc or acct_delete_keys:
            if is_acct_delete:
                row_distrib = "1"
            elif kept_count == 1:
                row_distrib = ""
            else:
                row_distrib = "1"
        else:
            row_distrib = "" if for_update else "1"
        qty_raw = _norm(alloc.get("qty")) or "1"
        try:
            qf = float(qty_raw)
        except ValueError:
            qf = 1.0
        if use_absolute_distr:
            distr_qty = qf
            pct = (qf / total * 100.0) if total > 0 else 100.0
        else:
            share = _z_distr_share(qf, grand_total=grand_total_qty)
            distr_qty = share
            pct = share * 100.0
        rows.append(
            _service_row(
                acct_no,
                CostCenter=cc,
                DistrPercentage=f"{pct:.1f}",
                DistrQuantity=_format_z_qty(distr_qty),
                **({"Distrib": row_distrib} if row_distrib else {}),
                **({"IsAcctAssgmtDeleted": "X"} if is_acct_delete else {}),
            )
        )
    return rows, seq


def build_z_yser_pr_payload(
    *,
    form: dict[str, Any],
    document_type: str,
    pr_number: str = "",
    ticket_id: str | None = None,
    for_update: bool = False,
    sap_item_count: int | None = None,
) -> dict[str, Any]:
    """Build POST/PATCH body for ``PRHeaderSet`` (YSER) per Service PR API doc."""
    dt = (document_type or "").upper()
    if dt != "YSER":
        raise ValueError(f"Z PR payload only supports YSER, got {dt!r}")

    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    lines_in = form.get("lines") if isinstance(form.get("lines"), list) else []
    user_header_note = _norm(header.get("header_note"))
    header_note = build_z_header_note(user_header_note, ticket_id=ticket_id)
    pur_org = _norm(header.get("purchasing_org"))
    pur_group = _norm(header.get("purchasing_group"))
    plant = _norm(header.get("plant"))
    sloc = storage_location_for_sap(
        _norm(header.get("storage_location")), plant=plant
    )
    fixed_vendor = _fixed_vendor(header)
    pr_key = _norm(pr_number)

    if not for_update:
        _ensure_yser_pr_service_refs(form, ticket_id=ticket_id)

    blocks = yser_effective_line_blocks(form)
    if not blocks:
        raise ValueError("No PR line items to send to SAP (empty blocks or allocations).")

    grouped_layout = not for_update
    if for_update and sap_item_count is not None:
        grouped_layout = yser_sap_layout_is_grouped(
            sap_item_count=sap_item_count,
            form_line_count=len(blocks),
        )

    item_header_note = user_header_note[:SAP_TEXT_FIELD_MAX_LEN] if user_header_note else ""
    items: list[dict[str, Any]] = []

    def _append_item_row(
        *,
        pr_item: str,
        mat_grp: str,
        short_text: str,
        item_delivery: str,
        first_cc: str,
        group_blocks: list[dict[str, Any]],
        service_rows: list[dict[str, Any]],
        marker_on_item: bool,
    ) -> None:
        row: dict[str, Any] = {
            "PRItem": pr_item,
            "PurOrg": pur_org,
            "PurGroup": pur_group,
            "ActAssignmentCat": Z_ACCOUNT_ASSIGNMENT_CAT,
            "ItemCat": Z_ITEM_CATEGORY_SERVICE,
            "Material": "",
            "ShortText": short_text or user_header_note[:SAP_TEXT_FIELD_MAX_LEN],
            "HeaderNote": item_header_note,
            "Plant": plant,
            "MaterialGroup": mat_grp,
            "DeliveryDate": item_delivery,
            "SLoc": sloc,
            "ServiceNo": "",
            "Quantity": Z_ITEM_HEADER_QUANTITY,
            "CostCenter": first_cc,
            "Asset": "",
            "FixedVendor": fixed_vendor,
            "ValuationPrice": "",
            "PRNumber": pr_key if for_update else "",
            "to_Services": service_rows,
        }
        if for_update:
            row.pop("ValuationPrice", None)
            row["ValuationPrice"] = _z_item_valuation_price_line_total(
                group_blocks, item_qty=1.0
            )
        if marker_on_item and ticket_id and not for_update:
            marker = sap_ticket_ext_system_marker(ticket_id)
            if marker:
                row["Extsourcesystem"] = marker
        items.append(row)

    if grouped_layout:
        for group_idx, group in enumerate(yser_group_blocks_by_service_group(blocks, header)):
            first_idx, first_block = group[0]
            mat_grp = line_catalog_group(first_block, header, dt)
            pr_item = _z_pr_item_key_for_block(first_block, line_index=first_idx)
            item_delivery = _z_delivery_date(_norm(first_block.get("delivery_date")))
            short_text = _norm(first_block.get("short_text")) or mat_grp
            first_cc = ""
            all_service_rows: list[dict[str, Any]] = []
            group_blocks = [block for _, block in group]
            for _line_idx, block in group:
                allocs = block.get("allocations")
                if not isinstance(allocs, list) or not allocs:
                    allocs = [{}]
                valid_allocs = [a for a in allocs if isinstance(a, dict)]
                service_no = normalize_service_performer_code(_norm(block.get("service")))
                line_short = _norm(block.get("short_text")) or short_text
                if not service_no:
                    continue
                for alloc in valid_allocs:
                    cc = _norm(alloc.get("cost_center"))
                    if cc and not first_cc:
                        first_cc = cc
                service_rows, _ = _inline_service_rows(
                    pr_key=pr_key,
                    pr_item=pr_item,
                    service_no=service_no,
                    short_text=line_short,
                    block=block,
                    allocs=valid_allocs,
                    next_acct_seq=1,
                    grand_total_qty=_grand_total_alloc_qty([block]),
                    for_update=for_update,
                    use_absolute_distr=True,
                    ext_ref=_yser_pr_service_external_ref(block),
                )
                all_service_rows.extend(service_rows)
            if not all_service_rows:
                continue
            _append_item_row(
                pr_item=pr_item,
                mat_grp=mat_grp,
                short_text=short_text,
                item_delivery=item_delivery,
                first_cc=first_cc,
                group_blocks=group_blocks,
                service_rows=all_service_rows,
                marker_on_item=group_idx == 0,
            )
    else:
        for line_idx, block in enumerate(blocks):
            allocs = block.get("allocations")
            if not isinstance(allocs, list) or not allocs:
                allocs = [{}]
            valid_allocs = [a for a in allocs if isinstance(a, dict)]
            service_no = normalize_service_performer_code(_norm(block.get("service")))
            short_text = _norm(block.get("short_text")) or user_header_note[:SAP_TEXT_FIELD_MAX_LEN]
            item_delivery = _z_delivery_date(_norm(block.get("delivery_date")))
            first_cc = ""
            for alloc in valid_allocs:
                cc = _norm(alloc.get("cost_center"))
                if cc and not first_cc:
                    first_cc = cc
            if not service_no:
                continue
            mat_grp = line_catalog_group(block, header, dt)
            pr_item = _z_pr_item_key_for_block(block, line_index=line_idx)
            service_rows, _ = _inline_service_rows(
                pr_key=pr_key,
                pr_item=pr_item,
                service_no=service_no,
                short_text=short_text,
                block=block,
                allocs=valid_allocs,
                next_acct_seq=1,
                grand_total_qty=_grand_total_alloc_qty([block]),
                for_update=for_update,
                use_absolute_distr=True,
                ext_ref=_yser_pr_service_external_ref(block),
            )
            _append_item_row(
                pr_item=pr_item,
                mat_grp=mat_grp,
                short_text=short_text,
                item_delivery=item_delivery,
                first_cc=first_cc,
                group_blocks=[block],
                service_rows=service_rows,
                marker_on_item=line_idx == 0,
            )

    if not items:
        raise ValueError("No PR line items to send to SAP (empty blocks or allocations).")

    out: dict[str, Any] = {
        "PRNumber": pr_key,
        "DocumentType": "YSER",
        "to_Items": items,
    }
    if user_header_note:
        out["PurReqnDescription"] = user_header_note[:SAP_TEXT_FIELD_MAX_LEN]
    return out


def finalize_z_yser_update_post_body(inner: dict[str, Any]) -> dict[str, Any]:
    """Flat ``POST`` collection body for Z update (no ``d`` / ``results`` wrapper)."""
    pr_number = _norm(inner.get("PRNumber"))
    if not pr_number:
        raise ValueError("PR number is required for Z YSER update")

    items_out: list[dict[str, Any]] = []
    for item in inner.get("to_Items") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["PRNumber"] = pr_number
        services = row.get("to_Services")
        if isinstance(services, list):
            row["to_Services"] = [
                {**svc, "PRNumber": pr_number} if isinstance(svc, dict) else svc
                for svc in services
            ]
        items_out.append(row)

    out: dict[str, Any] = {
        "PRNumber": pr_number,
        "DocumentType": _norm(inner.get("DocumentType")) or "YSER",
        "to_Items": items_out,
    }
    header_note = _norm(inner.get("PurReqnDescription"))
    if "PurReqnDescription" in inner:
        out["PurReqnDescription"] = header_note[:SAP_TEXT_FIELD_MAX_LEN]
    elif header_note:
        out["PurReqnDescription"] = header_note[:SAP_TEXT_FIELD_MAX_LEN]
    return out


def build_z_yser_item_delete_post_body(
    *,
    pr_number: str,
    item_numbers: list[str],
) -> dict[str, Any]:
    """Z ``POST PRHeaderSet`` — mark item(s) deleted via ``to_Items[].IsDeleted=X``.

    QAS accepts a minimal item stub (no ``to_Services``). Verified on collection POST.
    """
    pr_key = _norm(pr_number)
    if not pr_key:
        raise ValueError("PR number is required for Z YSER delete")
    items_out: list[dict[str, Any]] = []
    for raw in item_numbers:
        digits = normalize_pr_item_number(raw)
        if not digits:
            continue
        items_out.append(
            {
                "PRItem": format_z_pr_item_number(int(digits)),
                "PRNumber": pr_key,
                "IsDeleted": "X",
            }
        )
    if not items_out:
        raise ValueError("At least one PR item number is required for Z YSER delete")
    return finalize_z_yser_update_post_body(
        {
            "PRNumber": pr_key,
            "DocumentType": "YSER",
            "to_Items": items_out,
        }
    )


def parse_z_pr_number_from_response(body: Any) -> str | None:
    return parse_pr_number_from_response(body)


def _service_code_from_row(sp: dict[str, Any]) -> str:
    return odata_text(sp.get("Service")) or odata_text(sp.get("ServiceNumber"))


def _yser_sap_row_partial_distr_qty(sp: dict[str, Any]) -> str | None:
    """UI qty for a lone SAP service row with partial ``DistrPercentage`` (legacy duplicate rows).

    Fires only when SAP marks the row as a partial share (pct < 100 and distr != service qty).
    CC-split clusters use the multi-row ``distr_sum`` path instead.
    """
    distr_raw = odata_text(sp.get("DistrQuantity"))
    svc_qty_raw = odata_text(sp.get("Quantity"))
    pct_raw = odata_text(sp.get("DistrPercentage"))
    if not distr_raw or not svc_qty_raw or not pct_raw:
        return None
    try:
        distr = float(distr_raw.replace(",", "."))
        svc_qty = float(svc_qty_raw.replace(",", "."))
        pct = float(pct_raw.replace(",", "."))
    except ValueError:
        return None
    if pct >= 99.9 or svc_qty <= 0 or distr <= 0:
        return None
    if abs(distr - svc_qty) <= 1e-6:
        return None
    return _qty_for_ui_display(distr)


def _read_bucket_from_service_rows(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge one UI service line from clustered ``to_Services`` rows (CC splits)."""
    bucket: dict[str, Any] = {
        "service": "",
        "pr_acct_assgmt_number": "",
        "short_text": "",
        "order_unit": "EA",
        "gross_price": "",
        "service_quantity": "",
        "ext_ref": "",
        "allocations": [],
    }
    for svc_entry in cluster:
        sp = odata_entity_properties(svc_entry)
        ref = odata_text(sp.get("PurDocItemExternalReference"))
        if ref and not bucket["ext_ref"]:
            bucket["ext_ref"] = ref
        code = _service_code_from_row(sp)
        if code and not bucket["service"]:
            bucket["service"] = code
        acct_no = odata_text(sp.get("PRAcctAssgmtNumber"))
        gp = odata_text(sp.get("GrossPrice"))
        if gp:
            bucket["gross_price"] = gp
        st = strip_pr_item_text_marker(odata_text(sp.get("ShortText")))
        if st:
            bucket["short_text"] = st
        uom = odata_text(sp.get("UOM"))
        if uom:
            bucket["order_unit"] = uom
        svc_row_qty = odata_text(sp.get("Quantity"))
        if svc_row_qty and not bucket.get("service_quantity"):
            bucket["service_quantity"] = svc_row_qty
        acct_node = sp.get("to_AcctAssgmt")
        if acct_node is None and isinstance(svc_entry.get("to_AcctAssgmt"), dict):
            acct_node = svc_entry.get("to_AcctAssgmt")
        nested_accts = odata_results_list(acct_node)
        if nested_accts:
            for acct_entry in nested_accts:
                ap = odata_entity_properties(acct_entry)
                cc2 = odata_text(ap.get("CostCenter"))
                if not cc2:
                    continue
                qty2 = odata_text(ap.get("Quantity")) or svc_row_qty or "1"
                alloc_row: dict[str, Any] = {"cost_center": cc2, "qty": qty2}
                acct = normalize_pr_acct_assgmt_number(
                    odata_text(ap.get("PRAcctAssgmtNumber"))
                )
                if acct:
                    alloc_row["pr_acct_assgmt_number"] = acct
                    if not bucket["pr_acct_assgmt_number"]:
                        bucket["pr_acct_assgmt_number"] = acct
                bucket["allocations"].append(alloc_row)
            continue
        cc = odata_text(sp.get("CostCenter"))
        if cc:
            distr_raw = odata_text(sp.get("DistrQuantity")) or svc_row_qty or "1"
            try:
                distr_f = float(distr_raw.replace(",", "."))
            except ValueError:
                distr_f = 1.0
            alloc_row = {"cost_center": cc, "distr": distr_f}
            acct = normalize_pr_acct_assgmt_number(acct_no)
            if acct:
                alloc_row["pr_acct_assgmt_number"] = acct
                if not bucket["pr_acct_assgmt_number"]:
                    bucket["pr_acct_assgmt_number"] = acct
            bucket["allocations"].append(alloc_row)
    if len(cluster) == 1:
        partial_qty = _yser_sap_row_partial_distr_qty(odata_entity_properties(cluster[0]))
        if partial_qty:
            bucket["partial_distr_line_qty"] = partial_qty
    return bucket


def _allocs_from_read_bucket(
    bucket: dict[str, Any], *, service_index: int
) -> list[dict[str, Any]]:
    raw_allocs = bucket.get("allocations") or []

    def _with_acct(base: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
        row = dict(base)
        acct = normalize_pr_acct_assgmt_number(_norm(src.get("pr_acct_assgmt_number")))
        if acct:
            row["pr_acct_assgmt_number"] = acct
        return row

    if raw_allocs and "distr" in raw_allocs[0]:
        partial_qty = _norm(bucket.get("partial_distr_line_qty"))
        if partial_qty and len(raw_allocs) == 1:
            return [
                _with_acct(
                    {
                        "cost_center": raw_allocs[0].get("cost_center", ""),
                        "qty": partial_qty,
                    },
                    raw_allocs[0],
                )
            ]
        try:
            service_qty = float(str(bucket.get("service_quantity") or "0").replace(",", "."))
        except ValueError:
            service_qty = 0.0
        distr_sum = sum(float(a.get("distr") or 0) for a in raw_allocs)
        if service_qty > 0 and distr_sum > 0:
            return [
                _with_acct(
                    {
                        "cost_center": a.get("cost_center", ""),
                        "qty": _z_ui_qty_from_distr(
                            float(a.get("distr") or 0),
                            distr_sum=distr_sum,
                            service_qty=service_qty,
                        ),
                    },
                    a,
                )
                for a in raw_allocs
            ]
        if len(raw_allocs) == 1:
            return [
                _with_acct(
                    {
                        "cost_center": raw_allocs[0].get("cost_center", ""),
                        "qty": _qty_for_ui_display(distr_sum if distr_sum > 0 else 1.0),
                    },
                    raw_allocs[0],
                )
            ]
        return [
            _with_acct(
                {
                    "cost_center": a.get("cost_center", ""),
                    "qty": _z_alloc_qty_to_form(
                        str(a.get("distr") or "1"),
                        service_qty=bucket.get("service_quantity") or "",
                    ),
                },
                a,
            )
            for a in raw_allocs
        ]
    return raw_allocs or [{"cost_center": "", "qty": "1"}]


def form_from_z_pr_read(
    body: dict[str, Any],
    *,
    document_type: str,
    seed_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map resolved Z ``GET PRHeaderSet`` (+ deferred items/services) → AgentOS form."""
    import copy

    seed_h: dict[str, Any] = {}
    if isinstance(seed_form, dict) and isinstance(seed_form.get("header"), dict):
        seed_h = copy.deepcopy(seed_form["header"])

    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return {"header": seed_h, "lines": []}

    header = {**seed_h}
    lines: list[dict[str, Any]] = []
    root_note = odata_text(root.get("PurReqnDescription"))
    if root_note and not header.get("header_note"):
        header["header_note"] = strip_agentos_bracket_markers_from_text(root_note)
    items_node = root.get("to_Items")
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        pr_item = odata_text(props.get("PRItem"))
        if not pr_item:
            continue
        if is_sap_deleted_flag(odata_text(props.get("IsDeleted"))):
            continue

        services_node = props.get("to_Services")
        if services_node is None and isinstance(entry.get("to_Services"), dict):
            services_node = entry.get("to_Services")

        clusters = cluster_yser_pr_service_entries(odata_results_list(services_node))
        item_price = odata_text(props.get("ValuationPrice"))
        delivery = _z_delivery_date_to_form(props.get("DeliveryDate"))
        sap_pr_item = pr_item.lstrip("0") or pr_item
        multi_service_item = len(clusters) > 1

        if not clusters:
            cc = odata_text(props.get("CostCenter"))
            allocations = (
                [{"cost_center": cc, "qty": odata_text(props.get("Quantity")) or "1"}]
                if cc
                else [{"cost_center": "", "qty": "1"}]
            )
            clusters = [[]]  # placeholder; handle below
            bucket = {
                "service": "",
                "short_text": strip_pr_item_text_marker(odata_text(props.get("ShortText"))),
                "order_unit": "EA",
                "gross_price": "",
                "allocations": allocations,
            }
            allocs = allocations
            line_total = _z_sap_line_total_from_read(
                item_valuation=item_price, service_gross=""
            )
            unit_p, val_p, gross_p = _yser_form_prices_from_z_line_total(line_total, allocs)
            line_row: dict[str, Any] = {
                "service": "",
                "short_text": bucket["short_text"],
                "delivery_date": delivery,
                "unit_price": unit_p,
                "valuation_price": val_p,
                "gross_price": gross_p,
                "order_unit": "EA",
                "item_category": odata_text(props.get("ItemCat")) or Z_ITEM_CATEGORY_SERVICE,
                "account_assignment_cat": odata_text(props.get("ActAssignmentCat"))
                or Z_ACCOUNT_ASSIGNMENT_CAT,
                "purchase_requisition_item": sap_pr_item,
                "sap_pr_item": sap_pr_item,
                "allocations": allocs,
            }
            mg = odata_text(props.get("MaterialGroup"))
            if mg:
                line_row["service_group"] = mg
            lines.append(line_row)
            continue

        for svc_idx, cluster in enumerate(clusters, start=1):
            if not cluster:
                continue
            bucket = _read_bucket_from_service_rows(cluster)
            allocs = _allocs_from_read_bucket(bucket, service_index=svc_idx)
            bucket["pr_acct_assgmt_number"] = resolve_block_pr_acct_assgmt_number(
                {"allocations": allocs, "pr_acct_assgmt_number": bucket.get("pr_acct_assgmt_number")},
                service_index=svc_idx,
            )
            partial_line_qty = _norm(bucket.get("partial_distr_line_qty"))
            read_service_qty = partial_line_qty or bucket.get("service_quantity") or ""
            line_total = _z_sap_line_total_from_read(
                item_valuation=item_price,
                service_gross=bucket.get("gross_price") or "",
                service_qty=read_service_qty,
                multi_service_item=multi_service_item or bool(partial_line_qty),
            )
            unit_p, val_p, gross_p = _yser_form_prices_from_z_line_total(line_total, allocs)
            line_row = {
                "service": bucket["service"],
                "short_text": strip_pr_item_text_marker(
                    bucket["short_text"] or odata_text(props.get("ShortText"))
                ),
                "delivery_date": delivery,
                "unit_price": unit_p,
                "valuation_price": val_p,
                "gross_price": gross_p,
                "order_unit": bucket["order_unit"],
                "item_category": odata_text(props.get("ItemCat")) or Z_ITEM_CATEGORY_SERVICE,
                "account_assignment_cat": odata_text(props.get("ActAssignmentCat"))
                or Z_ACCOUNT_ASSIGNMENT_CAT,
                "purchase_requisition_item": sap_pr_item,
                "sap_pr_item": sap_pr_item,
                "allocations": allocs,
            }
            mg = odata_text(props.get("MaterialGroup"))
            if mg:
                line_row["service_group"] = mg
            acct = _norm(bucket.get("pr_acct_assgmt_number"))
            if acct:
                line_row["pr_acct_assgmt_number"] = acct
            ref = _norm(bucket.get("ext_ref"))
            if ref:
                line_row["sap_pr_service_ref"] = ref
            lines.append(line_row)

        if not header.get("purchasing_org"):
            header["purchasing_org"] = odata_text(props.get("PurOrg"))
        if not header.get("purchasing_group"):
            header["purchasing_group"] = odata_text(props.get("PurGroup"))
        if not header.get("plant"):
            header["plant"] = odata_text(props.get("Plant"))
        if not header.get("storage_location"):
            header["storage_location"] = odata_text(props.get("SLoc"))
        note = odata_text(props.get("HeaderNote"))
        if note and not header.get("header_note"):
            header["header_note"] = note

    lines.sort(
        key=lambda r: (
            int(str(r.get("purchase_requisition_item") or "0") or "0"),
            str(r.get("pr_acct_assgmt_number") or "99"),
            str(r.get("service") or ""),
        )
    )
    sync_header_catalog_group(header, lines, "YSER")
    return {"header": header, "lines": lines}
