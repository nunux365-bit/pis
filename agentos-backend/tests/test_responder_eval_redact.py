"""Tests for responder eval PII redaction (regex + Presidio)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.responder_eval.pii_patterns import apply_regex_redactions, order_ids_in_text
from app.agents.responder_eval.presidio_engine import (
    presidio_available,
    redact_free_text,
    reset_presidio_engines_for_tests,
)
from app.agents.responder_eval.redact import redact_chat_messages, redact_run_document, redact_string
from app.config.settings import settings

PO = "PO13326295207344"


@pytest.fixture(autouse=True)
def _disable_presidio_by_default(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_presidio_enabled", False)
    reset_presidio_engines_for_tests()


class TestRegexRedaction:
    @pytest.mark.parametrize(
        "raw,token",
        [
            ("call 9876543210", "[phone]"),
            ("call 09876543210", "[phone]"),
            ("call +91 9876543210", "[phone]"),
            ("call +91-9876543210", "[phone]"),
            ("call 919876543210", "[phone]"),
            ("reach a@b.com", "[email]"),
        ],
    )
    def test_apply_regex_redactions(self, raw, token):
        assert token in apply_regex_redactions(raw)

    def test_preserves_order_id_in_message(self):
        msg = f"Where is my order {PO}?"
        out = apply_regex_redactions(msg)
        assert PO in out
        assert order_ids_in_text(out) == [PO]

    def test_preserves_medicine_name(self):
        msg = "Where is my Crocin order?"
        out = apply_regex_redactions(msg)
        assert "Crocin" in out

    def test_greeting_name_redacted_without_presidio(self):
        out = apply_regex_redactions("Hi Rahul, where is my order?")
        assert "Rahul" not in out
        assert "Hi [customer]" in out


class TestRedactChat:
    def test_redact_chat_messages_strips_phone_keeps_po(self):
        chat = {
            "chat_id": "c1",
            "messages": [
                {"role": "user", "content": f"My order {PO}, call 9876543210"},
                {"role": "assistant", "content": "Delivered. Email me at user@test.com"},
            ],
        }
        out = redact_chat_messages(chat)
        user = out["messages"][0]["content"]
        bot = out["messages"][1]["content"]
        assert PO in user
        assert "[phone]" in user
        assert "[email]" in bot

    def test_assistant_greeting_uses_customer_hint(self):
        chat = {
            "messages": [
                {"role": "assistant", "content": "Hello Priya, your order is on the way"},
            ],
        }
        out = redact_chat_messages(chat)
        assert "Priya" not in out["messages"][0]["content"]
        assert "Hello [customer]" in out["messages"][0]["content"]

    def test_assistant_self_intro_uses_agent_hint(self):
        chat = {
            "messages": [
                {"role": "assistant", "content": "Hi, I am Rahul from Tata 1mg support"},
            ],
        }
        out = redact_chat_messages(chat)
        assert "Rahul" not in out["messages"][0]["content"]
        assert "[agent]" in out["messages"][0]["content"]

    def test_product_title_pipe_segments_not_redacted(self, monkeypatch):
        monkeypatch.setattr(settings, "responder_eval_presidio_enabled", True)

        class _Result:
            def __init__(self, start: int, end: int, entity_type: str = "PERSON"):
                self.start = start
                self.end = end
                self.entity_type = entity_type
                self.score = 0.9

        text = "Order PO1 (Medrays SPF 50 | 30g) is delivered"
        pack_start = text.index("30g")
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [_Result(pack_start, pack_start + 3)]

        class _AnonOut:
            text = "Order PO1 (Medrays SPF 50 | 30g) is delivered"

        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = _AnonOut()

        with (
            patch("app.agents.responder_eval.presidio_engine.presidio_available", return_value=True),
            patch("app.agents.responder_eval.presidio_engine._get_engines", return_value=(mock_analyzer, mock_anonymizer)),
        ):
            out = redact_free_text(text, speaker="customer")

        assert "30g" in out
        mock_anonymizer.anonymize.assert_not_called()

    def test_redact_chat_messages_redacts_metadata(self):
        chat = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "Your order is shipped",
                    "metadata": {"phone": "9876543210", "sender": "connect_agent"},
                },
            ],
        }
        out = redact_chat_messages(chat)
        meta = out["messages"][0]["metadata"]
        assert meta["phone"] == "[phone]"
        assert meta["sender"] == "connect_agent"

    def test_redact_run_document_redacts_structured_email(self):
        doc = {
            "chat_id": "c1",
            "report": {"order_details": {"email": "secret@test.com", "order_id": PO}},
        }
        out = redact_run_document(doc)
        assert out["report"]["order_details"]["email"] == "[email]"
        assert out["report"]["order_details"]["order_id"] == PO

    def test_redact_run_document_redacts_payment_details(self):
        doc = {
            "chat_id": "c1",
            "report": {
                "payment_details": {
                    "data": [
                        {
                            "payment_id": 119153739,
                            "type": "PAYMENT",
                            "order_id": PO,
                            "txn_id": "28336665093",
                            "gateway_aggregator_txn_id": "1mg-PO_119153739-1",
                            "payment_instrument_details": {
                                "instrument_text": "Visa Credit Card, Hdfc Bank",
                                "unique_id": "juspay-card-token",
                                "last_four_digits": "0000",
                                "payment_method": "VISA",
                                "card_issuer": "HDFC Bank",
                            },
                            "payment_instrument_summary": "Card - Visa",
                        }
                    ],
                    "is_success": True,
                }
            },
        }
        out = redact_run_document(doc)
        row = out["report"]["payment_details"]["data"][0]
        assert row["order_id"] == PO
        assert row["type"] == "PAYMENT"
        assert row["payment_id"] == "[id]"
        assert row["txn_id"] == "[id]"
        assert row["gateway_aggregator_txn_id"] == "[id]"
        assert row["payment_instrument_summary"] == "[id]"
        inst = row["payment_instrument_details"]
        assert inst["instrument_text"] == "[id]"
        assert inst["unique_id"] == "[id]"
        assert inst["last_four_digits"] == "[id]"
        assert inst["payment_method"] == "[id]"
        assert inst["card_issuer"] == "[id]"


class TestPresidioIntegration:
    def test_presidio_mock_anonymizes_person(self, monkeypatch):
        monkeypatch.setattr(settings, "responder_eval_presidio_enabled", True)

        class _Result:
            def __init__(self, start: int, end: int, entity_type: str = "PERSON"):
                self.start = start
                self.end = end
                self.entity_type = entity_type
                self.score = 0.9

        class _AnonOut:
            text = "Hi [customer], your order is shipped"

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [_Result(3, 8)]
        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = _AnonOut()

        with (
            patch("app.agents.responder_eval.presidio_engine.presidio_available", return_value=True),
            patch("app.agents.responder_eval.presidio_engine._get_engines", return_value=(mock_analyzer, mock_anonymizer)),
        ):
            out = redact_free_text("Hi Rahul, your order is shipped")

        assert out == "Hi [customer], your order is shipped"
        mock_analyzer.analyze.assert_called_once()
        mock_anonymizer.anonymize.assert_called_once()

    def test_presidio_disabled_uses_regex_only(self, monkeypatch):
        monkeypatch.setattr(settings, "responder_eval_presidio_enabled", False)
        out = redact_string("Contact 9876543210")
        assert "[phone]" in (out or "")

    def test_presidio_import_missing_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "responder_eval_presidio_enabled", True)
        with patch("app.agents.responder_eval.presidio_engine.presidio_available", return_value=False):
            out = redact_free_text("Contact 9876543210")
        assert "[phone]" in out


@pytest.mark.skipif(not presidio_available(), reason="presidio not installed")
def test_presidio_live_redacts_name(monkeypatch):
    """Run only when presidio + spaCy model are installed."""
    monkeypatch.setattr(settings, "responder_eval_presidio_enabled", True)
    reset_presidio_engines_for_tests()
    try:
        out = redact_free_text(f"Hi Rahul, status of {PO}? Call 9876543210")
    except OSError as exc:
        pytest.skip(f"spaCy model missing: {exc}")
    assert PO in out
    assert "[phone]" in out
    assert "Rahul" not in out
