"""Test that send_email accepts an attachments list."""
from unittest.mock import MagicMock, patch
import pytest


@patch("app.email_automation.gmail_sa._gmail_service")
def test_send_email_with_pdf_attachment(mock_svc):
    svc = MagicMock()
    mock_svc.return_value = svc
    svc.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "abc123"}

    from app.email_automation.gmail_sa import send_email
    result = send_email(
        to=["prospect@company.com"],
        cc=None,
        bcc=None,
        subject="Test",
        body_html="<p>Hello</p>",
        body_text="Hello",
        attachments=[(b"%PDF-1.4 test", "application/pdf", "deck.pdf")],
    )
    assert result == "abc123"
    svc.users.return_value.messages.return_value.send.assert_called_once()


@patch("app.email_automation.gmail_sa._gmail_service")
def test_send_email_without_attachment_unchanged(mock_svc):
    svc = MagicMock()
    mock_svc.return_value = svc
    svc.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "xyz"}
    from app.email_automation.gmail_sa import send_email
    result = send_email(
        to=["a@b.com"], cc=None, bcc=None,
        subject="S", body_html="<p>B</p>", body_text="B",
    )
    assert result == "xyz"
