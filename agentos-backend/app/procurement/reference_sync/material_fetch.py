"""Fetch SAP product master with descriptions.

On our SAP gateway ``$expand=to_Description`` on ``A_Product`` list reads returns
``null``; descriptions live on ``A_ProductDescription`` and must be joined in code.
"""

from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.procurement.reference_sync.constants import (
    PRODUCT_COLLECTION_PATH,
    PRODUCT_DESCRIPTION_COLLECTION_PATH,
)
from app.procurement.reference_sync.odata_client import ODataClient
from app.procurement.sap_odata_utils import odata_text

PRODUCT_PATH = PRODUCT_COLLECTION_PATH
PRODUCT_DESCRIPTION_PATH = PRODUCT_DESCRIPTION_COLLECTION_PATH

_DEFAULT_PRODUCT_SELECT = "Product,ProductGroup,BaseUnit,ProductType"


async def fetch_product_description_index(
    odata: ODataClient,
    *,
    language: str | None = None,
) -> dict[str, str]:
    """``Product`` → ``ProductDescription`` for one SAP language (EN by default)."""
    lang = (language or settings.procurement_sap_language or "EN").strip().upper()
    params = {
        "$filter": f"Language eq '{lang}'",
        "$select": "Product,Language,ProductDescription",
    }
    props = await odata.fetch_properties(PRODUCT_DESCRIPTION_PATH, params=params)
    out: dict[str, str] = {}
    for row in props:
        code = odata_text(row.get("Product"))
        text = odata_text(row.get("ProductDescription"))
        if code and text:
            out[code] = text
    return out


def attach_product_descriptions(
    props_list: list[dict[str, Any]],
    descriptions: dict[str, str],
) -> None:
    """Set ``ProductDescription`` on each product row when a catalogue text exists."""
    if not descriptions:
        return
    for props in props_list:
        code = odata_text(props.get("Product"))
        if not code:
            continue
        text = descriptions.get(code)
        if text:
            props["ProductDescription"] = text


async def fetch_material_props_with_descriptions(
    odata: ODataClient,
    *,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """``A_Product`` rows enriched with EN descriptions from ``A_ProductDescription``."""
    product_params = dict(params or {})
    product_params.pop("$expand", None)
    product_params.setdefault("$select", _DEFAULT_PRODUCT_SELECT)
    props = await odata.fetch_properties(PRODUCT_PATH, params=product_params)
    descriptions = await fetch_product_description_index(odata)
    attach_product_descriptions(props, descriptions)
    return props
