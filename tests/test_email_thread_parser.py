"""Unit tests for email thread message parsing helpers.

These test the pure parsing logic (header extraction + body assembly)
independently of the Gmail API. They use the same helper functions that
the /intelligence/thread/{id} endpoint uses.
"""

import pytest

from app.email_automation import gmail_sa


def _make_raw_message(
    msg_id: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    internal_date_ms: int,
    body_text: str,
) -> dict:
    """Build a minimal Gmail API ``messages`` resource dict for testing."""
    import base64

    encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
    return {
        "id": msg_id,
        "threadId": "thread_abc",
        "internalDate": str(internal_date_ms),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": encoded},
            "parts": [],
        },
    }


def test_message_resource_plain_text_returns_body():
    msg = _make_raw_message(
        "msg1", "alice@example.com", "bob@example.com",
        "Payment Reminder", 1_700_000_000_000, "Please pay ₹50 L."
    )
    text = gmail_sa.message_resource_plain_text(msg, max_chars=8000)
    assert "Please pay" in text
    assert "₹50 L" in text


def test_message_resource_internal_date_ms_returns_int():
    msg = _make_raw_message(
        "msg2", "a@b.com", "c@d.com", "Sub", 1_620_000_000_123, "body"
    )
    assert gmail_sa.message_resource_internal_date_ms(msg) == 1_620_000_000_123


def test_internal_date_ms_missing_returns_zero():
    msg = {"id": "x", "payload": {"headers": [], "body": {}, "parts": []}}
    assert gmail_sa.message_resource_internal_date_ms(msg) == 0


def test_message_resource_plain_text_empty_body_returns_empty():
    msg = {
        "id": "y",
        "payload": {"mimeType": "text/plain", "headers": [], "body": {}, "parts": []},
    }
    assert gmail_sa.message_resource_plain_text(msg, max_chars=8000) == ""


def test_message_resource_plain_text_prefers_richest_plain_branch_over_shorter_html():
    import base64

    def encode(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode()

    long_plain = (
        "Hello team,\n\n"
        "Here is the full payment update with the earlier context preserved.\n"
        "- Invoice A pending\n"
        "- Invoice B disputed\n"
        "- Expected closure by Friday\n\n"
        "Thanks,\nFinance"
    )
    short_html = "<div>Hello team,<br/>Expected closure by Friday.</div>"
    msg = {
        "id": "msg-rich",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [],
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode(long_plain)}, "parts": []},
                {"mimeType": "text/html", "body": {"data": encode(short_html)}, "parts": []},
            ],
        },
    }

    text = gmail_sa.message_resource_plain_text(msg)
    assert "Invoice A pending" in text
    assert "Invoice B disputed" in text
    assert "Expected closure by Friday" in text


def test_message_resource_plain_text_reads_nested_multipart_mixed_thread_body():
    import base64

    def encode(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode()

    nested_plain = (
        "Dear Team,\n\n"
        "Sharing the full thread body from inside multipart/alternative.\n"
        "Please consider this the latest response.\n\n"
        "Regards,\nHospital Finance"
    )
    msg = {
        "id": "msg-nested",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "body": {},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "body": {},
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": encode(nested_plain)}, "parts": []},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "statement.pdf",
                    "body": {"attachmentId": "att-1", "size": 1234},
                    "parts": [],
                },
            ],
        },
    }

    text = gmail_sa.message_resource_plain_text(msg)
    assert "full thread body" in text
    assert "latest response" in text


def test_message_resource_plain_text_does_not_truncate_by_default():
    import base64

    long_body = "A" * 130_500
    msg = {
        "id": "msg-long",
        "payload": {
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": base64.urlsafe_b64encode(long_body.encode()).decode()},
            "parts": [],
        },
    }

    text = gmail_sa.message_resource_plain_text(msg)
    assert len(text) == len(long_body)
    assert text.endswith("A")


def test_list_thread_message_ids_collects_all_pages(monkeypatch: pytest.MonkeyPatch):
    class FakeExecute:
        def __init__(self, payload: dict):
            self.payload = payload

        def execute(self):
            return self.payload

    class FakeMessages:
        def __init__(self):
            self.calls: list[dict] = []

        def list(self, **kwargs):
            self.calls.append(kwargs)
            page_token = kwargs.get("pageToken")
            if page_token is None:
                return FakeExecute({
                    "messages": [{"id": "m1"}, {"id": "m2"}],
                    "nextPageToken": "p2",
                })
            return FakeExecute({"messages": [{"id": "m3"}]})

    class FakeUsers:
        def __init__(self, messages_api: FakeMessages):
            self._messages_api = messages_api

        def messages(self):
            return self._messages_api

    class FakeService:
        def __init__(self, messages_api: FakeMessages):
            self._users = FakeUsers(messages_api)

        def users(self):
            return self._users

    messages_api = FakeMessages()
    monkeypatch.setattr(gmail_sa, "_gmail_service", lambda: FakeService(messages_api))

    ids = gmail_sa.list_thread_message_ids("thread_abc")

    assert ids == ["m1", "m2", "m3"]
    assert messages_api.calls[0]["q"] == "thread:thread_abc"
    assert messages_api.calls[0]["maxResults"] == 500


def test_parse_thread_messages_extracts_all_fields():
    """End-to-end shape check: parse a two-message fake thread the same way
    the endpoint does, and assert every field is populated correctly."""
    import base64

    def encode(s):
        return base64.urlsafe_b64encode(s.encode()).decode()

    raw_thread = {
        "id": "thread_abc",
        "messages": [
            {
                "id": "m1",
                "internalDate": "1700000000000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "sender@tata1mg.com"},
                        {"name": "To", "value": "finance@hospital.com"},
                        {"name": "Subject", "value": "Payment Reminder – Overdue Invoices | Tata1mg | 1000001144"},
                    ],
                    "body": {"data": encode("Dear Sir, please clear ₹40 L.")},
                    "parts": [],
                },
            },
            {
                "id": "m2",
                "internalDate": "1700100000000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "finance@hospital.com"},
                        {"name": "To", "value": "sender@tata1mg.com"},
                        {"name": "Subject", "value": "Re: Payment Reminder – Overdue Invoices | Tata1mg | 1000001144"},
                    ],
                    "body": {"data": encode("We are processing internally.")},
                    "parts": [],
                },
            },
        ],
    }

    # Parse the same way the endpoint does
    messages = []
    for msg in raw_thread.get("messages") or []:
        payload = msg.get("payload") or {}
        headers = {
            h.get("name", ""): h.get("value", "")
            for h in payload.get("headers") or []
        }
        body = gmail_sa.message_resource_plain_text(msg, max_chars=8000)
        date_ms = gmail_sa.message_resource_internal_date_ms(msg)
        messages.append(
            {
                "message_id": msg.get("id", ""),
                "from_addr": headers.get("From", ""),
                "to_addr": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date_ms": date_ms,
                "body": body,
            }
        )

    assert len(messages) == 2

    first = messages[0]
    assert first["message_id"] == "m1"
    assert first["from_addr"] == "sender@tata1mg.com"
    assert first["to_addr"] == "finance@hospital.com"
    assert "1000001144" in first["subject"]
    assert first["date_ms"] == 1_700_000_000_000
    assert "₹40 L" in first["body"]

    second = messages[1]
    assert second["message_id"] == "m2"
    assert second["from_addr"] == "finance@hospital.com"
    assert "internally" in second["body"]
