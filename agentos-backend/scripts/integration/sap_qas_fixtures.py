"""Hardcoded SAP QAS master data for live integration scripts (not production UI).

Values from SAP probe on API_PURCHASEREQ_PROCESS_SRV (plant H001 / sloc 3021).
DB catalogue codes (3000… / 4000…) are not activated on this QAS client.
"""

from __future__ import annotations

import os

# YUNB / YAST consumable materials valid on QAS plant H001
SAP_QAS_MATERIAL_1 = "4200000027"
SAP_QAS_MATERIAL_2 = "4200000016"

SAP_QAS_PLANT = "H001"
SAP_QAS_SLOC = "3021"
SAP_QAS_PUR_ORG = "1MGH"
SAP_QAS_PUR_GROUP = "A0B"
SAP_QAS_MATERIAL_GROUP = "SD05-0001"
SAP_QAS_COST_CENTER_1 = "HCO91001H0"
SAP_QAS_COST_CENTER_2 = "HCO91001A0"
# QAS vendor 1000000002 — PO PaymentTerms from vendor master.
SAP_QAS_PAYMENT_TERMS = "YI10"


def apply_yunb_sap_qas_fixtures() -> dict[str, str]:
    """Force integration env for YUNB — overrides DB defaults and prior env."""
    applied = {
        "SAP_MATERIAL": SAP_QAS_MATERIAL_1,
        "SAP_MATERIAL_2": SAP_QAS_MATERIAL_2,
        "SAP_PLANT": SAP_QAS_PLANT,
        "SAP_SLOC": SAP_QAS_SLOC,
        "SAP_PUR_ORG": SAP_QAS_PUR_ORG,
        "SAP_PUR_GROUP": SAP_QAS_PUR_GROUP,
        "SAP_MATERIAL_GROUP": SAP_QAS_MATERIAL_GROUP,
        "SAP_COST_CENTER": SAP_QAS_COST_CENTER_1,
        "SAP_COST_CENTER_2": SAP_QAS_COST_CENTER_2,
        "SAP_PAYMENT_TERMS": SAP_QAS_PAYMENT_TERMS,
    }
    for key, value in applied.items():
        os.environ[key] = value
    return applied


def apply_yast_sap_qas_fixtures() -> dict[str, str]:
    """Same plant/materials as YUNB QAS probe (asset PR uses material lines on same API)."""
    return apply_yunb_sap_qas_fixtures()


# Z_PURCHASE_REQUISITION_SRV (YSER) — doc-valid service numbers on QAS
SAP_QAS_SERVICE_1 = "000000010000000006"
SAP_QAS_SERVICE_2 = "000000010000000007"
SAP_QAS_SERVICE_GROUP = "S001-0001"


def apply_yser_sap_qas_fixtures() -> dict[str, str]:
    """Force YSER + shared header master (overrides DB catalogue codes)."""
    applied = apply_yunb_sap_qas_fixtures()
    yser = {
        "SAP_SERVICE": SAP_QAS_SERVICE_1,
        "SAP_SERVICE_2": SAP_QAS_SERVICE_2,
        "SAP_SERVICE_GROUP": SAP_QAS_SERVICE_GROUP,
    }
    for key, value in yser.items():
        os.environ[key] = value
        applied[key] = value
    return applied
