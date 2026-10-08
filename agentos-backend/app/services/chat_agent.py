"""Assistant reply — optional OpenAI or Anthropic; always scoped to requesting user."""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import Approval, ApprovalStatus, User


def approval_card_query_matches(user_message: str, pending_n: int) -> bool:
    """Whether to attach an inline approval card (avoids 'disapprove' ⊃ 'approve')."""
    if pending_n <= 0:
        return False
    lower = user_message.strip().lower()
    return (
        re.search(
            r"\b(vendor|payment|flagged|invoice|medplus)\b",
            lower,
        )
        is not None
        or "show me" in lower
        or re.search(r"\b(approve|approvals?)\b", lower) is not None
    )


async def _first_pending_approval(db: AsyncSession, user_id: UUID) -> Approval | None:
    r = await db.execute(
        select(Approval)
        .where(
            Approval.assignee_user_id == user_id,
            Approval.status == ApprovalStatus.PENDING.value,
        )
        .order_by(Approval.created_at.desc())
        .limit(1)
    )
    return r.scalar_one_or_none()


async def _pending_summary(db: AsyncSession, user_id: UUID) -> tuple[int, list[str]]:
    q = await db.execute(
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.assignee_user_id == user_id,
            Approval.status == ApprovalStatus.PENDING.value,
        )
    )
    n = int(q.scalar() or 0)
    tq = await db.execute(
        select(Approval.title)
        .where(
            Approval.assignee_user_id == user_id,
            Approval.status == ApprovalStatus.PENDING.value,
        )
        .order_by(Approval.created_at.desc())
        .limit(5)
    )
    titles = [row[0] for row in tq.fetchall()]
    return n, titles


def _system_prompt(pending_n: int) -> str:
    return (
        "You are AgentOS, an enterprise assistant for Tata 1mg employees. "
        "Be concise and professional. Never reveal other users' data. "
        f"The current user has {pending_n} pending approvals. "
        "If they ask about pending work, mention that count. "
        "Do not fabricate financial approvals."
    )


def _deterministic_reply(lower: str, pending_n: int, titles: list[str]) -> str:
    if any(k in lower for k in ("pending", "approval", "what's due", "queue")):
        body = f"You have **{pending_n}** pending approval(s)."
        if titles:
            body += "\n\nRecent titles:\n" + "\n".join(f"- {t}" for t in titles)
        return body
    provider = settings.llm_provider
    key_hint = (
        "Set **ANTHROPIC_API_KEY** in the environment."
        if provider == "anthropic"
        else "Set **OPENAI_API_KEY** in the environment."
    )
    return (
        f"I'm running in **deterministic mode** (`LLM_PROVIDER={provider}` but no API key for "
        f"that provider). {key_hint} "
        f"You have **{pending_n}** pending approvals. "
        "Try: “What are my pending approvals?” or open the **Approvals** page."
    )


@dataclass(frozen=True)
class AssistantReplyContext:
    text: str
    pending_n: int
    titles: list[str]
    card_meta: dict | None


async def load_assistant_reply_context(
    db: AsyncSession,
    user: User,
    user_message: str,
) -> AssistantReplyContext:
    """Load approval context. Call this before closing the DB session / LLM."""
    text = user_message.strip()
    pending_n, titles = await _pending_summary(db, user.id)
    card_meta: dict | None = None
    show_card = approval_card_query_matches(text, pending_n)
    if show_card:
        ap = await _first_pending_approval(db, user.id)
        if ap:
            amt = f"₹{ap.amount:,.0f}" if ap.amount is not None else None
            risk = ap.risk if ap.risk in ("low", "medium", "high") else "medium"
            conf = ap.confidence if ap.confidence is not None else 0
            expanded_detail: str | None = None
            if ap.payload and isinstance(ap.payload, dict):
                expanded_detail = ap.payload.get("expanded_detail") or ap.payload.get(
                    "line_item_summary"
                )
                if isinstance(expanded_detail, str):
                    expanded_detail = expanded_detail.strip() or None
            card_meta = {
                "approval_card": {
                    "approval_id": str(ap.id),
                    "title": ap.title,
                    "amount": amt,
                    "confidence": conf,
                    "risk": risk,
                    "summary": [
                        ap.description or "Review line items in Approvals.",
                        f"Queued by **{ap.agent_name}**.",
                        "Use Approve / Reject here or open the full Approvals page.",
                    ],
                    "expanded_detail": expanded_detail,
                }
            }
    return AssistantReplyContext(
        text=text, pending_n=pending_n, titles=titles, card_meta=card_meta
    )


async def generate_assistant_reply_from_context(
    ctx: AssistantReplyContext,
) -> tuple[str, dict | None]:
    """LLM (or deterministic fallback). No DB session."""
    text = ctx.text
    lower = text.lower()
    pending_n = ctx.pending_n
    titles = ctx.titles
    card_meta = ctx.card_meta

    use_anthropic = settings.llm_provider == "anthropic"
    has_key = settings.anthropic_api_key if use_anthropic else settings.openai_api_key
    if not has_key:
        return _deterministic_reply(lower, pending_n, titles), card_meta

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        sys = SystemMessage(content=_system_prompt(pending_n))
        human = HumanMessage(content=text)

        if use_anthropic:
            from langchain_anthropic import ChatAnthropic

            llm = ChatAnthropic(
                model=settings.anthropic_chat_model,
                temperature=0.2,
                api_key=settings.anthropic_api_key,
            )
        else:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=settings.openai_chat_model,
                temperature=0.2,
                api_key=settings.openai_api_key,
            )
        resp = await llm.ainvoke([sys, human])
        return (resp.content or "").strip(), card_meta
    except Exception as exc:
        return (
            f"I couldn't reach the language model ({exc!s}). "
            f"You have **{pending_n}** pending approvals."
            + (
                "\n\nTop items:\n" + "\n".join(f"- {t}" for t in titles)
                if titles
                else ""
            ),
            card_meta,
        )


async def generate_assistant_reply(
    db: AsyncSession,
    user: User,
    user_message: str,
) -> tuple[str, dict | None]:
    """Load context then reply. Prefer split helpers when the session must close first."""
    ctx = await load_assistant_reply_context(db, user, user_message)
    return await generate_assistant_reply_from_context(ctx)
