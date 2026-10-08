"""SAP delivery date sanitization (epoch /Date(0)/ round-trip guard)."""

from __future__ import annotations

from app.procurement.sap_odata_utils import (
    SAP_EPOCH_FORM_DATE,
    form_delivery_date_is_valid,
    odata_date_to_form,
    sanitize_form_delivery_date,
)
from app.procurement.sap_pr_payload import _sap_date


def test_odata_date_to_form_rejects_epoch() -> None:
    assert odata_date_to_form("/Date(0)/") == ""
    assert odata_date_to_form("/Date(-1)/") == ""
    assert odata_date_to_form("/Date(1786752000000)/") == "2026-08-15"


def test_form_delivery_date_is_valid() -> None:
    assert not form_delivery_date_is_valid("")
    assert not form_delivery_date_is_valid(SAP_EPOCH_FORM_DATE)
    assert form_delivery_date_is_valid("2026-08-17")


def test_sanitize_form_delivery_date_uses_fallback() -> None:
    assert sanitize_form_delivery_date("/Date(0)/", fallback="2026-08-17") == "2026-08-17"
    assert sanitize_form_delivery_date(SAP_EPOCH_FORM_DATE, fallback="2026-08-17") == "2026-08-17"
    assert sanitize_form_delivery_date("2026-08-15", fallback="2026-08-17") == "2026-08-15"


def test_sap_date_rejects_epoch_form_date() -> None:
    assert _sap_date(SAP_EPOCH_FORM_DATE) is None
    assert _sap_date("2026-08-17") == "/Date(1786924800000)/"
