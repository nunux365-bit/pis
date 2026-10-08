"""Unit tests for responder eval chat_source (conversations + messages)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.responder_eval.chat_source import (
    BOT_MESSAGE_NATURES,
    ChatFetchResult,
    _serialize_created_at,
    build_chat_doc,
    fetch_chats,
    looks_like_customer_null_text,
    looks_like_null_details_bot_text,
    resolve_message_role,
    role_from_details,
    role_from_nature,
    role_from_participant_type,
)
from app.agents.responder_eval.constants import CHAT_ROLE_AGENT, CHAT_ROLE_BOT, CHAT_ROLE_USER
from app.config.settings import settings

FIXTURE_CHAT_DIR = __import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "responder_eval"


def test_role_from_details_user_when_null_or_empty():
    assert role_from_details(None) == CHAT_ROLE_USER
    assert role_from_details(None, "I want to return it") == CHAT_ROLE_USER
    assert role_from_details("") == CHAT_ROLE_USER
    assert role_from_details("   ") == CHAT_ROLE_USER
    assert role_from_details([]) == CHAT_ROLE_USER


def test_role_from_details_bot_for_null_details_system_copy():
    welcome = "Hi , Welcome to Tata 1MG support.\nI am Nova, your virtual health assistant."
    assert role_from_details(None, welcome) == CHAT_ROLE_BOT
    assert role_from_details(None, "Your chat has ended") == CHAT_ROLE_BOT
    assert role_from_details(None, "You are no longer responding. Feel free to reach out.") == CHAT_ROLE_BOT
    assert looks_like_null_details_bot_text("Abandonment timer started") is True
    assert looks_like_null_details_bot_text(
        "You are connected with Tata 1mg Live Chat Support. Please confirm if we are still connected."
    )


def test_looks_like_customer_null_text():
    assert looks_like_customer_null_text("Pharmacy Orders") is True
    assert looks_like_customer_null_text("PO18826060680900 · Montina-L Tablet · Delivered") is True
    assert looks_like_customer_null_text("I didn't receive the packet") is True
    assert looks_like_customer_null_text("Why such a high wait time?") is True
    assert looks_like_customer_null_text("Have you checked with family members and guard ?") is False


def test_bot_message_natures_are_fixed_set():
    assert BOT_MESSAGE_NATURES == frozenset({"notification", "system_message"})


def test_role_from_participant_type_maps_user_agent_bot():
    assert role_from_participant_type("user") == CHAT_ROLE_USER
    assert role_from_participant_type("agent") == CHAT_ROLE_AGENT
    assert role_from_participant_type("bot") == CHAT_ROLE_BOT
    assert role_from_participant_type("USER") == CHAT_ROLE_USER
    assert role_from_participant_type(None) is None


def test_role_from_nature_marks_platform_messages_as_bot():
    assert role_from_nature("notification") == CHAT_ROLE_BOT
    assert role_from_nature("system_message") == CHAT_ROLE_BOT
    assert role_from_nature("SYSTEM_MESSAGE") == CHAT_ROLE_BOT
    assert role_from_nature(None) is None
    assert role_from_nature("message") is None


def test_participant_type_wins_over_nature():
    row = {
        "participant_type": "user",
        "nature": "system_message",
        "text": "Connecting you to an agent in 13 mins.",
        "details": None,
    }
    assert resolve_message_role(row) == CHAT_ROLE_USER


def test_nature_used_when_participant_type_absent():
    row = {
        "nature": "system_message",
        "text": "Connecting you to an agent in 13 mins.",
        "details": None,
    }
    assert resolve_message_role(row) == CHAT_ROLE_BOT


def test_resolve_message_role_precedence_participant_type_nature_details():
    assert (
        resolve_message_role(
            {"nature": "notification", "participant_type": "user", "text": "x", "details": None},
        )
        == CHAT_ROLE_USER
    )
    assert (
        resolve_message_role(
            {"nature": "notification", "text": "Queue update", "details": None},
        )
        == CHAT_ROLE_BOT
    )
    assert (
        resolve_message_role(
            {"participant_type": "user", "text": "I didn't receive the packet", "details": None},
        )
        == CHAT_ROLE_USER
    )
    assert (
        resolve_message_role(
            {"participant_type": "agent", "text": "Agent reply", "details": None},
        )
        == CHAT_ROLE_AGENT
    )
    assert (
        resolve_message_role(
            {"participant_type": "bot", "text": "Nova greeting", "details": {"sender_name": "Webhook"}},
        )
        == CHAT_ROLE_BOT
    )
    assert (
        resolve_message_role({"text": "I didn't receive the packet", "details": None})
        == CHAT_ROLE_USER
    )


def test_build_chat_doc_uses_participant_type():
    rows = [
        {
            "participant_type": "bot",
            "text": "Bot greeting",
            "details": {"sender_name": "Webhook"},
        },
        {"participant_type": "user", "text": "I didn't receive the packet", "details": None},
        {"participant_type": "agent", "text": "Have you checked with family members?", "details": None},
    ]
    doc = build_chat_doc("chat-1", rows)
    assert [m["role"] for m in doc["messages"]] == [CHAT_ROLE_BOT, CHAT_ROLE_USER, CHAT_ROLE_AGENT]


def test_build_chat_doc_prod_21506748_human_agent_roles():
    """Regression: participant_type from chat DB maps user / agent / bot correctly."""
    rows = [
        {
            "participant_type": "bot",
            "text": "Hello, I am Nova, your order support assistant. How may I assist you today?",
            "details": {"sender_name": "Webhook", "show_user_input": True, "is_external_chat": True},
        },
        {"participant_type": "user", "text": "Pharmacy Orders", "details": None},
        {
            "participant_type": "bot",
            "text": "Here are your recent orders — please select the one you need help with.",
            "details": {"sender_name": "Webhook", "show_user_input": True, "is_external_chat": True},
        },
        {
            "participant_type": "user",
            "text": "PO18826060680900 · Montina-L Tablet · Delivered",
            "details": None,
        },
        {
            "participant_type": "bot",
            "text": "I see your order for Montina-L Tablet is delivered. What would you like to do with this order?",
            "details": {"sender_name": "Webhook", "show_user_input": True, "is_external_chat": True},
        },
        {"participant_type": "user", "text": "I didn't receive the packet", "details": None},
        {
            "participant_type": "bot",
            "text": "I can see order PO18826060680900 is marked Delivered, and I'm connecting you to our team.",
            "details": {"action": "display_connect_timer", "sender_name": "Webhook", "show_user_input": True},
        },
        {
            "participant_type": "bot",
            "nature": "notification",
            "text": "Connecting you to an agent in 13 mins.",
            "details": {"show_user_input": True},
        },
        {"participant_type": "user", "text": "Why such a high wait time?", "details": None},
        {
            "participant_type": "bot",
            "text": "Our agents are busy assisting other customers. Your request will be serviced shortly.",
            "details": {"show_user_input": True},
        },
        {
            "participant_type": "bot",
            "nature": "system_message",
            "text": "Chatting with Deepanka Bajaj",
            "details": {"event": "agent_joined", "show_user_input": True},
        },
        {
            "participant_type": "bot",
            "nature": "system_message",
            "text": "Ronit A has been in queue for 12mins24secs",
            "details": {"visible_to": ["agent"], "show_user_input": True},
        },
        {
            "participant_type": "agent",
            "text": "Welcome to Tata 1mg esteemed customer service. How may I assist you today?",
            "details": None,
        },
        {
            "participant_type": "agent",
            "text": "Thank you for sharing your concern. I will certainly assist you with a resolution.",
            "details": None,
        },
        {"participant_type": "agent", "text": "Have you checked with family members and guard ?", "details": None},
        {
            "participant_type": "agent",
            "text": "And also this shows delivered on 8th Jul, and you are reporting now ?",
            "details": None,
        },
        {"participant_type": "agent", "text": "Are you connected ?", "details": None},
        {
            "participant_type": "bot",
            "nature": "system_message",
            "text": "Abandonment timer started",
            "details": {"visible_to": ["agent"]},
        },
        {
            "participant_type": "bot",
            "nature": "system_message",
            "text": "Connecting to bot",
            "details": {"show_user_input": False},
        },
        {"participant_type": "bot", "text": "How would you like to proceed?", "details": {}},
        {
            "participant_type": "bot",
            "text": "You are connected with Tata 1mg Live Chat Support. Please confirm if we are still connected.",
            "details": None,
        },
        {"participant_type": "bot", "text": "Thank you for choosing TATA 1mg as your health partner.", "details": {}},
        {
            "participant_type": "bot",
            "text": "Thank you for choosing TATA 1mg as your health partner.",
            "details": {"type": "action", "actions": [{"type": "feedback"}], "show_user_input": False},
        },
        {
            "participant_type": "bot",
            "nature": "system_message",
            "text": "Your chat has ended",
            "details": {"event": "chat_closed", "visible_to": ["user"], "show_user_input": False},
        },
    ]
    doc = build_chat_doc("21506748", rows)
    roles = [m["role"] for m in doc["messages"]]
    assert roles[13:17] == [CHAT_ROLE_AGENT, CHAT_ROLE_AGENT, CHAT_ROLE_AGENT, CHAT_ROLE_AGENT]
    assert roles[8] == CHAT_ROLE_USER
    assert roles[0] == CHAT_ROLE_BOT
    assert roles[20] == CHAT_ROLE_BOT


def test_build_chat_doc_maps_null_welcome_to_bot():
    doc = build_chat_doc(
        "56304",
        [
            {
                "text": "Hi , Welcome to Tata 1MG support.\nI am Nova, your virtual health assistant.",
                "details": None,
            },
            {"text": "I have other concerns", "details": {"key": "I have other concerns"}},
        ],
    )
    assert doc["messages"][0]["role"] == CHAT_ROLE_BOT
    assert doc["messages"][1]["role"] == CHAT_ROLE_USER


def test_role_from_details_agent_for_jsonb_string():
    assert role_from_details("pharmacy") == CHAT_ROLE_BOT
    assert role_from_details("connect_agent") == CHAT_ROLE_BOT


def test_role_from_details_agent_for_jsonb_object():
    assert role_from_details({"event": "chat_closed"}) == CHAT_ROLE_BOT


def test_role_from_details_assistant_for_empty_jsonb_object():
    assert role_from_details({}) == CHAT_ROLE_BOT


def test_role_from_details_user_for_quick_reply_key():
    assert role_from_details({"key": "pharmacy"}) == "user"
    assert role_from_details({"key": "Connect me to an Agent", "show_user_input": True}) == "user"


def test_build_chat_doc_orders_and_maps_rows():
    ts1 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
    ts2 = datetime(2026, 5, 18, 10, 1, 0, tzinfo=UTC)
    rows = [
        {"text": "Where is my order?", "details": None, "created_at": ts1},
        {"text": "Delivered yesterday.", "details": "connect_agent", "created_at": ts2},
        {"text": "   ", "details": None, "created_at": ts2},
        {"text": None, "details": None, "created_at": ts2},
    ]
    doc = build_chat_doc("chat-1", rows)
    assert doc["chat_id"] == "chat-1"
    assert len(doc["messages"]) == 2
    assert doc["messages"][0] == {
        "role": "user",
        "content": "Where is my order?",
        "created_at": ts1.isoformat(),
    }
    assert doc["messages"][1]["role"] == CHAT_ROLE_BOT
    assert doc["messages"][1]["content"] == "Delivered yesterday."
    assert doc["messages"][1]["metadata"] == {"sender": "connect_agent"}


def test_serialize_created_at_treats_naive_as_utc():
    naive = datetime(2026, 5, 18, 10, 0, 0)
    assert _serialize_created_at(naive) == "2026-05-18T10:00:00+00:00"
    aware = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
    assert _serialize_created_at(aware) == "2026-05-18T10:00:00+00:00"


def test_build_chat_doc_naive_created_at_is_utc_iso():
    naive = datetime(2026, 5, 18, 10, 0, 0)
    doc = build_chat_doc("chat-1", [{"text": "Hello", "details": None, "created_at": naive}])
    assert doc["messages"][0]["created_at"] == "2026-05-18T10:00:00+00:00"


def test_fetch_chats_from_fixtures(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", True)
    monkeypatch.setattr(settings, "responder_eval_chat_fixture_dir", str(FIXTURE_CHAT_DIR))
    result = fetch_chats(["chat-fixture-001", "missing"])
    assert "chat-fixture-001" in result.chats
    assert result.chats["chat-fixture-001"]["messages"]
    assert result.not_closed_ids == frozenset()
    assert result.missing_ids == frozenset({"missing"})


def test_parse_conversation_id_accepts_numeric_strings():
    from app.agents.responder_eval.chat_source import _parse_conversation_id

    assert _parse_conversation_id("42") == 42
    assert _parse_conversation_id("  99 ") == 99
    assert _parse_conversation_id("chat-abc") is None


def test_fetch_from_db_closed_with_messages(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)

    conv_rows = [{"id": 42, "status": "closed"}]
    msg_rows = [
        {
            "conversation_id": 42,
            "participant_id": 1,
            "text": "Hello",
            "details": "user-meta",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "conversation_id": 42,
            "participant_id": 2,
            "text": "Hi there",
            "details": None,
            "created_at": datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        },
    ]

    async def _fake_conv(int_ids):
        return conv_rows

    async def _fake_msg(closed_conv_ids):
        assert closed_conv_ids == [42]
        return msg_rows

    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_conv_rows_from_db",
        _fake_conv,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_msg_rows_from_db",
        _fake_msg,
    )
    result = fetch_chats(["42"])

    assert result.chats["42"]["messages"][0]["role"] == CHAT_ROLE_BOT
    assert result.chats["42"]["messages"][1]["role"] == "user"
    assert result.not_closed_ids == frozenset()
    assert result.missing_ids == frozenset()
    assert result.empty_closed_ids == frozenset()


def test_fetch_from_db_not_closed_stays_pending_candidate(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)

    conv_rows = [{"id": 7, "status": "active"}]

    async def _fake_conv(int_ids):
        return conv_rows

    async def _fake_msg(closed_conv_ids):
        assert closed_conv_ids == []
        return []

    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_conv_rows_from_db",
        _fake_conv,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_msg_rows_from_db",
        _fake_msg,
    )
    result = fetch_chats(["7"])

    assert "7" not in result.chats
    assert result.not_closed_ids == frozenset({"7"})


def test_fetch_from_db_missing_conversation(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)

    async def _fake_conv(int_ids):
        return []

    async def _fake_msg(closed_conv_ids):
        return []

    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_conv_rows_from_db",
        _fake_conv,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_msg_rows_from_db",
        _fake_msg,
    )
    result = fetch_chats(["99999"])

    assert result.missing_ids == frozenset({"99999"})
    assert result.invalid_ids == frozenset()


def test_fetch_from_db_closed_but_empty_messages(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)

    conv_rows = [{"id": 55, "status": "closed"}]

    async def _fake_conv(int_ids):
        return conv_rows

    async def _fake_msg(closed_conv_ids):
        assert closed_conv_ids == [55]
        return []

    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_conv_rows_from_db",
        _fake_conv,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_msg_rows_from_db",
        _fake_msg,
    )
    result = fetch_chats(["55"])

    assert "55" not in result.chats
    assert result.empty_closed_ids == frozenset({"55"})


def test_fetch_from_db_rejects_non_numeric_chat_id(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)
    result = fetch_chats(["chat-abc"])
    assert result.invalid_ids == frozenset({"chat-abc"})
    assert result.missing_ids == frozenset()


@pytest.mark.asyncio
async def test_fetch_from_db_queries_conv_once(monkeypatch):
    from app.agents.responder_eval.chat_source import fetch_chats_async

    monkeypatch.setattr(settings, "responder_eval_use_chat_fixtures", False)
    conv_calls = 0
    msg_calls = 0

    async def _fake_conv(int_ids):
        nonlocal conv_calls
        conv_calls += 1
        return [{"id": 42, "status": "closed"}]

    async def _fake_msg(closed_conv_ids):
        nonlocal msg_calls
        msg_calls += 1
        assert closed_conv_ids == [42]
        return [
            {
                "conversation_id": 42,
                "participant_id": 1,
                "text": "Hello",
                "details": None,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_conv_rows_from_db",
        _fake_conv,
    )
    monkeypatch.setattr(
        "app.agents.responder_eval.chat_source._load_msg_rows_from_db",
        _fake_msg,
    )

    result = await fetch_chats_async(["42"])
    assert conv_calls == 1
    assert msg_calls == 1
    assert "42" in result.chats


@pytest.mark.asyncio
async def test_tick_skips_not_closed_without_failing(monkeypatch):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.chat_source import ChatFetchResult
    from app.agents.responder_eval.tick import DumpWorkItem

    fail_mock = AsyncMock()

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="open-chat",
                    order_id="PO1",
                    run_id="r1",
                    eval_artifact={},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(return_value=ChatFetchResult(not_closed_ids=frozenset({"open-chat"}))),
    )
    monkeypatch.setattr(tick, "fail_eval", fail_mock)
    monkeypatch.setattr(tick, "mark_dump_eval_status", AsyncMock())
    monkeypatch.setattr(tick, "abandon_eval", AsyncMock())

    out = await tick.run_responder_eval_batch()
    assert out["skipped_not_closed"] == 1
    assert out["evaluated"] == 0
    fail_mock.assert_not_called()


@pytest.mark.asyncio
async def test_tick_abandons_missing_conversation(monkeypatch):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.chat_source import ChatFetchResult
    from app.agents.responder_eval.tick import DumpWorkItem

    abandon_mock = AsyncMock()
    fail_mock = AsyncMock()

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="999999",
                    order_id="PO1",
                    run_id="r1",
                    eval_artifact={},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(return_value=ChatFetchResult(missing_ids=frozenset({"999999"}))),
    )
    monkeypatch.setattr(tick, "abandon_eval", abandon_mock)
    monkeypatch.setattr(tick, "fail_eval", fail_mock)

    out = await tick.run_responder_eval_batch()
    assert out["abandoned"] == 1
    abandon_mock.assert_awaited_once()
    args, kwargs = abandon_mock.await_args
    assert args[0] == "999999"
    assert args[1] == "conversation_not_found"
    assert kwargs["expected_run_id"] == "r1"
    fail_mock.assert_not_called()


@pytest.mark.asyncio
async def test_tick_fetch_error_does_not_fail_dumps(monkeypatch):
    from app.agents.responder_eval import tick
    from app.agents.responder_eval.tick import DumpWorkItem

    fail_mock = AsyncMock()

    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(
        tick,
        "_load_batch_work_items",
        AsyncMock(
            return_value=[
                DumpWorkItem(
                    chat_id="42",
                    order_id="PO1",
                    run_id="r1",
                    eval_artifact={},
                )
            ]
        ),
    )
    monkeypatch.setattr(tick, "fetch_chats_async", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(tick, "fail_eval", fail_mock)

    out = await tick.run_responder_eval_batch()
    assert "fetch_error" in out
    assert out["evaluated"] == 0
    fail_mock.assert_not_called()
