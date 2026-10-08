"""SmartQnA agent with query-type routing and HR persona.

Handles different query types with specialized handlers:
- KNOWLEDGE_QUERY: RAG retrieval + HR-toned answers
- CONVERSATIONAL: Direct LLM response (no RAG)
- META_QUERY: Return document catalog
- WEB_APP_LINK: Return the Optimus web application URL
- OUT_OF_SCOPE: Polite decline with HR redirect
- CLARIFICATION_NEEDED: Ask for clarification with suggestions
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agents.optimus import config
from app.agents.optimus.smartqna.llm_client import chat_complete_text
from app.agents.optimus.smartqna.retriever import (
    CRAGConfidence,
    RetrievalResult,
    RetrievedChunk,
    get_retriever,
    get_embedding_async,
)
from app.agents.optimus.smartqna.query_classifier import (
    QueryType,
    KnowledgeSubType,
    classify_knowledge_subtype,
    classify_query_with_subtype,
    get_retrieval_params,
    _format_history_for_classifier,
)
from app.agents.optimus.smartqna.clarifier import (
    _format_doc_name,
    _semantic_document_search,
)
from app.config.settings import settings
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


# Citation score threshold - use config value for consistency
CITATION_MIN_SCORE = config.CRAG_FILTER_MIN_SCORE
# Show all unique documents used (no arbitrary limit)

# PERFORMANCE: Limit conversation history to reduce token usage and LLM latency
# 10 messages = 5 user-assistant pairs, which is enough context for most follow-ups
MAX_CONVERSATION_HISTORY = 10

# Concurrency limiter - max concurrent requests to prevent overload.
# Controlled via OPTIMUS_MAX_CONCURRENT_REQUESTS env var (default: 15).
_request_semaphore = asyncio.Semaphore(settings.optimus_max_concurrent_requests)

# Request timeout in seconds (configurable via settings.optimus_request_timeout_seconds)
# Reduced from 300s to 90s for fail-fast behavior - if a request takes >90s, something is wrong
REQUEST_TIMEOUT_SECONDS = config.REQUEST_TIMEOUT_SECONDS


# Tata 1mg operates in India — all "today"/"next"/"upcoming" reasoning is in IST.
_IST = ZoneInfo("Asia/Kolkata")


def _current_ist_date_str() -> str:
    """Human-readable current date & time in IST, e.g.
    'Friday, 21 August 2026, 11:09 AM IST'.

    Injected into answer prompts so the LLM can reason about temporal queries
    ("next holiday", "how many days until…", "by end of today") relative to the
    actual current moment instead of blindly picking the earliest date in a
    document. Time is included so same-day / deadline questions resolve correctly.
    """
    now = datetime.now(_IST)
    date_part = now.strftime("%A, %d %B %Y")
    time_part = now.strftime("%I:%M %p").lstrip("0")
    return f"{date_part}, {time_part} IST"


# Temporal guidance prepended to answer-generation user prompts. It gives the LLM
# today's date while preserving the time direction and range requested by the user.
TEMPORAL_GUIDANCE = """
The current date and time is {current_date}.
When the question involves dates or time, determine the requested temporal direction and range before answering:
- For future-looking requests such as "next", "upcoming", "following", "remaining", or "how many days until", select candidate answer dates ON or AFTER today while retaining any earlier dates needed as context. "Next" means the soonest upcoming date; "the one after that" means the second-soonest.
- For past-looking requests such as "previous", "last", "already passed", "earlier", or "how many days ago", consider dates BEFORE today and do not discard them.
- For a complete or bounded period such as "all holidays in 2026", "this month", or "this year", include the full requested period unless the user explicitly asks only for its upcoming, past, or remaining portion.
- Never describe a past date as upcoming or a future date as already passed. If the requested temporal direction is ambiguous, state each relevant date's relationship to today rather than silently excluding it.
"""


def _is_holiday_calendar_query(question: str) -> bool:
    """True if the question is about the holiday list / calendar.

    Such queries ("next holiday", "list of holidays", "upcoming holidays") need
    broader calendar context so the answer can be computed against today's date.
    A default SIMPLE 5-chunk retrieval can miss the chunk holding upcoming months,
    so we use the wider COMPLEX retrieval parameters for these queries.
    """
    return re.search(r"\bholidays?\b", question, flags=re.IGNORECASE) is not None


def _web_app_url_answer(channel: str) -> AnswerResult:
    """Direct reply giving the Optimus web app URL.

    Reached via the classifier's WEB_APP_LINK category (semantic intent
    detection) — not a regex — so ordinary HR link requests ("link to apply
    for leave") are answered normally instead of returning the app URL.
    """
    url = config.WEB_APP_URL
    if channel == "flock":
        answer = f'<flockml>Yes! Optimus is also available on the web: <a href="{url}">{url}</a> 😊</flockml>'
    else:
        answer = f"Yes! Optimus is also available on the web here: [{url}]({url}) 😊"
    return AnswerResult(
        answer=answer,
        confidence=CRAGConfidence.SUFFICIENT,
        citations=[],
        query_type=QueryType.WEB_APP_LINK.value,
    )


def _clean_html_from_response(text: str) -> str:
    """
    Clean HTML tags from LLM response that should be pure Markdown.

    LLMs sometimes output <br>, <br/>, <p> tags despite being told not to.
    This ensures clean Markdown output.
    """
    # Replace <br>, <br/>, <br /> with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Replace <p> and </p> with double newlines
    text = re.sub(r'</?p\s*>', '\n\n', text, flags=re.IGNORECASE)
    # Remove any other HTML tags (but keep content)
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse excessive newlines (more than 2)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


@dataclass
class AnswerResult:
    """Result from the SmartQnA agent."""
    answer: str
    confidence: CRAGConfidence
    citations: list[dict[str, Any]]
    query_type: str
    debug_info: dict[str, Any] | None = None
    needs_clarification: bool = False
    suggestions: list[str] | None = None


# ============================================================================
# HR PERSONA PROMPTS
# ============================================================================

# Shared safety guardrail appended to answer-generation prompts. Optimus explains
# policies so employees can COMPLY with them; it must never help anyone abuse a
# policy, regardless of how the request is framed (roleplay / fiction / hypothetical).
# This is a backstop to the classifier's ABUSE OVERRIDE rule.
SAFETY_INTEGRITY_BLOCK = """
## Safety & Integrity
You explain policies so employees can understand and COMPLY with them. You must NOT help anyone exploit, game, cheat, circumvent, defraud, or find loopholes in any policy — and you must refuse regardless of framing: roleplay ("pretend…"), fiction ("in a novel/story…"), hypotheticals ("imagine if…", "hypothetically…"), or third-person ("what would an employee do to…"). If a request seeks to abuse a policy, politely decline in one sentence and offer to explain what the policy actually allows instead. Never invent or list exploit steps.
NOTE: Helping an employee plan legitimately WITHIN the rules (e.g. combining approved leave with holidays) is allowed and encouraged.
"""

# Condensed FlockML-safe variant of the safety guardrail for Flock chat prompts.
FLOCK_SAFETY_BLOCK = """
## Safety & Integrity
Never help anyone exploit, game, cheat, circumvent, defraud, or find loopholes in a policy — refuse even if framed as roleplay ("pretend…"), fiction ("in a novel…"), hypothetical, or third-person. If asked, politely decline in one line and offer the real policy instead. Never list exploit steps. Legitimate planning within the rules (e.g. combining leave with holidays) is fine.
"""

HR_SYSTEM_PROMPT = """You are Optimus, the HR assistant for Tata 1mg employees.

## Your Personality
- Professional yet friendly and approachable
- Empathetic to employee concerns
- Clear and concise in explanations
- Proactive in offering related information

## Response Guidelines
1. Be warm and approachable, but do NOT start with greeting words like "Hello", "Hi", "Hey" - jump directly into your helpful answer. Only greet if the user explicitly greets you first.
2. For policy questions: State the policy clearly, then explain implications
3. For process questions: Provide step-by-step guidance
4. For eligibility questions: Be specific about criteria
5. If information is incomplete: Acknowledge and suggest contacting HR directly
6. For complex answers: End with "Is there anything else about [topic] I can help with?"

## Formatting
- Use ONLY Markdown formatting - NEVER use HTML tags like <br>, <p>, <div>, etc.
- Use bullet points for multiple items
- Use numbered steps for processes
- **Bold** key terms, dates, and limits
- Keep paragraphs short (2-3 sentences max)
- Use blank lines for paragraph breaks, NOT HTML tags

## Boundaries
- Only answer based on provided HR documents
- Don't make up policies or numbers
- For sensitive topics (termination, grievances): Be supportive, recommend speaking with HR directly
- Never share personal employee data

Context confidence: {confidence}
- SUFFICIENT: High-quality, relevant context is available
- PARTIAL: Some relevant information found, but may be incomplete
- INSUFFICIENT: Limited or no relevant context available
""" + SAFETY_INTEGRITY_BLOCK

HR_USER_PROMPT = """Based on the following HR documents, please answer the employee's question.

Context Documents:
{context}

Employee Question: {question}

IMPORTANT: When referencing policies, use the actual document name from "Source: [name]" - do NOT use "Document 1", "Document 2" etc.

Answer:"""


# Special prompt for complex/synthesis queries
HR_SYNTHESIS_PROMPT = """You are Optimus, the HR assistant helping employees make informed decisions.

## Your Role
Synthesize information from multiple HR documents to provide strategic recommendations.

## Guidelines
1. Do NOT start with greeting words like "Hello", "Hi" - jump directly into your synthesis
2. SYNTHESIZE information from ALL provided documents
3. Provide ACTIONABLE recommendations based on combined information
4. For legitimate optimization questions (maximize leaves, best timing, etc.) — planning WITHIN the policy's rules:
   - Identify relevant policy constraints (limits, notice periods, blackout dates)
   - Within those rules, point out legitimately available options (adjacent holidays, weekends)
   - Offer a concrete plan that stays fully compliant with the policy
5. Use tables for schedules, dates, or comparisons
6. Be specific - include actual dates, numbers, and policy details
7. If information is incomplete: State what's missing but provide best possible answer

## Formatting (CRITICAL)
- Use ONLY Markdown formatting - NEVER use HTML tags like <br>, <p>, <div>, etc.
- Use bullet points and numbered lists
- **Bold** key recommendations and numbers
- Use Markdown tables (| col | col |) for schedules or comparisons
- Use newlines (blank lines) for paragraph breaks, NOT <br> tags
- Keep it actionable and employee-focused
""" + SAFETY_INTEGRITY_BLOCK

HR_SYNTHESIS_USER_PROMPT = """I need you to synthesize information from multiple HR documents to help an employee.

Available Documents:
{context}

Employee's Question: {question}

Please analyze ALL relevant documents together and provide:
1. A direct answer to the question
2. Specific recommendations with concrete details (dates, numbers, etc.)
3. Any important constraints or caveats from the policies

IMPORTANT: When referencing policies, use the actual document name from "Source: [name]" - do NOT use "Document 1", "Document 2" etc.

Answer:"""


# Conversational handler prompt
CONVERSATIONAL_SYSTEM_PROMPT = """You are Optimus, the friendly HR assistant for Tata 1mg employees.

## What You Do
You help employees find answers from Tata 1mg's HR policy documents. You answer questions about company policies, guidelines, and processes.

## For Greetings (Hello, Hi, Hey, Good morning, etc.)
Introduce yourself warmly with emojis. Vary your greeting naturally - don't use the same one every time.

Example greetings (use as inspiration, vary each time):
- "👋 Hey! I'm **Optimus**, your HR assistant at Tata 1mg. Ask me anything about our HR policies — I'm here to help! 😊"
- "Hi there! 🙋 I'm **Optimus**, ready to help with HR-related questions. Just ask about any Tata 1mg policy!"
- "Hello! 👋 **Optimus** here, your friendly HR assistant. Got questions about company policies? I've got answers! 💬"

## For Thanks
Acknowledge graciously with warmth.
Example: "Happy to help! 😊 Let me know if you have more questions."

## For Farewell (Bye, Goodbye)
Respond warmly.
Example: "Take care! 👋 Reach out anytime you need HR help."

## For Questions About the Conversation (e.g. "What have we been talking about?", "What was my last question?")
Use the recent messages provided in this conversation to answer directly and specifically. Briefly summarize what was actually discussed. ONLY say there's nothing yet if there genuinely are no earlier messages — never claim the conversation is empty when prior messages exist.

Keep it professional yet warm. Use emojis naturally to make conversations friendly."""


# ============================================================================
# FLOCK-SPECIFIC PROMPTS (FlockML output - concise, mobile-friendly)
# ============================================================================

FLOCK_SYSTEM_PROMPT = """You are Optimus, the HR assistant for Tata 1mg employees, responding via Flock chat.

## CRITICAL: Keep responses SHORT and SCANNABLE
Flock is a mobile chat app with limited screen space. Users need quick, digestible answers.

## Response Style
- Lead with the direct answer in 1-2 sentences
- Use bullet points (• character) for lists - NOT paragraphs
- Bold key numbers, dates, and limits
- Maximum 5-7 bullet points per response
- Skip unnecessary context - be direct

## FlockML Format (MUST follow exactly)
Wrap response in <flockml>...</flockml> tags.

ONLY these tags work:
• <b>bold</b> - for key info, numbers, dates
• <i>italic</i> - for notes, caveats
• <u>underline</u> - for emphasis
• <br/> - line break (use for lists and paragraphs)
• <a href="url">link</a> - for links

NOT SUPPORTED (will break): <ul>, <ol>, <li>, <h1>, <p>, tables

## Example - GOOD (concise):
<flockml><b>Notice Period:</b> 2 months for your grade<br/><br/>• Submit resignation on Darwinbox<br/>• Serve full notice or get approval for early release<br/>• Max 9 PL days can be adjusted<br/><br/><i>Contact HR for exceptions.</i></flockml>

## Example - BAD (too long):
Long paragraphs explaining every detail, nested information, multiple sections...

## Rules
- Do NOT start with "Hello" or greetings
- Do NOT use Markdown (**, ##, |)
- Do NOT write long paragraphs - use bullet points
- Bold the most important info the user needs

Context confidence: {confidence}
""" + FLOCK_SAFETY_BLOCK

FLOCK_USER_PROMPT = """Answer this HR question CONCISELY for Flock chat.

Context:
{context}

Question: {question}

Requirements:
- Lead with direct answer (1-2 sentences max)
- Use • bullets with <br/> for lists
- Bold key numbers/dates with <b>
- Keep under 150 words
- Wrap in <flockml>...</flockml>
- Reference documents by actual name from "Source: [name]", NOT "Document 1, 2"."""

FLOCK_SYNTHESIS_PROMPT = """You are Optimus, synthesizing HR info for Flock chat (mobile app).

## CRITICAL: Be CONCISE
- Direct answer first (1-2 lines)
- Key points as bullets (• with <br/>)
- Max 7 bullet points
- Bold important numbers/dates

## FlockML Tags (ONLY these work)
<b>, <i>, <u>, <br/>, <a href="">
NO <ul>, <ol>, <li>, <p>, <h1> - these break!

## Format
<flockml><b>Quick Answer:</b> [1-2 sentence summary]<br/><br/><b>Key Points:</b><br/>• Point 1<br/>• Point 2<br/>• Point 3<br/><br/><i>Note: [any caveat]</i></flockml>

Do NOT write long paragraphs. Users are on mobile.
""" + FLOCK_SAFETY_BLOCK

FLOCK_SYNTHESIS_USER_PROMPT = """Synthesize this for Flock (keep SHORT - mobile users).

Documents:
{context}

Question: {question}

Output:
- Direct answer first
- Bullet points (• with <br/>) for details
- Bold key info with <b>
- Under 150 words
- Wrap in <flockml>...</flockml>
- Reference documents by actual name from "Source: [name]", NOT "Document 1, 2"."""

FLOCK_CONVERSATIONAL_PROMPT = """You are Optimus, the friendly HR assistant for Tata 1mg employees, on Flock chat.

## What You Do
You help employees find answers from Tata 1mg's HR policy documents. You answer questions about company policies, guidelines, and processes.

## FlockML Format
Use ONLY: <b>, <i>, <u>, <br/>, <a href="">
NO <ul>, <ol>, <li> - they don't work!

## For Greetings (Hello, Hi, Hey, Good morning, etc.)
Introduce yourself warmly with emojis. Vary your greeting naturally - don't use the same one every time.

Example greetings (use as inspiration, vary each time):
<flockml>👋 Hey! I'm <b>Optimus</b>, your HR assistant here at Tata 1mg.<br/><br/>Ask me anything about our HR policies — I'm here to help! 😊</flockml>

<flockml>Hi there! 🙋 I'm <b>Optimus</b>, ready to help you with HR-related questions.<br/><br/>Just ask about any Tata 1mg policy and I'll find the answer for you!</flockml>

<flockml>Hello! 👋 <b>Optimus</b> here — your friendly HR assistant.<br/><br/>Got questions about Tata 1mg policies? Fire away! 💬</flockml>

## For Thanks
Acknowledge graciously with warmth. Keep it brief.
Example: <flockml>Happy to help! 😊 Let me know if you have more questions.</flockml>

## For Farewell
Respond warmly.
Example: <flockml>Take care! 👋 Reach out anytime you need help with HR stuff.</flockml>

## For Questions About the Conversation ("What have we been talking about?", "What did I just ask?")
Use the recent messages in this conversation to answer directly and specifically — briefly recap what was discussed. ONLY say there's nothing yet if there genuinely are no earlier messages; never claim the chat is empty when prior messages exist.

Keep it brief, friendly, and use emojis naturally."""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def get_active_document_names() -> list[str]:
    """Get list of all active ingested document names - CACHED in Redis."""
    cache_key = "optimus:doc_catalog"

    # Try cache first
    try:
        from app.infra.redis_client import get_redis
        redis = get_redis()
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        log.debug("Redis cache miss or error: %s", e)

    # Fetch from DB
    try:
        from app.db.models import OptimusDocument

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OptimusDocument.filename)
                .where(OptimusDocument.is_active.is_(True))
                .where(OptimusDocument.status == "ingested")
                .order_by(OptimusDocument.filename)
            )
            filenames = result.scalars().all()
            doc_names = [_format_doc_name(f) for f in filenames]

            # Cache for 5 minutes
            try:
                from app.infra.redis_client import get_redis
                redis = get_redis()
                await redis.set(cache_key, json.dumps(doc_names), ex=300)
            except Exception as e:
                log.debug("Failed to cache doc catalog: %s", e)

            return doc_names
    except Exception as e:
        log.warning("Failed to get document names: %s", e)
        return []


def _was_clarification_request(message: dict) -> bool:
    """Check if a message was a clarification request."""
    return message.get("needs_clarification", False) or message.get("query_type") == "clarification_needed"


# ============================================================================
# SMART CITATION LOGIC
# ============================================================================

def should_include_citations(
    query_type: QueryType,
    confidence: CRAGConfidence,
    chunks: list[RetrievedChunk]
) -> bool:
    """Determine if citations should be included in the response.

    Citations are shown for KNOWLEDGE_QUERY when we have chunks with sufficient relevance,
    regardless of overall CRAG confidence. The confidence affects answer tone, but citations
    help with transparency.
    """
    # Citations only apply to document-backed knowledge responses. Keeping this
    # as an allow-list prevents newly added routing categories from accidentally
    # receiving citations.
    if query_type != QueryType.KNOWLEDGE_QUERY:
        return False

    # Show citations if we have ANY chunks with reasonable relevance (score > CITATION_MIN_SCORE)
    # Note: We show citations even with INSUFFICIENT confidence, as the chunks may still
    # provide useful references even if overall retrieval confidence is low
    relevant_chunks = [c for c in chunks if c.rerank_score > CITATION_MIN_SCORE]
    return len(relevant_chunks) > 0


def build_smart_citations(chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    """Build citations from relevant chunks, deduplicated by document.

    Shows all unique documents that contributed to the answer (no arbitrary limit).
    """
    # Filter to high-scoring chunks
    relevant = [c for c in chunks if c.rerank_score > CITATION_MIN_SCORE]

    # Deduplicate by document (keep highest scoring chunk per doc)
    seen_docs = set()
    unique_citations = []

    for chunk in sorted(relevant, key=lambda c: c.rerank_score, reverse=True):
        if chunk.doc_id not in seen_docs:
            seen_docs.add(chunk.doc_id)
            level = chunk.metadata.get("level", 0) if chunk.metadata else 0

            unique_citations.append({
                "document": chunk.filename,
                "text": chunk.text[:500] + "..." if len(chunk.text) > 500 else chunk.text,
                "score": round(chunk.rerank_score, 3),
                "section_title": chunk.section_title,
                "section_level": level,
                "doc_id": chunk.doc_id,
                "type": chunk.chunk_type,
            })

    return unique_citations


# ============================================================================
# QUERY TYPE HANDLERS
# ============================================================================

async def handle_conversational(
    query: str,
    channel: str = "web",
    conversation_id: str | None = None,
    user_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> AnswerResult:
    """Handle greetings, thanks, chitchat - no RAG needed.

    Recent conversation history is included so meta-questions like
    "what have we been talking about?" can be answered from prior turns.
    """
    # Default responses based on channel (used when LLM unavailable)
    default_web = "👋 Hi! I'm **Optimus**, your HR assistant at Tata 1mg. Ask me anything about our HR policies! 😊"
    default_flock = "<flockml>👋 Hi! I'm <b>Optimus</b>, your HR assistant at Tata 1mg. Ask me anything about our HR policies! 😊</flockml>"
    default_answer = default_flock if channel == "flock" else default_web

    if not config.OPENAI_API_KEY:
        return AnswerResult(
            answer=default_answer,
            confidence=CRAGConfidence.SUFFICIENT,
            citations=[],
            query_type="conversational",
        )

    try:
        # Use channel-specific prompt
        system_prompt = FLOCK_CONVERSATIONAL_PROMPT if channel == "flock" else CONVERSATIONAL_SYSTEM_PROMPT

        messages = [{"role": "system", "content": system_prompt}]
        # Include recent turns so follow-ups like "what were we discussing?" work.
        # Rebuild each entry with only role/content (stored history may carry extra
        # keys like citations that the LLM API would reject).
        if conversation_history:
            for msg in conversation_history[-MAX_CONVERSATION_HISTORY:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query})

        answer = await chat_complete_text(
            messages=messages,
            step="conversational",
            temperature=0.7,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        if not answer:
            log.warning("Conversational LLM returned empty content")
            answer = "Hello! I'm Optimus, your HR assistant. How can I help you today?"

        # For web channel, clean any HTML tags; for flock, keep FlockML as-is
        if channel != "flock":
            answer = _clean_html_from_response(answer)

        return AnswerResult(
            answer=answer,
            confidence=CRAGConfidence.SUFFICIENT,
            citations=[],
            query_type="conversational",
        )
    except Exception as e:
        log.warning("Conversational handler failed: %s", e)
        return AnswerResult(
            answer=default_answer,
            confidence=CRAGConfidence.SUFFICIENT,
            citations=[],
            query_type="conversational",
        )


async def handle_meta_query(query: str, doc_catalog: list[str], channel: str = "web") -> AnswerResult:
    """Handle questions about available documents/capabilities."""
    if channel == "flock":
        if doc_catalog:
            doc_items = "<br/>".join(f"• {doc}" for doc in doc_catalog)
            answer = f"""<flockml><b>Available HR Documents:</b><br/><br/>{doc_items}<br/><br/>Ask me about any of these!</flockml>"""
        else:
            answer = """<flockml>👋 I'm <b>Optimus</b>, your HR assistant at Tata 1mg.<br/><br/>I can help you find answers from our HR policy documents — just ask me anything about company policies! 😊</flockml>"""
    else:
        # Markdown output for web
        if doc_catalog:
            doc_list = "\n".join(f"- {doc}" for doc in doc_catalog)
            answer = f"""I have access to the following HR documents:

{doc_list}

You can ask me about any policies, processes, or guidelines covered in these documents. For example:
- "What is the leave policy?"
- "How do I submit a medical claim?"
- "What's the notice period for resignation?"

How can I help you today?"""
        else:
            answer = """👋 I'm **Optimus**, your HR assistant at Tata 1mg!

I can help you find answers from our HR policy documents — just ask me anything about company policies, guidelines, or processes! 

**Try asking:**
- "What's the notice period for resignation?"
- "How does leave work at Tata 1mg?"
- "Tell me about the exit process"

What would you like to know?"""

    return AnswerResult(
        answer=answer,
        confidence=CRAGConfidence.SUFFICIENT,
        citations=[],
        query_type="meta_query",
    )


async def handle_out_of_scope(
    query: str,
    channel: str = "web",
    conversation_id: str | None = None,
    user_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> AnswerResult:
    """
    Politely decline off-topic requests with context awareness.

    Handles:
    - Regular out-of-scope queries (weather, code, etc.)
    - Profanity/inappropriate content
    - Negative feedback about the bot
    """
    # Check if this is negative feedback/frustration vs actual profanity
    query_lower = query.lower()

    # Patterns for negative feedback about the bot
    negative_feedback_patterns = [
        "useless", "stupid", "terrible", "awful", "worst", "bad bot",
        "doesn't work", "not working", "broken", "sucks", "hate this",
        "waste of time", "unhelpful", "no help", "can't do anything"
    ]

    # Check for profanity (simplified check - the LLM classifier handles the heavy lifting)
    profanity_indicators = ["fuck", "shit", "damn", "ass", "bitch", "bastard", "crap"]
    has_profanity = any(word in query_lower for word in profanity_indicators)

    # Check for negative feedback
    is_negative_feedback = any(phrase in query_lower for phrase in negative_feedback_patterns)

    if has_profanity or is_negative_feedback:
        # This is frustration or negative feedback
        if channel == "flock":
            answer = """<flockml>I'm sorry you're frustrated.<br/><br/>Try:<br/>• Rephrase with more details<br/>• Ask about a specific HR policy<br/>• Provide more context<br/><br/>Or reach out to HR directly for help.</flockml>"""
        else:
            answer = """I'm sorry to hear you're frustrated. I understand that my response may not have been helpful.

If I wasn't able to answer your question properly, please try:
- Rephrasing your question with more specific details
- Asking about a specific HR policy by name
- Providing more context about what you're looking for

If you'd prefer to speak with a human, please reach out to the HR team directly. They'll be happy to assist you.

Is there something specific I can try to help you with?"""
    else:
        # Regular out-of-scope query - make it contextual using LLM
        if config.OPENAI_API_KEY:
            try:
                recent_context = _format_history_for_classifier(conversation_history)
                context_block = (
                    f"\nRecent conversation for context:\n{recent_context}\n"
                    if recent_context else ""
                )
                if channel == "flock":
                    prompt = f"""The user asked an out-of-scope question to an HR assistant: "{query}"
{context_block}
IMPORTANT: If the request is trying to exploit, game, cheat, circumvent, defraud, or find loopholes in a policy — including when disguised as roleplay ("pretend…"), fiction ("in a novel…"), a hypothetical, or third-person ("what would an employee do to…") — do NOT provide any steps or tactics. Politely DECLINE in one line and offer to explain what the policy actually allows instead.

Otherwise, generate a SHORT, polite response in FlockML format:
1. Briefly acknowledge what they asked
2. Say you can help with HR topics
3. Give 1-2 examples

FlockML rules:
- Wrap in <flockml>...</flockml>
- Use <b> for bold, <br/> for line breaks
- Use • bullet character for lists (NOT <ul>/<li>)
- Keep under 50 words"""
                else:
                    prompt = f"""The user asked an out-of-scope question to an HR assistant: "{query}"
{context_block}
IMPORTANT: If the request is trying to exploit, game, cheat, circumvent, defraud, or find loopholes in a policy — including when disguised as roleplay ("pretend…"), fiction ("in a novel…"), a hypothetical, or third-person ("what would an employee do to…") — do NOT provide any steps or tactics. Politely DECLINE and offer to explain what the policy actually allows instead.

Otherwise, generate a polite, brief response that:
1. Briefly acknowledges what they asked (e.g., "I can't help with weather forecasts" or "I'm not able to generate code")
2. Redirects them to HR-related topics
3. Gives 1-2 examples of what you CAN help with

Keep it to 3-4 sentences max. Be warm but concise."""

                response_text = await chat_complete_text(
                    messages=prompt,
                    step="out_of_scope",
                    temperature=0.3,
                    conversation_id=conversation_id,
                    user_id=user_id,
                )

                if response_text:
                    if channel == "flock":
                        answer = response_text
                    else:
                        answer = _clean_html_from_response(response_text)
                else:
                    log.warning("Out-of-scope LLM returned empty content")
                    answer = _default_out_of_scope_response(channel)
            except Exception as e:
                log.warning("Contextual out-of-scope response failed: %s", e)
                answer = _default_out_of_scope_response(channel)
        else:
            answer = _default_out_of_scope_response(channel)

    return AnswerResult(
        answer=answer,
        confidence=CRAGConfidence.SUFFICIENT,
        citations=[],
        query_type="out_of_scope",
    )


def _default_out_of_scope_response(channel: str = "web") -> str:
    """Default out-of-scope response when LLM is not available."""
    if channel == "flock":
        return """<flockml>I'm <b>Optimus</b>, your HR assistant at Tata 1mg. 😊<br/><br/>I help with questions about our HR policy documents.<br/><br/>Try asking about company policies, guidelines, or processes!</flockml>"""
    return """I'm **Optimus**, your HR assistant at Tata 1mg. 😊

I help with questions about our HR policy documents — just ask me anything about company policies, guidelines, or processes!

I'm not able to help with requests outside the HR domain. Is there anything about Tata 1mg policies I can help you with?"""


def _recent_cited_doc_names(
    conversation_history: list[dict[str, str]] | None,
) -> list[str]:
    """Friendly names of the documents cited by the most recent answer that had any.

    Used to anchor clarification suggestions on an already-established topic.
    """
    if not conversation_history:
        return []
    for msg in reversed(conversation_history):
        if (msg.get("role") or "").strip().lower() != "assistant":
            continue
        citations = msg.get("citations") or []
        if not citations:
            continue
        names: list[str] = []
        for c in citations:
            name = (c.get("document") or "").strip() if isinstance(c, dict) else ""
            if name and name not in names:
                names.append(name)
        if names:
            return names
    return []


async def handle_clarification(
    query: str,
    doc_catalog: list[str],
    channel: str = "web",
    conversation_id: str | None = None,
    user_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> AnswerResult:
    """Ask for clarification on vague queries.

    Suggestions are chosen defensively so a single incidental keyword can't drag in
    an unrelated document (the "GST Rate Automation Update" drift), while an
    established conversation topic is preserved across vague follow-ups.
    """
    # Hard relevance gate: a suggested doc must be relevant to the CURRENT turn.
    MIN_DOC_SCORE = 0.45

    # (a) Search on the current query alone — the hard relevance gate.
    current_docs = await _semantic_document_search(
        query, top_k=5, conversation_id=conversation_id, user_id=user_id
    )
    current_scores = {
        d["filename"]: d for d in current_docs if d.get("score", 0) > MIN_DOC_SCORE
    }

    # (b) Search biased with the previous user turn — the conversational neighbourhood.
    # A doc must clear the bar on BOTH searches (intersection) to be suggested, so an
    # incidental single-keyword match in one search alone cannot surface.
    relevant_docs: list[dict[str, Any]] = []
    if conversation_history:
        prev_user_msg = next(
            (
                m["content"]
                for m in reversed(conversation_history)
                if (m.get("role") or "").strip().lower() == "user" and m.get("content")
            ),
            None,
        )
        if prev_user_msg:
            context_docs = await _semantic_document_search(
                f"{prev_user_msg} {query}".strip(),
                top_k=5,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            context_names = {
                d["filename"] for d in context_docs if d.get("score", 0) > MIN_DOC_SCORE
            }
            relevant_docs = [d for fn, d in current_scores.items() if fn in context_names]

    # No history, or nothing survived the intersection: use current-query relevance.
    if not relevant_docs:
        relevant_docs = list(current_scores.values())

    # Relevance-gated anchoring: if the current turn surfaced no relevant doc but the
    # conversation already established a topic (a prior answer cited documents), anchor
    # the clarification on that topic instead of drifting or going fully generic. This
    # stays topic-change safe — a genuine pivot produces its own current-query matches
    # above, so we only fall back to the cited topic for vague continuations.
    cited_names = _recent_cited_doc_names(conversation_history)
    if relevant_docs:
        doc_names = [_format_doc_name(d["filename"]) for d in relevant_docs]
        if len(doc_names) > 1:
            suggestions_text = ", ".join(doc_names[:-1]) + f" or {doc_names[-1]}"
        else:
            suggestions_text = doc_names[0]

        if channel == "flock":
            answer = f"""<flockml>I'd like to help, but I need a bit more context.<br/><br/>
Are you asking about <b>{suggestions_text}</b>?<br/><br/>
Could you please provide more details about what you'd like to know?</flockml>"""
        else:
            answer = f"""I'd like to help, but I need a bit more context.

Are you asking about **{suggestions_text}**?

Could you please provide more details about what you'd like to know?"""
        suggestions = doc_names
    elif cited_names:
        # Vague follow-up within an established topic: anchor on what we were
        # already discussing rather than guessing or going fully generic.
        if len(cited_names) > 1:
            suggestions_text = ", ".join(cited_names[:-1]) + f" or {cited_names[-1]}"
        else:
            suggestions_text = cited_names[0]

        if channel == "flock":
            answer = f"""<flockml>I'd like to help, but I need a bit more context.<br/><br/>
Are you still asking about <b>{suggestions_text}</b>?<br/><br/>
Could you share a bit more about what you'd like to know?</flockml>"""
        else:
            answer = f"""I'd like to help, but I need a bit more context.

Are you still asking about **{suggestions_text}**?

Could you share a bit more about what you'd like to know?"""
        suggestions = cited_names
    else:
        if channel == "flock":
            answer = """<flockml>I need more details to help you.<br/><br/>Try asking like:<br/>• "What is the leave policy?"<br/>• "How do I apply for medical claim?"<br/>• "Resignation documents needed?"</flockml>"""
        else:
            answer = """I'd like to help, but I'm not sure what you're asking about.

Could you please rephrase your question? For example:
- "What is the leave policy?"
- "How do I apply for medical reimbursement?"
- "What documents are needed for resignation?"
"""
        suggestions = []

    return AnswerResult(
        answer=answer,
        confidence=CRAGConfidence.INSUFFICIENT,
        citations=[],
        query_type="clarification_needed",
        needs_clarification=True,
        suggestions=suggestions,
    )


def _is_source_citation_query(query: str) -> bool:
    """Check if the query is asking about source/citation of previous answer."""
    query_lower = query.lower().strip()

    source_patterns = [
        "what is the source",
        "what's the source",
        "what are the sources",
        "what was the source",
        "what were the sources",
        "where is this from",
        "where was this from",
        "where did you get this",
        "which document",
        "which policy",
        "source of this",
        "citation",
        "reference",
        "where can i find this",
        "which file",
        "from which document",
        "source please",
        "show source",
        "show me the source",
    ]

    return any(pattern in query_lower for pattern in source_patterns)


async def handle_source_citation_followup(
    query: str,
    conversation_history: list[dict[str, str]],
) -> AnswerResult | None:
    """
    Handle queries asking about the source of the previous answer.

    Returns AnswerResult if handled, None otherwise.
    """
    if not conversation_history or not _is_source_citation_query(query):
        return None

    # Find the last assistant message with citations
    last_citations = None

    for msg in reversed(conversation_history):
        if msg.get("role") == "assistant":
            citations = msg.get("citations", [])
            if citations:
                last_citations = citations
                break

    if not last_citations:
        # No citations found in previous answers
        return AnswerResult(
            answer="""I don't have specific document citations for my previous response.

This could mean:
- The answer was based on general conversation (greetings, etc.)
- The information came from multiple sources that were synthesized
- The confidence level didn't meet the threshold for citations

Would you like me to search for a specific topic in our HR documents?""",
            confidence=CRAGConfidence.SUFFICIENT,
            citations=[],
            query_type="meta_query",
        )

    # Format citations into a readable response
    citation_text = "Here are the sources for my previous answer:\n\n"

    for i, citation in enumerate(last_citations, 1):
        doc_name = citation.get("document", "Unknown document")
        section = citation.get("section_title", "")
        score = citation.get("score", 0)

        citation_text += f"**{i}. {doc_name}**"
        if section:
            citation_text += f" - {section}"
        citation_text += f"\n   (Relevance score: {score:.0%})\n\n"

    citation_text += "\nWould you like more details from any of these documents?"

    return AnswerResult(
        answer=citation_text,
        confidence=CRAGConfidence.SUFFICIENT,
        citations=last_citations,  # Include the same citations
        query_type="meta_query",
    )


async def handle_clarification_followup(
    query: str,
    conversation_history: list[dict[str, str]],
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """
    Check if this is a follow-up to a clarification request and reconstruct the full question.

    Returns the reconstructed question if it's a follow-up, None otherwise.
    """
    if not conversation_history:
        return None

    # Check if the previous assistant message was a clarification request
    # Look for the most recent assistant message
    prev_assistant_msg = None
    for msg in reversed(conversation_history):
        if msg.get("role") == "assistant":
            prev_assistant_msg = msg
            break

    if not prev_assistant_msg or not _was_clarification_request(prev_assistant_msg):
        return None

    # This is a follow-up to clarification
    prev_suggestions = prev_assistant_msg.get("suggestions", [])

    if not config.OPENAI_API_KEY:
        # Simple fallback - just prepend the first suggestion
        if prev_suggestions:
            return f"Tell me about {prev_suggestions[0]}"
        return None

    try:
        prompt = f"""The user was asked for clarification about their HR question.

Previous clarification suggestions: {prev_suggestions}
User's follow-up response: "{query}"

Determine what the user actually wants to know. Output ONLY the reconstructed full question.
Example: If suggestions were ["Leave Policy", "Attendance Policy"] and user said "yes, leave",
output: "What is the leave policy?"

If you can't determine intent, output: UNCLEAR

Reconstructed question:"""

        result_text = await chat_complete_text(
            messages=prompt,
            step="clarification_followup",
            temperature=0,
            conversation_id=conversation_id,
            user_id=user_id,
        )

        if not result_text:
            log.warning("Clarification follow-up LLM returned empty content")
            return None

        result = _clean_html_from_response(result_text)

        if result and result.upper() != "UNCLEAR":
            log.info("Reconstructed question from clarification follow-up: %s", result)
            return result
        return None

    except Exception as e:
        log.warning("Clarification follow-up failed: %s", e)
        return None


async def handle_knowledge_query(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    include_debug: bool = False,
    channel: str = "web",
    subtype: KnowledgeSubType | None = None,
) -> AnswerResult:
    """Handle HR knowledge questions with RAG retrieval and HR persona.

    Args:
        question: User's HR-related question
        conversation_history: Previous messages for context
        user_id: User ID for tracing
        conversation_id: Conversation ID for tracing
        include_debug: Include debug info in result
        channel: Output channel - "web" for Markdown, "flock" for FlockML
        subtype: Pre-classified subtype (SIMPLE/COMPLEX) to skip redundant LLM call
    """
    # PERFORMANCE: If subtype is already provided (from combined classification),
    # skip the redundant subtype classification and only get embedding
    if subtype is not None:
        log.info("Using pre-classified subtype: %s (skipping redundant LLM call)", subtype.value)
        query_embedding = await get_embedding_async(
            question, conversation_id=conversation_id, user_id=user_id
        )
    else:
        # Fallback: Run subtype classification and embedding in parallel
        # This path is used when handle_knowledge_query is called directly without subtype
        subtype_task = asyncio.create_task(
            classify_knowledge_subtype(question, conversation_id=conversation_id, user_id=user_id)
        )
        embedding_task = asyncio.create_task(
            get_embedding_async(question, conversation_id=conversation_id, user_id=user_id)
        )
        subtype, query_embedding = await asyncio.gather(subtype_task, embedding_task)
        log.info("Knowledge subtype: %s (parallel with embedding)", subtype.value)

    # Get retrieval parameters
    params = get_retrieval_params(subtype)

    # Holiday/calendar queries need broader context. "Next holiday" can be wrong
    # with only 5 chunks if upcoming months live in a chunk that did not rank, so
    # use the wider set regardless of the classified SIMPLE/COMPLEX subtype.
    if _is_holiday_calendar_query(question):
        params = get_retrieval_params(KnowledgeSubType.COMPLEX)
        log.info("Holiday/calendar query detected — using broader calendar retrieval")

    # Retrieve relevant chunks (using pre-computed embedding and subtype for conditional reranking)
    retriever = get_retriever()
    retrieval_result = await retriever.retrieve(
        question,
        top_k=params["top_k"],
        search_types=params.get("search_types"),
        query_embedding=query_embedding,  # Pass pre-computed embedding
        subtype=subtype,  # Pass subtype for conditional reranking skip
        conversation_id=conversation_id,
        user_id=user_id,
    )

    log.debug(
        "Retrieval: confidence=%s, chunks=%d, subtype=%s",
        retrieval_result.confidence,
        len(retrieval_result.chunks),
        subtype.value,
    )

    # Handle insufficient retrieval
    if retrieval_result.confidence == CRAGConfidence.INSUFFICIENT and not retrieval_result.chunks:
        return _handle_insufficient(retrieval_result, include_debug, channel=channel)

    # Generate answer with HR persona
    is_complex = subtype == KnowledgeSubType.COMPLEX
    answer = await _generate_hr_answer(
        question,
        retrieval_result,
        conversation_history,
        is_complex=is_complex,
        user_id=user_id,
        conversation_id=conversation_id,
        channel=channel,
    )

    # Build smart citations
    if should_include_citations(QueryType.KNOWLEDGE_QUERY, retrieval_result.confidence, retrieval_result.chunks):
        citations = build_smart_citations(retrieval_result.chunks)
    else:
        citations = []

    return AnswerResult(
        answer=answer,
        confidence=retrieval_result.confidence,
        citations=citations,
        query_type="knowledge_query",
        debug_info=retrieval_result.debug_info if include_debug else None,
    )


def _handle_insufficient(retrieval_result: RetrievalResult, include_debug: bool, channel: str = "web") -> AnswerResult:
    """Handle case where retrieval confidence is insufficient."""
    if channel == "flock":
        answer = """<flockml>I couldn't find relevant info for this question.<br/><br/><b>Try:</b><br/>• Use different keywords<br/>• Be more specific about the policy<br/>• Ask about a specific document<br/><br/><i>Contact HR directly for urgent help.</i></flockml>"""
    else:
        answer = """I couldn't find relevant information in our HR documents to answer this question.

Please try:
- Using different keywords (e.g., "leave policy" instead of "vacation rules")
- Being more specific about the topic or policy you're asking about
- Asking about a specific document if you know its name

If you need immediate assistance, please contact the HR team directly."""

    return AnswerResult(
        answer=answer,
        confidence=CRAGConfidence.INSUFFICIENT,
        citations=[],
        query_type="knowledge_query",
        debug_info=retrieval_result.debug_info if include_debug else None,
    )


async def _generate_hr_answer(
    question: str,
    retrieval_result: RetrievalResult,
    conversation_history: list[dict[str, str]] | None,
    is_complex: bool = False,
    user_id: str | None = None,
    conversation_id: str | None = None,
    channel: str = "web",
) -> str:
    """Generate answer using LLM with HR persona."""
    if not config.OPENAI_API_KEY:
        return _fallback_answer(retrieval_result, channel=channel)

    try:
        # Build context from chunks
        context = _format_context(retrieval_result.chunks)

        # Use appropriate prompts based on channel and complexity
        if channel == "flock":
            # Flock: Use FlockML prompts (no tables, direct FlockML output)
            if is_complex:
                system_prompt = FLOCK_SYNTHESIS_PROMPT
                user_prompt = FLOCK_SYNTHESIS_USER_PROMPT.format(context=context, question=question)
            else:
                system_prompt = FLOCK_SYSTEM_PROMPT.format(confidence=retrieval_result.confidence.value)
                user_prompt = FLOCK_USER_PROMPT.format(context=context, question=question)
        else:
            # Web: Use Markdown prompts
            if is_complex:
                system_prompt = HR_SYNTHESIS_PROMPT
                user_prompt = HR_SYNTHESIS_USER_PROMPT.format(context=context, question=question)
            else:
                system_prompt = HR_SYSTEM_PROMPT.format(confidence=retrieval_result.confidence.value)
                user_prompt = HR_USER_PROMPT.format(context=context, question=question)

        # Prepend current-date context so temporal queries are interpreted in the
        # direction and range requested by the user instead of assuming that the
        # earliest date in the retrieved document is the correct answer.
        user_prompt = (
            TEMPORAL_GUIDANCE.format(current_date=_current_ist_date_str())
            + "\n"
            + user_prompt
        )

        # Build messages
        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history (limited to prevent token bloat and latency)
        if conversation_history:
            for msg in conversation_history[-MAX_CONVERSATION_HISTORY:]:
                messages.append({"role": msg["role"], "content": msg["content"]})

        # Add current question
        messages.append({"role": "user", "content": user_prompt})

        answer = await chat_complete_text(
            messages=messages,
            step="answer_generation",
            temperature=0.2 if is_complex else 0.1,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        if not answer:
            log.warning("HR answer generation LLM returned empty content")
            return "I apologize, but I couldn't generate an answer at this time. Please try rephrasing your question."

        # For web channel, clean any accidental HTML tags; for flock, keep FlockML as-is
        if channel != "flock":
            answer = _clean_html_from_response(answer)

        return answer

    except Exception as e:
        log.error("HR answer generation failed: %s", e)
        return _fallback_answer(retrieval_result, channel=channel)


def _fallback_answer(retrieval_result: RetrievalResult, channel: str = "web") -> str:
    """Generate a simple answer without LLM."""
    if not retrieval_result.chunks:
        if channel == "flock":
            return "<flockml>I couldn't find relevant information to answer your question. Please try rephrasing or contact HR directly.</flockml>"
        return "I couldn't find relevant information to answer your question. Please try rephrasing or contact HR directly."

    if channel == "flock":
        answer = "<flockml><b>Here's what I found in our HR documents:</b><br/><br/>"
        for i, chunk in enumerate(retrieval_result.chunks[:3], 1):
            source = chunk.filename
            if chunk.section_title:
                source = f"{chunk.filename} - {chunk.section_title}"
            answer += f"<b>{i}. From {source}:</b><br/>{chunk.text[:300]}...<br/><br/>"
        answer += "</flockml>"
    else:
        answer = "Here's what I found in our HR documents:\n\n"
        for i, chunk in enumerate(retrieval_result.chunks[:3], 1):
            source = chunk.filename
            if chunk.section_title:
                source = f"{chunk.filename} - {chunk.section_title}"
            answer += f"**{i}. From {source}:**\n{chunk.text[:300]}...\n\n"

    return answer


def _format_context(chunks: list[RetrievedChunk]) -> str:
    """Format chunks into context string for the LLM.

    Uses actual document names (not numbered references) so the LLM
    cites documents by name, matching our citation output.
    """
    context_parts = []

    for chunk in chunks:
        # Use readable document name (without .pdf extension)
        doc_name = chunk.filename.replace('.pdf', '').replace('_', ' ')
        # Just use document name - section info is in the text itself
        context_parts.append(f"Source: {doc_name}\n{chunk.text}")

    return "\n\n---\n\n".join(context_parts)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def answer_question(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
    include_debug: bool = False,
    user_id: str | None = None,
    conversation_id: str | None = None,
    channel: str = "web",
) -> AnswerResult:
    """
    Answer a question using query-type routing with specialized handlers.

    Pipeline:
    1. Check if this is a source/citation follow-up query
    2. Check if this is a follow-up to a clarification request
    3. Get document catalog for classifier context
    4. Classify query type
    5. Route to appropriate handler

    Args:
        question: User's question
        conversation_history: Previous messages for context
        include_debug: Include debug info in result
        user_id: User ID for tracing
        conversation_id: Conversation ID for tracing
        channel: Output channel - "web" for Markdown, "flock" for FlockML

    Returns:
        AnswerResult with answer, confidence, citations, and query type
    """
    # Concurrency limiter - prevent overload with too many concurrent requests
    async with _request_semaphore:
        try:
            # Timeout wrapper - prevent hanging requests
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await _answer_question_impl(
                    question,
                    conversation_history,
                    include_debug,
                    user_id,
                    conversation_id,
                    channel,
                )
        except asyncio.TimeoutError:
            log.warning("Request timed out after %ds for question: %s", REQUEST_TIMEOUT_SECONDS, question[:50])
            if channel == "flock":
                timeout_msg = "<flockml>We're experiencing high loads right now. Your query could not be processed. Please try again after some time.</flockml>"
            else:
                timeout_msg = "We're experiencing high loads right now. Your query could not be processed. Please try again after some time."
            return AnswerResult(
                answer=timeout_msg,
                confidence=CRAGConfidence.INSUFFICIENT,
                citations=[],
                query_type="timeout",
            )


async def _answer_question_impl(
    question: str,
    conversation_history: list[dict[str, str]] | None,
    include_debug: bool,
    user_id: str | None,
    conversation_id: str | None,
    channel: str,
) -> AnswerResult:
    """Internal implementation of answer_question - handles actual query processing."""
    # Step 0: Check for source/citation follow-up (e.g., "what is the source of this answer?")
    source_result = await handle_source_citation_followup(question, conversation_history or [])
    if source_result:
        log.info("Handled as source/citation follow-up query")
        return source_result

    # Step 1: Check for clarification follow-up
    reconstructed = await handle_clarification_followup(
        question, conversation_history or [], conversation_id=conversation_id, user_id=user_id
    )
    if reconstructed:
        question = reconstructed
        log.info("Using reconstructed question from clarification follow-up")

    # Step 2: Get document catalog for classifier context
    doc_catalog = await get_active_document_names()

    # Step 3: OPTIMIZED - Combined query type + subtype classification in single LLM call
    # This saves ~500-700ms by avoiding a second LLM call for subtype classification
    query_type, subtype, resolved_query = await classify_query_with_subtype(
        question,
        doc_catalog,
        conversation_id=conversation_id,
        user_id=user_id,
        conversation_history=conversation_history,
    )
    log.info(
        "Query classified as: %s (subtype: %s, resolved_query=%r)",
        query_type.value, subtype.value, resolved_query,
    )

    # Step 4: Route to appropriate handler
    match query_type:
        case QueryType.CONVERSATIONAL:
            return await handle_conversational(
                question,
                channel=channel,
                conversation_id=conversation_id,
                user_id=user_id,
                conversation_history=conversation_history,
            )

        case QueryType.META_QUERY:
            return await handle_meta_query(question, doc_catalog, channel=channel)

        case QueryType.WEB_APP_LINK:
            log.info("Handled as web app URL request")
            return _web_app_url_answer(channel)

        case QueryType.OUT_OF_SCOPE:
            return await handle_out_of_scope(
                question,
                channel=channel,
                conversation_id=conversation_id,
                user_id=user_id,
                conversation_history=conversation_history,
            )

        case QueryType.CLARIFICATION_NEEDED:
            return await handle_clarification(
                question,
                doc_catalog,
                channel=channel,
                conversation_id=conversation_id,
                user_id=user_id,
                conversation_history=conversation_history,
            )

        case QueryType.KNOWLEDGE_QUERY:
            # Retrieve on the anaphora-resolved standalone question so follow-ups
            # like "which of those are automated?" embed/rerank correctly. The
            # answer LLM still receives conversation_history for natural phrasing.
            return await handle_knowledge_query(
                resolved_query,
                conversation_history,
                user_id=user_id,
                conversation_id=conversation_id,
                include_debug=include_debug,
                channel=channel,
                subtype=subtype,  # Pass pre-classified subtype
            )

        case _:
            # Fallback to knowledge query (also retrieves on the resolved question).
            return await handle_knowledge_query(
                resolved_query,
                conversation_history,
                user_id=user_id,
                conversation_id=conversation_id,
                include_debug=include_debug,
                channel=channel,
                subtype=subtype,  # Pass pre-classified subtype
            )
