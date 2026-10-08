"""SAP OData read retry policy."""

from __future__ import annotations

import pytest

from app.procurement.sap_read_retry import should_retry_sap_odata_read_error


def test_retryable_deferred_404() -> None:
    err = "SAP HTTP 404: Resource not found for the segment 'to_ScheduleLine'."
    assert should_retry_sap_odata_read_error(err, status_code=404) is True


def test_retryable_mapping_error() -> None:
    err = (
        "SAP internal server error: Invalid or no mapping to system data types found "
        "for property 'ScheduleLineDeliveryDate'"
    )
    assert should_retry_sap_odata_read_error(err, status_code=500) is True
    assert should_retry_sap_odata_read_error(err) is True


def test_retryable_5xx_status_code() -> None:
    assert should_retry_sap_odata_read_error("SAP HTTP 503", status_code=503) is True


def test_non_retryable_unauthorized() -> None:
    err = "Unauthorized — check SAP credentials"
    assert should_retry_sap_odata_read_error(err, status_code=401) is False


def test_non_retryable_validation_400() -> None:
    err = "SAP HTTP 400: Bad Request — Vendor (Supplier) is required for SAP PO create"
    assert should_retry_sap_odata_read_error(err, status_code=400) is False


def test_retryable_connection_error() -> None:
    assert should_retry_sap_odata_read_error("SAP GET connection error: reset by peer") is True
