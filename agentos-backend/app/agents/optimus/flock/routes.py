"""Flock webhook handler for bot messages."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from app.agents.optimus import config
from app.agents.optimus.flock import api_client, service as flock_service
from app.agents.optimus.flock.formatting import (
    dual_message,
    flockml_to_markdown,
    is_flockml as _is_flockml,
    markdown_to_flockml as _markdown_to_flockml,
    strip_markdown as _strip_markdown,
)
from app.agents.optimus.smartqna import session_storage
from app.agents.optimus.smartqna.agent import answer_question
from app.agents.optimus import run_store

log = logging.getLogger(__name__)

# Separate from SmartQnA answer_question semaphore — nested acquire on one Semaphore deadlocks.
# Queueing (never drop): excess handlers wait here without holding a DB session.
_flock_handler_semaphore = asyncio.Semaphore(max(1, int(config.FLOCK_MAX_CONCURRENT_HANDLERS)))


# ─────────────────────────────────────────────────────────────────────────────
# Thinking indicators - shown while processing
# ─────────────────────────────────────────────────────────────────────────────

THINKING_PHRASES = [
    "⏳ Optimus is thinking…",
    "🔍 Searching the knowledge base…",
    "🧠 Processing your query…",
    "⚙️ Analysing the documents…",
    "📚 Consulting the knowledge base…",
    "🔎 Examining your question…",
    "💭 Working on a response…",
    "⏳ Retrieving the answer…",
    "🧩 Putting together a response…",
    "📖 Scanning relevant documents…",
    "⚡ On it — give me a moment…",
    "🤔 Digging through the details…",
]


# FlockML/Markdown converters (is_flockml, strip_markdown, markdown_to_flockml,
# flockml_to_markdown) now live in flock/formatting.py so service.py can share them.


def _format_response_with_sources(answer: str, citations: list[dict]) -> str:
    """
    Format the answer with source citations for Flock.

    Since answer_question() now returns FlockML directly for channel="flock",
    this function just appends sources if citations exist.
    Falls back to markdown conversion for any non-FlockML responses (edge cases).
    """
    text = answer.strip()

    # Ensure we have FlockML (should already be FlockML from LLM, but handle edge cases)
    if not _is_flockml(text):
        text = _markdown_to_flockml(text)

    if citations:
        # Extract unique document names
        unique_sources = list(dict.fromkeys(
            c.get("document", "") for c in citations
            if c.get("document") and c.get("document") not in ("Document", "")
        ))

        if unique_sources:
            source_items = "".join(f"• {src}<br/>" for src in unique_sources[:3])
            sources_block = f"<br/><b>📎 Sources:</b><br/>{source_items}"
            # Inject sources before closing </flockml> tag
            text = text[:-len("</flockml>")] + sources_block + "</flockml>"

    return text


# ─────────────────────────────────────────────────────────────────────────────
# Install handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_install_async(flock_user_id: str, user_token: str) -> None:
    """
    Handle app.install event asynchronously (fire-and-forget).

    Called from the webhook endpoint after returning HTTP 200 to Flock.
    Uses the userToken to resolve the user's email via Flock API.
    """
    try:
        result = await flock_service.auto_link_on_install(flock_user_id, user_token)

        # Send welcome message to user via bot.
        # Prefer a pre-built flockml payload (e.g. messages with links); otherwise
        # wrap the plain message text in a <flockml> envelope.
        message = result.get("message", "Optimus bot installed successfully!")
        welcome_flockml = result.get("flockml") or f"<flockml>{message}</flockml>"
        await api_client.send_message(
            to=flock_user_id,
            text=message,
            flockml=welcome_flockml,
        )
    except Exception as e:
        log.exception("[Flock] Error processing app.install for %s: %s", flock_user_id, e)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook signature verification
# ─────────────────────────────────────────────────────────────────────────────

def verify_event_token(event_token: str, expected_user_id: str | None = None) -> bool:
    """
    Verify the JWT that Flock sends in the `x-flock-event-token` header.

    Flock signs every webhook event with a JWT (HS256) using the app secret.
    The JWT carries the claims: appId, userId, exp, iat, jti. There is NO
    verification token in the JSON body — authentication lives entirely in
    this header.

    Args:
        event_token: Value of the `x-flock-event-token` request header.
        expected_user_id: If given, the token's `userId` claim must match it
            (guards against replaying another user's token).

    Returns:
        True if the token is present, correctly signed, unexpired, and its
        claims match the configured app. False otherwise.
    """
    if not config.FLOCK_APP_SECRET:
        log.warning("[Flock] No app secret configured, cannot verify event token")
        return False

    if not event_token:
        log.warning("[Flock] Missing x-flock-event-token header, rejecting")
        return False

    try:
        claims = jwt.decode(
            event_token,
            config.FLOCK_APP_SECRET,
            algorithms=["HS256"],
            options={"require_exp": True, "require_iat": True},
        )
    except ExpiredSignatureError:
        log.warning("[Flock] Event token expired, rejecting")
        return False
    except JWTError:
        log.warning("[Flock] Invalid event token, rejecting")
        return False

    if config.FLOCK_APP_ID and claims.get("appId") != config.FLOCK_APP_ID:
        log.warning("[Flock] Event token appId mismatch, rejecting")
        return False

    if expected_user_id and claims.get("userId") != expected_user_id:
        log.warning("[Flock] Event token userId mismatch, rejecting")
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main webhook handler
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_handle_message(event: dict[str, Any]) -> None:
    """Queue behind handler semaphore (never drop), then run with error handling."""
    reply_to = event.get("chat", "") or event.get("userId", "")

    # Acquire before any DB work so queued tasks do not pin pool connections.
    async with _flock_handler_semaphore:
        try:
            await _handle_message_async(event)
        except Exception as e:
            log.error("Flock message handler failed: %s", e, exc_info=True)
            try:
                await api_client.send_message(
                    to=reply_to,
                    text="<flockml>Sorry, I encountered an error processing your request. Please try again. 🙏</flockml>",
                )
            except Exception as send_err:
                log.error("Failed to send error message to user: %s", send_err)


async def handle_webhook(event: dict[str, Any]) -> dict[str, Any]:
    """
    Handle incoming Flock webhook event.

    Supported events:
    - app.mention: Bot was mentioned in a chat
    - chat.receiveMessage: Direct message to bot

    Returns response dict for Flock API.
    """
    event_type = event.get("name")

    if event_type == "client.pressButton":
        return {"text": "Button actions not yet implemented."}

    if event_type == "app.install":
        # Handled in optimus.py webhook endpoint with fire-and-forget
        return {}

    if event_type == "app.uninstall":
        return {}

    if event_type in ("app.mention", "chat.receiveMessage"):
        # Fire-and-forget; concurrency capped inside _safe_handle_message (semaphore, never drop).
        from app.infra.task_tracker import spawn

        spawn(_safe_handle_message(event), name="flock-message")
        return {}

    # Slash commands are intentionally unsupported — users just type their question.
    log.debug("Unhandled Flock event type: %s", event_type)
    return {}


async def _handle_message_async(event: dict[str, Any]) -> None:
    """
    Handle incoming message asynchronously with immediate acknowledgment.

    DB sessions are short-lived: closed before LLM and reopened only to persist
    the assistant reply (avoids holding a pool connection across OpenAI/Qdrant).
    """
    from app.db.session import AsyncSessionLocal

    message = event.get("message", {})
    text = message.get("text", "").strip()
    flock_user_id = event.get("userId", "")
    chat_id = event.get("chat", "")

    # Determine where to send reply (chat or DM)
    reply_to = chat_id if chat_id else flock_user_id

    if not text:
        await api_client.send_message(
            to=reply_to,
            text="I didn't catch that. Could you please type your question?",
        )
        return

    user_id: str | None = None
    conversation_id: str | None = None
    history: list[dict[str, Any]] = []

    # ── Session A: link user, conversation, persist user message ─────────────
    async with AsyncSessionLocal() as db_session:
        user_info = await flock_service.get_user_by_flock_id(flock_user_id, session=db_session)
        if not user_info:
            # Auto-provisioning can only happen during app.install: Flock provides a
            # per-user API token only on that event (message events carry no token and
            # the signed JWT has no email), so we cannot resolve or create an account
            # here. Guide the user to (re)install, which will set them up automatically.
            not_linked = dual_message(
                "Hmm, I couldn't find your Optimus account — it looks like your Flock "
                "account isn't linked to Optimus, so I can't answer just yet.\n\n"
                "**Reinstalling the app fixes this** — it links you automatically:\n"
                "1. Go to [apps.flock.com/manage](https://apps.flock.com/manage), click the "
                "3 dots next to Optimus and remove it.\n"
                "2. Go to [apps.flock.com](https://apps.flock.com/), search for Optimus and "
                "install it.\n\n"
                "Then come back and ask your question. Still stuck? "
                f"[Raise a request here]({flock_service.SUPPORT_FORM_URL})."
            )
            await api_client.send_message(
                to=reply_to,
                text=not_linked["message"],
                flockml=not_linked["flockml"],
            )
            return

        user_id = user_info["user_id"]
        conversation = await session_storage.get_or_create_flock_conversation(
            user_id=user_id,
            flock_chat_id=chat_id or flock_user_id,
            session=db_session,
        )
        conversation_id = conversation["id"]
        await session_storage.add_message(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=text,
            session=db_session,
        )
        history = await run_store.get_history(conversation_id)

    # Session A closed — no PG connection held across thinking / LLM.
    thinking_phrase = random.choice(THINKING_PHRASES)
    thinking_flockml = f"<flockml><i>{thinking_phrase}</i></flockml>"
    await api_client.send_message(
        to=reply_to,
        text=thinking_phrase,
        flockml=thinking_flockml,
    )

    try:
        assert user_id is not None and conversation_id is not None
        result = await answer_question(
            text,
            conversation_history=history,
            channel="flock",
            user_id=user_id,
            conversation_id=conversation_id,
        )

        response_flockml = _format_response_with_sources(result.answer, result.citations)
        response_plain = _strip_markdown(result.answer)

        if result.confidence.value == "PARTIAL":
            prefix = "<i>(Based on partially relevant information)</i><br/><br/>"
            response_flockml = response_flockml.replace("<flockml>", f"<flockml>{prefix}")
            response_plain = "(Based on partially relevant information)\n\n" + response_plain

        markdown_content = flockml_to_markdown(result.answer)

        # ── Session B: persist assistant reply for web UI history ─────────────
        async with AsyncSessionLocal() as db_session:
            await session_storage.add_message(
                conversation_id=conversation_id,
                user_id=user_id,
                role="assistant",
                content=markdown_content,
                citations=result.citations,
                confidence=result.confidence.value,
                session=db_session,
            )

        await api_client.send_message(
            to=reply_to,
            text=response_plain,
            flockml=response_flockml,
        )

    except Exception as e:
        log.exception("[Flock] Error handling message from %s: %s", flock_user_id, e)
        err = dual_message(
            "⚠️ **Optimus** hit an unexpected error. Please try again in a moment."
        )
        await api_client.send_message(
            to=reply_to,
            text=err["message"],
            flockml=err["flockml"],
        )


