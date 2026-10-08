"""Map SAP OData entities → :class:`ReferenceRow`."""

from __future__ import annotations

from typing import Any

from app.procurement.reference_sync.rows import ReferenceRow
from app.procurement.sap_odata_utils import odata_entity_properties, odata_results_list, odata_text


def _label(text: str, *, fallback: str, max_len: int = 500) -> str:
    s = (text or "").strip() or fallback
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _service_short_code(asnum: str) -> str:
    s = asnum.strip()
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def map_matgroup_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("MATKL"))
        if not code:
            continue
        label = _label(odata_text(props.get("WGBEZ")), fallback=code)
        rows.append(
            ReferenceRow(
                domain="material_group",
                code=code,
                label=label,
                sort_order=i,
            )
        )
    return rows


def map_service_group_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    """Same catalogue as material group (``MatGroupSet``)."""
    return [
        ReferenceRow(
            domain="service_group",
            code=r.code,
            label=r.label,
            sort_order=r.sort_order,
        )
        for r in map_matgroup_rows(props_list)
    ]


def map_pur_group_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("EKGRP"))
        if not code:
            continue
        label = _label(odata_text(props.get("EKNAM")), fallback=code)
        rows.append(
            ReferenceRow(
                domain="purchasing_group",
                code=code,
                label=label,
                sort_order=i,
            )
        )
    return rows


def map_tax_code_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("Mwskz"))
        if not code:
            continue
        label = _label(odata_text(props.get("Text1")), fallback=code)
        rows.append(
            ReferenceRow(domain="tax_code", code=code, label=label, sort_order=i)
        )
    return rows


def map_plant_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("Plant"))
        if not code:
            continue
        label = _label(odata_text(props.get("PlantName")), fallback=code)
        rows.append(ReferenceRow(domain="plant", code=code, label=label, sort_order=i))
    return rows


def map_storage_location_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        plant = odata_text(props.get("Plant"))
        sloc = odata_text(props.get("StorageLocation"))
        if not plant or not sloc:
            continue
        code = f"{plant}|{sloc}"
        label = _label(
            odata_text(props.get("StorageLocationName")),
            fallback=sloc,
        )
        rows.append(
            ReferenceRow(
                domain="storage_location",
                code=code,
                label=label,
                sort_order=i,
                extra={"plant": plant, "storage_location": sloc},
            )
        )
    return rows


def map_asset_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    """``Z_PURCHASE_REQUISITION_SRV/AssetSet`` → ``domain=asset`` (scoped by company in ``extra``)."""
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        anln1 = odata_text(props.get("Anln1"))
        bukrs = odata_text(props.get("Bukrs"))
        if not anln1:
            continue
        label = _label(odata_text(props.get("Txt50")), fallback=anln1)
        anlkl = odata_text(props.get("Anlkl"))
        extra: dict[str, Any] = {
            "company_code": bukrs,
            "asset_number": anln1,
        }
        if anlkl:
            extra["asset_class"] = anlkl
        created = odata_text(props.get("CreatedOn"))
        changed = odata_text(props.get("ChangedOn"))
        if created:
            extra["created_on"] = created
        if changed:
            extra["changed_on"] = changed
        code = f"{bukrs}|{anln1}" if bukrs else anln1
        rows.append(
            ReferenceRow(
                domain="asset",
                code=code,
                label=label,
                sort_order=i,
                extra=extra,
            )
        )
    return rows


def map_service_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        asnum = odata_text(props.get("Asnum"))
        if not asnum:
            continue
        short = _service_short_code(asnum)
        label = _label(odata_text(props.get("ASKTX")), fallback=asnum)
        matkl = odata_text(props.get("Matkl"))
        extra: dict[str, Any] = {}
        if matkl:
            extra["service_group"] = matkl
            extra["Material Group"] = matkl
        if short != asnum:
            extra["short_code"] = short
        rows.append(
            ReferenceRow(
                domain="service",
                code=asnum,
                label=label,
                sort_order=i,
                extra=extra or None,
            )
        )
    return rows


def _product_description(entry: dict[str, Any]) -> str:
    direct = odata_text(entry.get("ProductDescription"))
    if direct:
        return direct
    raw = entry.get("_raw_entry")
    if not isinstance(raw, dict):
        raw = entry
    for desc in odata_results_list(raw.get("to_Description")):
        dp = odata_entity_properties(desc)
        lang = odata_text(dp.get("Language")).upper()
        text = odata_text(dp.get("ProductDescription"))
        if lang == "EN" and text:
            return text
    for desc in odata_results_list(raw.get("to_Description")):
        dp = odata_entity_properties(desc)
        text = odata_text(dp.get("ProductDescription"))
        if text:
            return text
    return ""


def map_material_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("Product"))
        if not code:
            continue
        desc = _product_description(props)
        label = _label(desc, fallback=code)
        pg = odata_text(props.get("ProductGroup"))
        bu = odata_text(props.get("BaseUnit"))
        pt = odata_text(props.get("ProductType"))
        extra: dict[str, Any] = {}
        if desc:
            extra["description"] = desc
        if pg:
            extra["material_group"] = pg
        if bu:
            extra["base_unit"] = bu
        if pt:
            extra["material_type"] = pt
        rows.append(
            ReferenceRow(
                domain="material",
                code=code,
                label=label,
                sort_order=i,
                extra=extra or None,
            )
        )
    return rows


def map_supplier_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    """``API_BUSINESS_PARTNER/A_Supplier`` → ``domain=vendor`` (supplier number only)."""
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        vendor = odata_text(props.get("Supplier"))
        if not vendor:
            continue
        name = odata_text(props.get("SupplierName"))
        label = _label(name, fallback=vendor)
        extra: dict[str, Any] = {"vendor": vendor, "name_1": name}
        created = odata_text(props.get("CreationDate"))
        if created:
            extra["creation_date"] = created
        rows.append(
            ReferenceRow(
                domain="vendor",
                code=vendor,
                label=label,
                sort_order=i,
                extra=extra,
            )
        )
    return rows


def map_vendor_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        vendor = odata_text(props.get("Supplier"))
        cocd = odata_text(props.get("CompanyCode"))
        if not vendor or not cocd:
            continue
        # Prefer supplier display name (A_Supplier); CompanyCodeName is the CoCd legal name.
        name = odata_text(props.get("SupplierName")) or odata_text(
            props.get("SupplierFullName")
        )
        cocd_name = odata_text(props.get("CompanyCodeName"))
        label = _label(name, fallback=f"{vendor}|{cocd}")
        extra: dict[str, Any] = {
            "vendor": vendor,
            "cocd": cocd,
            "company_code": cocd,
            "name_1": name or cocd_name,
        }
        if name and odata_text(props.get("SupplierFullName")):
            full = odata_text(props.get("SupplierFullName"))
            if full and full != name:
                extra["name_2"] = full
        if cocd_name:
            extra["company_code_name"] = cocd_name
        for sap_key, extra_key in (
            ("PaymentTerms", "payt"),
            ("AccountingClerk", "accounting_clerk"),
        ):
            v = odata_text(props.get(sap_key))
            if v:
                extra[extra_key] = v
        rows.append(
            ReferenceRow(
                domain="vendor",
                code=f"{vendor}|{cocd}",
                label=label,
                sort_order=i,
                extra=extra,
            )
        )
    return rows


def _cost_center_label(props: dict[str, Any]) -> str:
    raw = props.get("_raw_entry")
    if isinstance(raw, dict):
        for desc in odata_results_list(raw.get("to_Text")):
            dp = odata_entity_properties(desc)
            if odata_text(dp.get("Language")).upper() == "EN":
                name = odata_text(dp.get("CostCenterName")) or odata_text(
                    dp.get("CostCenterDescription")
                )
                if name:
                    return name
        for desc in odata_results_list(raw.get("to_Text")):
            dp = odata_entity_properties(desc)
            name = odata_text(dp.get("CostCenterName")) or odata_text(
                dp.get("CostCenterDescription")
            )
            if name:
                return name
    return (
        odata_text(props.get("CostCenterDescription"))
        or odata_text(props.get("CostCenterName"))
        or ""
    )


def map_cost_center_rows(props_list: list[dict[str, Any]]) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    for i, props in enumerate(props_list):
        code = odata_text(props.get("CostCenter"))
        if not code:
            continue
        name = _cost_center_label(props) or code
        label = _label(name, fallback=code)
        extra: dict[str, Any] = {}
        cc = odata_text(props.get("CompanyCode"))
        if cc:
            extra["Entity"] = cc
            extra["company_code"] = cc
        pc = odata_text(props.get("ProfitCenter"))
        if pc:
            extra["Profit Center"] = pc
            extra["profit_center"] = pc
        dept = odata_text(props.get("Department"))
        if dept:
            extra["Department"] = dept
            extra["department"] = dept
        fa = odata_text(props.get("FunctionalArea"))
        if fa:
            extra["Functional Area"] = fa
        ba = odata_text(props.get("BusinessArea"))
        if ba:
            extra["Business Area"] = ba
        ca = odata_text(props.get("ControllingArea"))
        if ca:
            extra["ControllingArea"] = ca
        rows.append(
            ReferenceRow(
                domain="cost_center",
                code=code,
                label=label,
                sort_order=i,
                extra=extra or None,
            )
        )
    return rows
