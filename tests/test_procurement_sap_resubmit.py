"""SAP resubmit: live PR/PO update keep the same document id on success."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.procurement import sap_po_client, sap_pr_client
from tests.conftest_procurement_sap import sample_yser_pr_form, sample_yunb_po_form


@patch("app.procurement.sap_pr_client._yser_skip_z_update_after_deletes", new_callable=AsyncMock)
@patch("app.procurement.sap_pr_z_client.apply_yser_z_line_deletes", new_callable=AsyncMock)
@patch("app.procurement.sap_pr_z_client.update_yser_pr", new_callable=AsyncMock)
def test_update_pr_returns_same_sap_id(
    mock_update: AsyncMock,
    mock_line_deletes: AsyncMock,
    mock_skip_z: AsyncMock,
) -> None:
    mock_line_deletes.return_value = None
    mock_skip_z.return_value = (False, None)
    mock_update.return_value = ("1040000099", None)
    sid, err = asyncio.run(
        sap_pr_client.update_pr(
            sap_id="1040000099",
            ticket_id="t1",
            form=sample_yser_pr_form(),
            document_type="YSER",
        )
    )
    assert err is None
    assert sid == "1040000099"
    mock_update.assert_awaited_once()


@patch("app.procurement.sap_po_client._sap_update_po", new_callable=AsyncMock)
def test_update_po_returns_same_sap_id(mock_update: AsyncMock) -> None:
    mock_update.return_value = ("4500000123", None)
    sid, err = asyncio.run(
        sap_po_client.update_po(
            sap_id="4500000123",
            ticket_id="t2",
            form=sample_yunb_po_form(),
            document_type="YUNB",
            parent_sap_id="1040000063",
        )
    )
    assert err is None
    assert sid == "4500000123"
    mock_update.assert_awaited_once()
