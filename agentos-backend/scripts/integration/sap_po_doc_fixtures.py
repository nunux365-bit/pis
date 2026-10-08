"""SAP PO doc master data (PO All API-Structure) — integration / dev QAS only.

DB catalogue codes may not exist on QAS; use these when ``SAP_PO_USE_DOC_MASTER=1``.
"""

from __future__ import annotations

import copy
import os
from typing import Any

# Header (doc examples use 1LFS org / company)
SAP_PO_DOC_PUR_ORG = "1LFS"
SAP_PO_DOC_PUR_GROUP = "A0B"
SAP_PO_DOC_VENDOR = "1000000002"
SAP_PO_DOC_TAX_CODE = "XE"
SAP_PO_DOC_TAX_JURISDICTION = "ABCDABCDAB"
# Vendor 1000000002 on QAS (1LFS / 1MGH) — SAP stores this from vendor master.
SAP_PO_DOC_PAYMENT_TERMS = "YI10"
SAP_PO_DOC_CONTROLLING_AREA = "T1MG"
SAP_PO_DOC_GL_ACCOUNT = "29000001"

# YUNB line (doc create example)
SAP_PO_DOC_YUNB_PLANT = "L001"
SAP_PO_DOC_YUNB_SLOC = "1001"
SAP_PO_DOC_YUNB_MATERIAL = "4100000012"
# Material 4100000012 on plant L001 uses CAR on QAS (PC fails MEINS validation).
SAP_PO_DOC_YUNB_UNIT = "CAR"
SAP_PO_DOC_YUNB_QTY = "5"
SAP_PO_DOC_YUNB_PRICE = "100.00"
# Controlling area L001 — SAP stores L-prefixed CC on read (see PO 4080000014).
SAP_PO_DOC_YUNB_COST_CENTER = "LCO91001A0"

# YAST line (doc create example)
SAP_PO_DOC_YAST_PLANT = "L001"
SAP_PO_DOC_YAST_SLOC = "1003"
SAP_PO_DOC_YAST_MATERIAL = "4100000018"
SAP_PO_DOC_YAST_MATERIAL_GROUP = "M003-0043"
SAP_PO_DOC_YAST_INFO_RECORD = "5300000009"
SAP_PO_DOC_YAST_UNIT = "KG"
SAP_PO_DOC_YAST_QTY = "5"
SAP_PO_DOC_YAST_PRICE = "1500.00"
SAP_PO_DOC_YAST_ASSET = "7100001182"
SAP_PO_DOC_YAST_ASSET_2 = "7100001183"

# YSER line (Service PO doc — org 1MGH / plant H002)
SAP_PO_DOC_YSER_PUR_ORG = "1MGH"
SAP_PO_DOC_YSER_PLANT = "H002"
SAP_PO_DOC_YSER_SLOC = "1001"
SAP_PO_DOC_YSER_SERVICE_GROUP = "S001-0001"
SAP_PO_DOC_YSER_SERVICE = "10000000006"
SAP_PO_DOC_YSER_SERVICE_2 = "10000000007"
SAP_PO_DOC_YSER_UNIT = "EA"
SAP_PO_DOC_YSER_QTY = "2"
SAP_PO_DOC_YSER_PRICE = "100.00"
SAP_PO_DOC_YSER_COST_CENTER = "HCO91004A0"
SAP_PO_DOC_YSER_COST_CENTER_B = "HCO91001H0"
SAP_PO_DOC_YSER_COST_CENTER_C = "HCO91001A0"


def po_use_doc_master() -> bool:
    raw = os.environ.get("SAP_PO_USE_DOC_MASTER", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def apply_yast_pr_doc_env() -> dict[str, str]:
    """1LFS asset PR master (asset 7100001182 is not valid on 1MGH)."""
    applied = {
        "SAP_PUR_ORG": SAP_PO_DOC_PUR_ORG,
        "SAP_PUR_GROUP": SAP_PO_DOC_PUR_GROUP,
        "SAP_PLANT": SAP_PO_DOC_YAST_PLANT,
        "SAP_SLOC": SAP_PO_DOC_YAST_SLOC,
        "SAP_MATERIAL": SAP_PO_DOC_YAST_MATERIAL,
        "SAP_MATERIAL_2": SAP_PO_DOC_YUNB_MATERIAL,
        "SAP_MATERIAL_GROUP": SAP_PO_DOC_YAST_MATERIAL_GROUP,
        "SAP_ASSET": SAP_PO_DOC_YAST_ASSET,
        "SAP_ASSET_2": SAP_PO_DOC_YAST_ASSET_2,
        "SAP_PO_INFO_RECORD": SAP_PO_DOC_YAST_INFO_RECORD,
        "SAP_ORDER_UNIT": SAP_PO_DOC_YAST_UNIT,
        "SAP_UNIT_PRICE": SAP_PO_DOC_YAST_PRICE,
        "SAP_COST_CENTER": "",
        "SAP_COST_CENTER_2": "",
    }
    for key, value in applied.items():
        os.environ[key] = value
    return applied


def apply_po_doc_master_env() -> dict[str, str]:
    """Set env vars used by integration scripts (does not touch DB)."""
    applied = {
        "SAP_PO_USE_DOC_MASTER": "1",
        "SAP_PUR_ORG": SAP_PO_DOC_PUR_ORG,
        "SAP_PUR_GROUP": SAP_PO_DOC_PUR_GROUP,
        "SAP_VENDOR": SAP_PO_DOC_VENDOR,
        "SAP_PAYMENT_TERMS": SAP_PO_DOC_PAYMENT_TERMS,
        "SAP_TAX_CODE": SAP_PO_DOC_TAX_CODE,
        "SAP_MATERIAL": SAP_PO_DOC_YUNB_MATERIAL,
        "SAP_MATERIAL_2": SAP_PO_DOC_YAST_MATERIAL,
        "SAP_PLANT": SAP_PO_DOC_YUNB_PLANT,
        "SAP_SLOC": SAP_PO_DOC_YUNB_SLOC,
        "SAP_ORDER_UNIT": SAP_PO_DOC_YUNB_UNIT,
        "SAP_UNIT_PRICE": SAP_PO_DOC_YUNB_PRICE,
        "SAP_MATERIAL_GROUP": SAP_PO_DOC_YAST_MATERIAL_GROUP,
        "SAP_COST_CENTER": SAP_PO_DOC_YUNB_COST_CENTER,
        "SAP_ASSET": SAP_PO_DOC_YAST_ASSET,
        "SAP_ASSET_2": SAP_PO_DOC_YAST_ASSET_2,
        "SAP_PO_INFO_RECORD": SAP_PO_DOC_YAST_INFO_RECORD,
        "SAP_PO_GL_ACCOUNT": SAP_PO_DOC_GL_ACCOUNT,
        "SAP_PO_CONTROLLING_AREA": SAP_PO_DOC_CONTROLLING_AREA,
        "SAP_SERVICE": SAP_PO_DOC_YSER_SERVICE,
        "SAP_SERVICE_2": SAP_PO_DOC_YSER_SERVICE_2,
        "SAP_SERVICE_GROUP": SAP_PO_DOC_YSER_SERVICE_GROUP,
    }
    for key, value in applied.items():
        os.environ[key] = value
    return applied


def apply_doc_master_to_po_form(form: dict[str, Any], document_type: str) -> dict[str, Any]:
    """Overlay doc SAP master on a normalized UI form (keeps UI field shape)."""
    out = copy.deepcopy(form)
    header = out.setdefault("header", {})
    dt = (document_type or "").upper()
    header["purchasing_org"] = SAP_PO_DOC_PUR_ORG
    header["purchasing_group"] = SAP_PO_DOC_PUR_GROUP
    header["vendor"] = SAP_PO_DOC_VENDOR
    header["tax_code"] = SAP_PO_DOC_TAX_CODE
    header["payment_terms"] = SAP_PO_DOC_PAYMENT_TERMS
    lines = out.get("lines")
    if not isinstance(lines, list) or not lines:
        return out
    line = lines[0] if isinstance(lines[0], dict) else {}
    lines[0] = line
    if dt == "YUNB":
        header["plant"] = SAP_PO_DOC_YUNB_PLANT
        header["storage_location"] = SAP_PO_DOC_YUNB_SLOC
        header["material_group"] = "S002-0001"
        line["material"] = SAP_PO_DOC_YUNB_MATERIAL
        line["order_unit"] = SAP_PO_DOC_YUNB_UNIT
        line["unit_price"] = SAP_PO_DOC_YUNB_PRICE
        line["net_price"] = SAP_PO_DOC_YUNB_PRICE
        if line.get("allocations"):
            line["allocations"] = [
                {"cost_center": SAP_PO_DOC_YUNB_COST_CENTER, "qty": SAP_PO_DOC_YUNB_QTY}
            ]
    elif dt == "YAST":
        header["plant"] = SAP_PO_DOC_YAST_PLANT
        header["storage_location"] = SAP_PO_DOC_YAST_SLOC
        header["material_group"] = SAP_PO_DOC_YAST_MATERIAL_GROUP
        line["material"] = SAP_PO_DOC_YAST_MATERIAL
        line["order_unit"] = SAP_PO_DOC_YAST_UNIT
        line["unit_price"] = SAP_PO_DOC_YAST_PRICE
        line["net_price"] = SAP_PO_DOC_YAST_PRICE
        line["asset"] = SAP_PO_DOC_YAST_ASSET
        line["purchasing_info_record"] = SAP_PO_DOC_YAST_INFO_RECORD
        if line.get("allocations"):
            line["allocations"] = [
                {"asset": SAP_PO_DOC_YAST_ASSET, "qty": SAP_PO_DOC_YAST_QTY}
            ]
    elif dt == "YSER":
        header["plant"] = SAP_PO_DOC_YSER_PLANT
        header["storage_location"] = SAP_PO_DOC_YSER_SLOC
        header["service_group"] = SAP_PO_DOC_YSER_SERVICE_GROUP
        line["service"] = SAP_PO_DOC_YSER_SERVICE
        line["order_unit"] = SAP_PO_DOC_YSER_UNIT
        line["unit_price"] = SAP_PO_DOC_YSER_PRICE
        line["net_price"] = SAP_PO_DOC_YSER_PRICE
        if line.get("allocations"):
            line["allocations"] = [
                {"cost_center": SAP_PO_DOC_YSER_COST_CENTER, "qty": SAP_PO_DOC_YSER_QTY}
            ]
    return out
