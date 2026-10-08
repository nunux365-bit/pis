"""Semantic query clarification for ambiguous or vague queries.

Uses embeddings and LLM to understand user intent, NOT keyword matching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from app.agents.optimus import config
from app.agents.optimus.smartqna.llm_client import chat_complete_text
from app.agents.optimus.smartqna.retriever import (
    RetrievalResult,
    CRAGConfidence,
    get_async_qdrant_client,
    get_embedding_async,
)
from app.agents.optimus.smartqna.query_classifier import QueryType

log = logging.getLogger(__name__)


@dataclass
class ClarificationResult:
    """Result of clarification check."""
    needs_clarification: bool
    suggestions: list[str]
    clarification_message: str | None = None
    related_documents: list[dict[str, Any]] | None = None


async def _semantic_document_search(
    query: str,
    top_k: int = 5,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Find documents semantically related to the query.

    This searches document summaries (not chunks) to find which
    documents might be relevant to a vague/unclear query.
    """
    try:
        client = get_async_qdrant_client()

        query_embedding = await get_embedding_async(
            query, conversation_id=conversation_id, user_id=user_id
        )

        results = await client.query_points(
            collection_name=config.QDRANT_COLLECTION,
            query=query_embedding,
            query_filter=Filter(
                must=[FieldCondition(key="type", match=MatchValue(value="document_summary"))]
            ),
            limit=top_k,
            with_payload=True,
        )

        documents = []
        seen = set()
        for hit in results.points:
            payload = hit.payload or {}
            filename = payload.get("filename", "")
            if filename and filename not in seen:
                seen.add(filename)
                documents.append({
                    "filename": filename,
                    "summary": payload.get("text", "")[:200],
                    "score": hit.score,
                    "doc_id": payload.get("doc_id", ""),
                })

        return documents
    except Exception as e:
        log.warning("Semantic document search failed: %s", e)
        return []


async def check_needs_clarification(
    query: str,
    retrieval_result: RetrievalResult,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> ClarificationResult:
    """
    Check if a query needs clarification using semantic understanding.

    Strategy:
    1. If query is REASONING type → never clarify, let LLM synthesize
    2. If retrieval found good matches → no clarification needed
    3. If retrieval failed but semantic doc search finds related docs → suggest them
    4. Use LLM to generate natural clarification with suggestions
    """
    # KNOWLEDGE_QUERY with COMPLEX subtype should not ask for clarification
    # These queries need multi-document synthesis, not disambiguation
    # Note: This function is now mostly called from agent.py for specific cases
    if hasattr(retrieval_result, 'query_type') and retrieval_result.query_type == QueryType.KNOWLEDGE_QUERY:
        log.debug("Skipping clarification check - handled by agent routing")
        return ClarificationResult(needs_clarification=False, suggestions=[])

    # If we have good confidence, no clarification needed
    if retrieval_result.confidence == CRAGConfidence.SUFFICIENT:
        return ClarificationResult(needs_clarification=False, suggestions=[])

    # If we have ANY chunks at all, let the agent answer (even with low confidence)
    # The confidence level affects answer generation, not whether to clarify
    # Only ask for clarification when retrieval truly found nothing relevant
    if retrieval_result.chunks:
        log.debug(
            "Skipping clarification: have %d chunks (confidence=%s)",
            len(retrieval_result.chunks),
            retrieval_result.confidence,
        )
        return ClarificationResult(needs_clarification=False, suggestions=[])

    # Low confidence or no chunks - try semantic document search
    related_docs = await _semantic_document_search(
        query, top_k=5, conversation_id=conversation_id, user_id=user_id
    )

    # Filter to docs with reasonable similarity (score > 0.3)
    relevant_docs = [d for d in related_docs if d.get("score", 0) > 0.3]

    if not relevant_docs:
        # No related documents found at all
        return ClarificationResult(
            needs_clarification=False,  # Let the agent handle "no info found"
            suggestions=[],
        )

    # We found potentially related documents - generate clarification
    clarification = await _generate_semantic_clarification(
        query=query,
        related_docs=relevant_docs,
        retrieval_chunks=retrieval_result.chunks,
        conversation_id=conversation_id,
        user_id=user_id,
    )

    if clarification:
        # Extract document names as suggestions
        suggestions = [_format_doc_name(d["filename"]) for d in relevant_docs[:4]]

        return ClarificationResult(
            needs_clarification=True,
            suggestions=suggestions,
            clarification_message=clarification,
            related_documents=relevant_docs,
        )

    return ClarificationResult(needs_clarification=False, suggestions=[])


async def _generate_semantic_clarification(
    query: str,
    related_docs: list[dict[str, Any]],
    retrieval_chunks: list | None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """
    Use LLM to generate a natural, contextual clarification message.

    The LLM understands:
    - Typos and misspellings naturally
    - Vague queries and their likely intent
    - How to map user language to document topics
    """
    if not config.OPENAI_API_KEY:
        return _fallback_clarification(query, related_docs)

    try:
        # Build document context
        doc_descriptions = []
        for i, doc in enumerate(related_docs[:4], 1):
            name = _format_doc_name(doc["filename"])
            summary = doc.get("summary", "")[:150]
            doc_descriptions.append(f"{i}. **{name}**: {summary}")

        docs_text = "\n".join(doc_descriptions)

        # Check if we found any chunks (even low scoring)
        found_something = bool(retrieval_chunks)

        prompt = f"""You are a helpful assistant that helps users find information in company policy documents.

The user asked: "{query}"

{"I found some content but it may not be exactly what you're looking for." if found_something else "I couldn't find a direct match for this query."}

However, I found these potentially related documents in our knowledge base:
{docs_text}

Generate a brief, helpful clarification response that:
1. Acknowledges what the user asked (naturally handle any typos/unclear terms)
2. Suggests which document(s) might contain what they're looking for
3. Asks them to clarify or confirm which topic they want to explore

Keep it conversational and helpful, 2-4 sentences max. Don't be overly apologetic.

Response:"""

        return await chat_complete_text(
            messages=prompt,
            step="clarification",
            temperature=0.3,
            conversation_id=conversation_id,
            user_id=user_id,
        )

    except Exception as e:
        log.warning("LLM clarification failed: %s", e)
        return _fallback_clarification(query, related_docs)


def _fallback_clarification(query: str, related_docs: list[dict[str, Any]]) -> str:
    """Generate a simple clarification without LLM."""
    if not related_docs:
        return None

    doc_names = [_format_doc_name(d["filename"]) for d in related_docs[:3]]
    docs_list = ", ".join(doc_names[:-1]) + f" or {doc_names[-1]}" if len(doc_names) > 1 else doc_names[0]

    return (
        f"I'm not sure exactly what you're looking for with \"{query}\". "
        f"I found some related documents: {docs_list}. "
        f"Could you clarify which topic you'd like to know about?"
    )


def _format_doc_name(filename: str) -> str:
    """Format document filename to a friendly readable name."""
    import re

    # Remove extension
    name = filename.replace(".pdf", "").replace(".docx", "")

    # Remove common prefixes
    name = re.sub(r"^POL_HR_", "", name)
    name = re.sub(r"^HR_", "", name)
    name = re.sub(r"^1MG_", "1mg ", name)

    # Remove version numbers
    name = re.sub(r"_Ver_?\d+\.?\d*[a-z]?", "", name)
    name = re.sub(r"_V\d+\.?\d*", "", name)
    name = re.sub(r"\s*\(\d+\)\s*", "", name)

    # Clean up underscores and extra spaces
    name = name.replace("_", " ").strip()
    name = re.sub(r"\s+", " ", name)

    return name


async def generate_smart_clarification(
    query: str,
    retrieval_result: RetrievalResult,
    available_docs: list[str] | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """
    Generate a smart clarification using semantic search and LLM.

    This is the main entry point - it combines semantic document search
    with LLM understanding to generate helpful clarifications.
    """
    result = await check_needs_clarification(
        query, retrieval_result, conversation_id=conversation_id, user_id=user_id
    )
    return result.clarification_message if result.needs_clarification else None
