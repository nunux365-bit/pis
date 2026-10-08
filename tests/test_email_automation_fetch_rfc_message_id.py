"""Gmail metadata fetch for outbound Message-ID (weekly thread chain)."""

from __future__ import annotations

from app.email_automation import gmail_sa


def test_fetch_message_rfc_message_id_reads_metadata_header(monkeypatch):
    class FakeReq:
        def execute(self):
            return {
                "payload": {
                    "headers": [
                        {"name": "Message-ID", "value": " <abc@mail.gmail.com> "},
                    ]
                }
            }

    class FakeMessages:
        def get(self, **kwargs):
            assert kwargs["userId"] == "me"
            assert kwargs["id"] == "gmail-internal-id"
            assert kwargs["format"] == "metadata"
            assert kwargs["metadataHeaders"] == ["Message-ID"]
            return FakeReq()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeSvc:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(
        "app.email_automation.gmail_sa._gmail_service",
        lambda: FakeSvc(),
    )
    assert gmail_sa.fetch_message_rfc_message_id("gmail-internal-id") == "<abc@mail.gmail.com>"


def test_fetch_message_rfc_message_id_empty_id_returns_none():
    assert gmail_sa.fetch_message_rfc_message_id("") is None
    assert gmail_sa.fetch_message_rfc_message_id("   ") is None


def test_fetch_message_rfc_message_id_missing_header_returns_none(monkeypatch):
    class FakeReq:
        def execute(self):
            return {"payload": {"headers": [{"name": "Subject", "value": "x"}]}}

    class FakeMessages:
        def get(self, **kwargs):
            return FakeReq()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeSvc:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(
        "app.email_automation.gmail_sa._gmail_service",
        lambda: FakeSvc(),
    )
    assert gmail_sa.fetch_message_rfc_message_id("mid") is None
