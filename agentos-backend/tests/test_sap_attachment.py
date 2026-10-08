"""SAP attachment slug/filter helpers and list filter."""

from unittest.mock import AsyncMock, patch
import pytest

from app.procurement.sap_attachment_client import (
    attachment_filter_document_id,
    attachment_upload_slug,
    list_attachments,
)


def test_attachment_slug_pr() -> None:
    assert attachment_filter_document_id(kind="PR", sap_id="1040000077") == "PR-1040000077"
    assert attachment_upload_slug(filename="note.pdf", kind="PR", sap_id="1040000077") == (
        "note.pdf;PR-1040000077"
    )


def test_attachment_slug_po() -> None:
    assert attachment_filter_document_id(kind="PO", sap_id="4500000123") == "PO-4500000123"
    assert "PO-4500000123" in attachment_upload_slug(filename="x.pdf", kind="PO", sap_id="4500000123")


@pytest.mark.asyncio
async def test_list_attachments_parses_results() -> None:
    payload = {
        "d": {
            "results": [
                {
                    "DocumentId": "FOL123",
                    "FileName": "note.pdf",
                    "MimeType": "application/pdf",
                    "CreatedOn": "/Date(1784808805000)/",
                }
            ]
        }
    }

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_Resp())
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.procurement.sap_attachment_client._credentials_or_error",
        return_value=(("https://sap.example", "u", "p"), None),
    ):
        with patch("app.procurement.sap_attachment_client._fetch_csrf", new=AsyncMock(return_value=("tok", None))):
            with patch("app.procurement.sap_attachment_client.httpx.AsyncClient", return_value=mock_client):
                rows, err = await list_attachments(
                    ticket_id="t1", kind="PR", sap_id="1010000953"
                )
    assert err is None
    assert len(rows) == 1
    assert rows[0]["sap_document_id"] == "FOL123"
    assert rows[0]["name"] == "note.pdf"
    call_kwargs = mock_client.get.call_args.kwargs
    assert call_kwargs["params"]["$filter"] == "DocumentId eq 'PR-1010000953'"
