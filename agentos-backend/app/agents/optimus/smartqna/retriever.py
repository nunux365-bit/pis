"""Adaptive retrieval with CRAG (Corrective RAG) pipeline and reranking.

PERFORMANCE: Uses async OpenAI client for non-blocking embeddings and reranking.
Includes Redis caching for embeddings and query expansion to reduce API latency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import Distance, FieldCondition, Filter, MatchValue, VectorParams

from app.agents.optimus import config
from app.agents.optimus.smartqna.async_bridge import run_sync
from app.agents.optimus.smartqna.openai_client import get_async_openai_client
from app.agents.optimus.smartqna.query_classifier import QueryType, KnowledgeSubType
from app.config.settings import settings

log = logging.getLogger(__name__)


class CRAGConfidence(str, Enum):
    """CRAG confidence levels based on reranker scores."""
    SUFFICIENT = "SUFFICIENT"   # High confidence, answer directly
    PARTIAL = "PARTIAL"         # Medium confidence, answer with caveats
    INSUFFICIENT = "INSUFFICIENT"  # Low confidence, decline gracefully


@dataclass
class RetrievedChunk:
    """A retrieved chunk with metadata and scores."""
    id: str
    text: str
    doc_id: str
    filename: str
    chunk_type: str  # chunk, section_summary, document_summary
    vector_score: float = 0.0
    rerank_score: float = 0.0
    section_title: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    """Result of retrieval operation with CRAG confidence."""
    chunks: list[RetrievedChunk]
    confidence: CRAGConfidence
    query_type: QueryType
    debug_info: dict = field(default_factory=dict)


# Singleton for async client (lazy-loaded)
_async_qdrant_client: AsyncQdrantClient | None = None


def get_embedding(
    text: str,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> list[float]:
    """Sync wrapper for scripts/tests — delegates to :func:`get_embedding_async`."""
    return run_sync(get_embedding_async(text, conversation_id=conversation_id, user_id=user_id))


async def expand_query_for_retrieval(
    query: str,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Use LLM to expand query with synonyms and related HR terms for better retrieval.

    This helps bridge the semantic gap between user phrasing and document content.
    E.g., "remote work policy" → "remote work policy, work from home, WFH, telecommuting guidelines"
    """
    client = get_async_openai_client()

    prompt = f"""You are helping improve search retrieval for an HR knowledge base.
Given a user query, expand it to include synonyms and related terms that might appear in HR policy documents.

Rules:
1. Keep the original query intent
2. Add 3-5 synonyms or related HR terms
3. Output ONLY the expanded query, nothing else
4. Keep it under 100 words

User query: "{query}"

Expanded query:"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=150,
        )

        # Log token usage (fire-and-forget)
        from app.agents.optimus.smartqna.usage_logger import (
            extract_usage_from_openai_response,
            schedule_usage_log,
        )
        usage = extract_usage_from_openai_response(response)
        schedule_usage_log(
            step="query_expansion",
            model="gpt-4o-mini",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            conversation_id=conversation_id,
            user_id=user_id,
        )

        expanded = response.choices[0].message.content.strip()
        log.debug("Query expansion: '%s' → '%s'", query, expanded[:100])
        return expanded
    except Exception as e:
        log.warning("Query expansion failed: %s, using original query", e)
        return query


async def expand_query_for_retrieval_cached(
    query: str,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Expand query with LLM using Redis caching to reduce API calls.

    PERFORMANCE OPTIMIZATION (staging latency fix):
    - Caches query expansions in Redis with configurable TTL (default 30 minutes)
    - Cache hit skips LLM call entirely (~200-300ms saved)
    - Cache key is MD5 hash of normalized query

    Args:
        query: User query to expand
        conversation_id: Conversation ID for usage logging
        user_id: User ID for usage logging

    Returns:
        Expanded query (cached or freshly generated)
    """
    from app.infra.redis_client import get_redis

    # Normalize query for consistent cache keys
    normalized = query.strip().lower()
    cache_key = f"optimus:qexp:{hashlib.md5(normalized.encode()).hexdigest()}"

    try:
        redis = get_redis()
        cached = await redis.get(cache_key)
        if cached:
            expanded = cached.decode() if isinstance(cached, bytes) else cached
            log.debug("Query expansion cache HIT for key %s", cache_key[:40])
            return expanded
    except Exception as e:
        log.warning("Redis cache read failed for query expansion: %s", e)

    # Cache miss - expand via LLM
    expanded = await expand_query_for_retrieval(query, conversation_id=conversation_id, user_id=user_id)

    # Store in cache
    try:
        redis = get_redis()
        await redis.set(cache_key, expanded, ex=config.QUERY_EXPANSION_CACHE_TTL_SECONDS)
        log.debug("Query expansion cache SET for key %s (TTL=%ds)", cache_key[:40], config.QUERY_EXPANSION_CACHE_TTL_SECONDS)
    except Exception as e:
        log.warning("Redis cache write failed for query expansion: %s", e)

    return expanded


async def get_embedding_async(
    text: str,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> list[float]:
    """Generate embedding for a single text using OpenAI API (ASYNC - non-blocking)."""
    client = get_async_openai_client()
    response = await client.embeddings.create(
        model=config.EMBEDDING_MODEL,
        input=text,
    )

    # Log token usage (fire-and-forget)
    from app.agents.optimus.smartqna.usage_logger import (
        extract_usage_from_embedding_response,
        schedule_usage_log,
    )
    usage = extract_usage_from_embedding_response(response)
    schedule_usage_log(
        step="embedding_async",
        model=config.EMBEDDING_MODEL,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        conversation_id=conversation_id,
        user_id=user_id,
    )

    return response.data[0].embedding


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Sync wrapper for scripts/tests — delegates to :func:`get_embeddings_batch_async`."""
    return run_sync(get_embeddings_batch_async(texts))


async def get_embeddings_batch_async(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for multiple texts using OpenAI API (ASYNC - non-blocking)."""
    if not texts:
        return []
    client = get_async_openai_client()
    response = await client.embeddings.create(
        model=config.EMBEDDING_MODEL,
        input=texts,
    )
    # Sort by index to maintain order
    sorted_data = sorted(response.data, key=lambda x: x.index)
    return [item.embedding for item in sorted_data]


def rerank_with_llm(
    query: str,
    chunks: list["RetrievedChunk"],
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> list[tuple["RetrievedChunk", float]]:
    """Sync wrapper for scripts/tests — delegates to :func:`rerank_with_llm_async`."""
    return run_sync(
        rerank_with_llm_async(
            query,
            chunks,
            conversation_id=conversation_id,
            user_id=user_id,
        )
    )


async def rerank_with_llm_async(
    query: str,
    chunks: list["RetrievedChunk"],
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> list[tuple["RetrievedChunk", float]]:
    """
    Rerank chunks using GPT-4o Mini as an LLM-based reranker (ASYNC - non-blocking).

    Returns list of (chunk, score) tuples sorted by relevance score descending.
    """
    if not chunks:
        return []

    client = get_async_openai_client()

    # Build the prompt for LLM reranking
    chunk_descriptions = []
    for i, chunk in enumerate(chunks):
        # Send full chunk text (up to 2000 chars) for accurate reranking
        # Don't truncate too aggressively - important info may be at the end
        text_preview = chunk.text[:2000] + "..." if len(chunk.text) > 2000 else chunk.text
        # Clean up whitespace for better readability
        text_preview = " ".join(text_preview.split())
        chunk_descriptions.append(f"[Passage {i}]\n{text_preview}")

    chunks_text = "\n\n".join(chunk_descriptions)

    prompt = f"""Score each passage's relevance to the query from 0.0 to 1.0.

QUERY: "{query}"

PASSAGES:
{chunks_text}

CRITICAL SCORING RULES:
1. MULTI-PART QUERIES: If the query asks about multiple topics (e.g., "leaves AND settlement"), score each passage for ANY topic it relates to
2. TOPIC MATCHING - score AT LEAST 0.5 if passage discusses ANY topic mentioned in query:
   - Query mentions "leaves" → ANY passage about leaves/PTO/absence scores 0.5+
   - Query mentions "resign/exit/settlement" → ANY passage about resignation/exit/F&F scores 0.5+
3. DIRECT ANSWERS score 0.8-1.0
4. ONLY score 0.0 if passage has NO connection to ANY query topic

BE GENEROUS: Include passages that provide context, not just direct answers.

EXAMPLE for query "What happens to leaves when I resign and when is F&F paid?":
- Passage about F&F payment → 0.9
- Passage about notice period → 0.7 (context for resignation)
- Passage about leave types → 0.6 (context for leaves question)
- Passage about leave balance → 0.6 (context for leaves question)
- Passage about handover process → 0.5 (context for exit)
- Passage about health insurance → 0.0 (unrelated)

OUTPUT: JSON only. Example: {{"0": 0.8, "1": 0.6, "2": 0.0}}

JSON:"""

    try:
        response = await client.chat.completions.create(
            model=config.RERANKER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,  # Fixed high value to ensure full JSON output
        )

        # Log token usage (fire-and-forget)
        from app.agents.optimus.smartqna.usage_logger import (
            extract_usage_from_openai_response,
            schedule_usage_log,
        )
        usage = extract_usage_from_openai_response(response)
        schedule_usage_log(
            step="reranking_async",
            model=config.RERANKER_MODEL,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            conversation_id=conversation_id,
            user_id=user_id,
        )

        # Parse the JSON response - with bounds check for empty/malformed responses
        if not response.choices or not response.choices[0].message:
            log.warning("LLM reranking returned empty choices, falling back to vector scores")
            return [(chunk, chunk.vector_score) for chunk in sorted(chunks, key=lambda c: c.vector_score, reverse=True)]

        response_text = response.choices[0].message.content
        if response_text is None:
            log.warning("LLM reranking returned None content, falling back to vector scores")
            return [(chunk, chunk.vector_score) for chunk in sorted(chunks, key=lambda c: c.vector_score, reverse=True)]

        response_text = response_text.strip()
        log.debug("Reranker raw response (first 500 chars): %s", response_text[:500])

        # Handle potential markdown code blocks
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()

        scores_dict = json.loads(response_text)
        log.debug("Parsed %d scores from reranker", len(scores_dict))

        # Create results with scores
        results = []
        for i, chunk in enumerate(chunks):
            score = float(scores_dict.get(str(i), 0.0))
            results.append((chunk, score))

        # Sort by score descending
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    except json.JSONDecodeError as e:
        log.warning("LLM reranking JSON parse failed: %s, response was: %s", e, response_text[:200] if response_text else "None")
        return [(chunk, chunk.vector_score) for chunk in sorted(chunks, key=lambda c: c.vector_score, reverse=True)]
    except Exception as e:
        log.warning("LLM reranking (async) failed: %s, falling back to vector scores", e)
        # Fallback: use vector scores as rerank scores
        return [(chunk, chunk.vector_score) for chunk in sorted(chunks, key=lambda c: c.vector_score, reverse=True)]


def get_async_qdrant_client() -> AsyncQdrantClient:
    """Get async Qdrant client singleton for non-blocking operations."""
    global _async_qdrant_client
    if _async_qdrant_client is None:
        _async_qdrant_client = AsyncQdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout_seconds)
    return _async_qdrant_client


async def close_async_qdrant_client() -> None:
    """Close SmartQnA Qdrant client singleton. Safe to call multiple times."""
    global _async_qdrant_client
    client = _async_qdrant_client
    _async_qdrant_client = None
    if client is not None:
        try:
            await client.close()
        except Exception:
            log.debug("SmartQnA Qdrant client close failed", exc_info=True)


async def ensure_collection_exists_async() -> None:
    """Ensure Qdrant collection exists with correct config (async)."""
    client = get_async_qdrant_client()
    collection_name = config.QDRANT_COLLECTION

    try:
        await client.get_collection(collection_name)
        log.debug("Collection %s already exists", collection_name)
        return
    except UnexpectedResponse as e:
        if e.status_code != 404:
            raise

    log.info("Creating Qdrant collection: %s (dim=%d)", collection_name, config.EMBEDDING_DIMENSIONS)
    await client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=config.EMBEDDING_DIMENSIONS,
            distance=Distance.COSINE,
        ),
    )

    for field_name in ("doc_id", "type", "filename"):
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema="keyword",
        )


def ensure_collection_exists() -> None:
    """Sync wrapper for scripts/tests — delegates to :func:`ensure_collection_exists_async`."""
    run_sync(ensure_collection_exists_async())


async def get_active_document_ids() -> set[str]:
    """Get IDs of all active documents from the database."""
    from sqlalchemy import select

    from app.db.models import OptimusDocument
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.scalars(
            select(OptimusDocument.id).where(
                OptimusDocument.is_active.is_(True),
                OptimusDocument.status == "ingested",
            )
        )
        return {str(doc_id) for doc_id in result.all()}


class AdaptiveRetriever:
    """
    Adaptive retrieval with CRAG pipeline.

    Features:
    - Query type classification for retrieval strategy
    - Multi-level search (chunk, section_summary, document_summary)
    - LLM-based reranking using GPT-4o Mini
    - CRAG confidence scoring with adaptive stopping
    - Hierarchical fallback
    - Filters by active documents only
    """

    def __init__(self):
        self.async_client = get_async_qdrant_client()
        self.collection = config.QDRANT_COLLECTION

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        search_types: list[str] | None = None,
        rerank_threshold: float | None = None,
        query_embedding: list[float] | None = None,
        subtype: KnowledgeSubType | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> RetrievalResult:
        """
        Retrieve relevant chunks for a query.

        Args:
            query: User query
            top_k: Number of results (default: 5)
            search_types: Types to search (default: ["chunk"])
            rerank_threshold: Minimum reranker score (default: config value)
            query_embedding: Pre-computed embedding (optional, for parallelization)
            subtype: Query subtype (SIMPLE/COMPLEX) for conditional reranking skip
            conversation_id: Conversation ID for usage logging
            user_id: User ID for usage logging

        Returns:
            RetrievalResult with chunks and CRAG confidence
        """
        # Use defaults if not provided
        # Search both chunks and section summaries for better recall
        params = {
            "top_k": top_k or 5,
            "search_types": search_types or ["chunk", "section_summary"],
            "rerank_threshold": rerank_threshold or config.CRAG_FILTER_MIN_SCORE,
        }

        # Query type is set to KNOWLEDGE_QUERY since this is only called for knowledge queries
        query_type = QueryType.KNOWLEDGE_QUERY
        # Default to SIMPLE if not provided
        subtype = subtype or KnowledgeSubType.SIMPLE

        log.debug("Retrieval params: %s, subtype: %s", params, subtype.value)

        # Get active document IDs (filter out inactive documents)
        active_doc_ids = await get_active_document_ids()
        log.info(
            "Active documents in DB: %d IDs=%s",
            len(active_doc_ids),
            list(active_doc_ids)[:5] if active_doc_ids else "[]",
        )
        if not active_doc_ids:
            log.warning("No active documents found - check optimus_documents table (is_active=True, status='ingested')")
            return RetrievalResult(
                chunks=[],
                confidence=CRAGConfidence.INSUFFICIENT,
                query_type=query_type,
                debug_info={"reason": "No active documents in knowledge base"},
            )

        # Embed query using OpenAI (async - non-blocking)
        # Use pre-computed embedding if provided (for parallelization)
        if query_embedding is None:
            # Expand query with LLM to include synonyms for better retrieval (cached)
            expanded_query = await expand_query_for_retrieval_cached(
                query, conversation_id=conversation_id, user_id=user_id
            )
            # Generate embedding
            query_embedding = await get_embedding_async(
                expanded_query, conversation_id=conversation_id, user_id=user_id
            )

        # Search across configured types (async - non-blocking)
        # Run searches in parallel if multiple types
        search_tasks = [
            self._vector_search(query_embedding, search_type, config.VECTOR_SEARCH_CANDIDATES, active_doc_ids)
            for search_type in params["search_types"]
        ]
        search_results = await asyncio.gather(*search_tasks)

        all_chunks = []
        for search_type, chunks in zip(params["search_types"], search_results):
            log.debug("Vector search type=%s found %d chunks", search_type, len(chunks))
            all_chunks.extend(chunks)

        log.info("Total chunks found: %d (from types: %s)", len(all_chunks), params["search_types"])
        if not all_chunks:
            return RetrievalResult(
                chunks=[],
                confidence=CRAGConfidence.INSUFFICIENT,
                query_type=query_type,
                debug_info={"reason": "No chunks found in vector search"},
            )

        # Deduplicate by ID and sort by vector score
        seen_ids = set()
        unique_chunks = []
        for chunk in all_chunks:
            if chunk.id not in seen_ids:
                seen_ids.add(chunk.id)
                unique_chunks.append(chunk)

        # Sort by vector score descending for skip-reranking check
        unique_chunks.sort(key=lambda c: c.vector_score, reverse=True)

        # OPTIMIZATION: Conditional reranking skip based on subtype and top vector score
        # - Simple queries: skip if top vector score > 0.85
        # - Complex queries: skip if top vector score > 0.90 (more conservative)
        skip_threshold = (
            config.RERANK_SKIP_THRESHOLD_SIMPLE
            if subtype == KnowledgeSubType.SIMPLE
            else config.RERANK_SKIP_THRESHOLD_COMPLEX
        )

        top_vector_score = unique_chunks[0].vector_score if unique_chunks else 0.0
        should_skip_reranking = top_vector_score > skip_threshold

        if should_skip_reranking:
            log.info(
                "SKIP RERANKING: top_vector_score=%.3f > threshold=%.2f (subtype=%s) - using vector scores directly",
                top_vector_score, skip_threshold, subtype.value
            )
            # Use vector scores as rerank scores (already sorted by vector_score desc)
            for chunk in unique_chunks:
                chunk.rerank_score = chunk.vector_score
            reranked = unique_chunks
        else:
            log.info(
                "RERANKING: top_vector_score=%.3f <= threshold=%.2f (subtype=%s) - performing LLM reranking",
                top_vector_score, skip_threshold, subtype.value
            )
            # Rerank (async - non-blocking)
            reranked = await self._rerank(
                query, unique_chunks, conversation_id=conversation_id, user_id=user_id
            )

        # Apply CRAG pipeline
        result = self._apply_crag(reranked, params["top_k"], params["rerank_threshold"], query_type)

        # Add skip info to debug
        result.debug_info["reranking_skipped"] = should_skip_reranking
        result.debug_info["top_vector_score"] = top_vector_score
        result.debug_info["skip_threshold"] = skip_threshold
        result.debug_info["subtype"] = subtype.value

        return result

    async def _vector_search(
        self, query_embedding: list[float], chunk_type: str, limit: int, active_doc_ids: set[str] | None = None
    ) -> list[RetrievedChunk]:
        """Search Qdrant for chunks of a specific type, filtered by active documents (async - non-blocking)."""
        try:
            # Use query_points API (qdrant-client >= 1.7)
            from qdrant_client.models import MatchAny

            # Build filter conditions
            filter_conditions = [FieldCondition(key="type", match=MatchValue(value=chunk_type))]

            # Add doc_id filter if we have active doc IDs
            if active_doc_ids:
                filter_conditions.append(
                    FieldCondition(key="doc_id", match=MatchAny(any=list(active_doc_ids)))
                )

            # Use async client for non-blocking search
            results = await self.async_client.query_points(
                collection_name=self.collection,
                query=query_embedding,
                query_filter=Filter(must=filter_conditions),
                limit=limit,
                with_payload=True,
            )

            chunks = []
            for hit in results.points:
                payload = hit.payload or {}
                chunks.append(
                    RetrievedChunk(
                        id=str(hit.id),
                        text=payload.get("text", ""),
                        doc_id=payload.get("doc_id", ""),
                        filename=payload.get("filename", ""),
                        chunk_type=chunk_type,
                        vector_score=hit.score,
                        section_title=payload.get("section_title"),
                        metadata=payload,
                    )
                )
            return chunks

        except Exception as e:
            log.warning("Vector search failed for type %s: %s", chunk_type, e)
            return []

    async def _rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Rerank chunks using LLM-based scoring with GPT-4o Mini (async - non-blocking)."""
        if not chunks:
            return []

        # Use LLM-based reranking (async)
        reranked_results = await rerank_with_llm_async(
            query, chunks, conversation_id=conversation_id, user_id=user_id
        )

        # Attach scores to chunks
        reranked_chunks = []
        for chunk, score in reranked_results:
            chunk.rerank_score = score
            reranked_chunks.append(chunk)

        return reranked_chunks

    def _apply_crag(
        self, chunks: list[RetrievedChunk], top_k: int, min_score: float, query_type: QueryType
    ) -> RetrievalResult:
        """
        Apply CRAG pipeline with confidence scoring and adaptive stopping.

        CRAG Logic:
        1. Filter by minimum score
        2. Check top score for confidence level
        3. Apply adaptive stopping (drop gap)
        4. Return top_k chunks with confidence
        """
        if not chunks:
            return RetrievalResult(
                chunks=[],
                confidence=CRAGConfidence.INSUFFICIENT,
                query_type=query_type,
                debug_info={"reason": "No chunks after reranking"},
            )

        top_score = chunks[0].rerank_score

        # Determine confidence
        if top_score >= config.CRAG_SUFFICIENT_THRESHOLD:
            confidence = CRAGConfidence.SUFFICIENT
        elif top_score >= config.CRAG_PARTIAL_THRESHOLD:
            confidence = CRAGConfidence.PARTIAL
        else:
            confidence = CRAGConfidence.INSUFFICIENT

        # Filter and apply adaptive stopping
        filtered = []
        prev_score = None

        for chunk in chunks:
            # Skip below minimum threshold (but only if we already have some results)
            if chunk.rerank_score < min_score and filtered:
                continue

            # Adaptive stopping: stop if score drops too much (but only if we have enough results)
            if prev_score is not None and len(filtered) >= 2:
                gap = prev_score - chunk.rerank_score
                if gap > config.CRAG_DROP_GAP:
                    log.debug("Adaptive stop: gap %.3f > %.3f", gap, config.CRAG_DROP_GAP)
                    break

            filtered.append(chunk)
            prev_score = chunk.rerank_score

            if len(filtered) >= top_k:
                break

        return RetrievalResult(
            chunks=filtered,
            confidence=confidence,
            query_type=query_type,
            debug_info={
                "top_score": top_score,
                "filtered_count": len(filtered),
                "total_candidates": len(chunks),
            },
        )


# Module-level retriever instance (lazy)
_retriever: AdaptiveRetriever | None = None


def get_retriever() -> AdaptiveRetriever:
    """Get the singleton retriever instance."""
    global _retriever
    if _retriever is None:
        _retriever = AdaptiveRetriever()
    return _retriever
