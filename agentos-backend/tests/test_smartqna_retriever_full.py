"""
Comprehensive regression tests for smartqna/retriever.py.

Covers all code paths in:
  - get_embedding()                      – sync embedding
  - get_embedding_async()                – async embedding
  - get_embeddings_batch()               – empty + success
  - expand_query_for_retrieval()         – success, LLM failure
  - expand_query_for_retrieval_cached()  – cache hit, miss, redis error
  - rerank_with_llm()                    – sync: empty, success, JSON error, markdown wrap, exception
  - rerank_with_llm_async()             – async: same paths
  - AdaptiveRetriever._apply_crag()      – SUFFICIENT, PARTIAL, INSUFFICIENT,
                                           adaptive stopping, fallback
  - AdaptiveRetriever._vector_search()  – success, exception
  - AdaptiveRetriever._rerank()         – empty, success
  - AdaptiveRetriever.retrieve()        – no docs, no chunks, skip reranking,
                                           full pipeline with reranking
  - ensure_collection_exists()           – exists, 404 create, non-404 re-raise
  - get_active_document_ids()           – populated, empty
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.optimus.smartqna.retriever import (
    AdaptiveRetriever,
    CRAGConfidence,
    RetrievalResult,
    RetrievedChunk,
    QueryType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_chunk(
    *,
    id_: str | None = None,
    text: str = "chunk text content",
    doc_id: str = "doc-1",
    filename: str = "test.pdf",
    vector_score: float = 0.7,
    rerank_score: float = 0.7,
    chunk_type: str = "chunk",
    section_title: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        id=id_ or str(uuid.uuid4()),
        text=text,
        doc_id=doc_id,
        filename=filename,
        chunk_type=chunk_type,
        vector_score=vector_score,
        rerank_score=rerank_score,
        section_title=section_title,
        metadata={},
    )


def _make_openai_embedding_response(
    embedding: list[float] | None = None,
) -> MagicMock:
    embedding = embedding or [0.1] * 1536
    resp = MagicMock()
    item = MagicMock()
    item.embedding = embedding
    item.index = 0
    resp.data = [item]
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 0
    resp.usage.total_tokens = 10
    return resp


def _make_chat_response(content: str = '{"0": 0.9, "1": 0.5}') -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 50
    resp.usage.total_tokens = 150
    return resp


# ---------------------------------------------------------------------------
# get_embedding  (sync)
# ---------------------------------------------------------------------------

class TestGetEmbeddingSync:
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_embedding_response")
    def test_returns_embedding_vector(self, mock_extract, mock_schedule, mock_get_client):
        from app.agents.optimus.smartqna.retriever import get_embedding

        mock_extract.return_value = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=_make_openai_embedding_response())
        mock_get_client.return_value = mock_client

        result = get_embedding("test text")

        assert isinstance(result, list)
        assert len(result) == 1536

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_embedding_response")
    def test_calls_openai_with_configured_model(self, mock_extract, mock_schedule, mock_get_client):
        from app.agents.optimus.smartqna.retriever import get_embedding

        mock_extract.return_value = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=_make_openai_embedding_response())
        mock_get_client.return_value = mock_client

        get_embedding("hello world")

        mock_client.embeddings.create.assert_called_once()


# ---------------------------------------------------------------------------
# get_embedding_async
# ---------------------------------------------------------------------------

class TestGetEmbeddingAsync:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_embedding_response")
    async def test_returns_embedding_vector(self, mock_extract, mock_schedule, mock_get_client):
        from app.agents.optimus.smartqna.retriever import get_embedding_async

        mock_extract.return_value = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(
            return_value=_make_openai_embedding_response()
        )
        mock_get_client.return_value = mock_client

        result = await get_embedding_async("async text")

        assert isinstance(result, list)
        assert len(result) == 1536


# ---------------------------------------------------------------------------
# get_embeddings_batch
# ---------------------------------------------------------------------------

class TestGetEmbeddingsBatch:
    def test_empty_list_returns_empty(self):
        from app.agents.optimus.smartqna.retriever import get_embeddings_batch

        result = get_embeddings_batch([])
        assert result == []

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    def test_multiple_texts(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import get_embeddings_batch

        item0 = MagicMock()
        item0.embedding = [0.1] * 1536
        item0.index = 0
        item1 = MagicMock()
        item1.embedding = [0.2] * 1536
        item1.index = 1

        resp = MagicMock()
        resp.data = [item1, item0]  # Out of order — should be sorted by index

        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=resp)
        mock_get_client.return_value = mock_client

        result = get_embeddings_batch(["text1", "text2"])

        assert len(result) == 2
        assert result[0] == [0.1] * 1536  # index 0
        assert result[1] == [0.2] * 1536  # index 1


# ---------------------------------------------------------------------------
# expand_query_for_retrieval
# ---------------------------------------------------------------------------

class TestExpandQueryForRetrieval:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response")
    async def test_success_returns_expanded_query(
        self, mock_extract, mock_schedule, mock_get_client
    ):
        from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval

        mock_extract.return_value = {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response("leave policy work from home remote work")
        )
        mock_get_client.return_value = mock_client

        result = await expand_query_for_retrieval("leave policy")

        assert "leave" in result.lower()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    async def test_llm_exception_returns_original_query(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("LLM unavailable")
        )
        mock_get_client.return_value = mock_client

        result = await expand_query_for_retrieval("original query text")

        assert result == "original query text"


# ---------------------------------------------------------------------------
# expand_query_for_retrieval_cached
# ---------------------------------------------------------------------------

class TestExpandQueryCached:
    @pytest.mark.asyncio
    @patch("app.infra.redis_client.get_redis")
    async def test_cache_hit_returns_cached_value(self, mock_get_redis):
        from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval_cached

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"cached expanded query")
        mock_get_redis.return_value = mock_redis

        result = await expand_query_for_retrieval_cached("test query")

        assert result == "cached expanded query"

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval")
    @patch("app.infra.redis_client.get_redis")
    async def test_cache_miss_calls_expansion_and_stores(
        self, mock_get_redis, mock_expand
    ):
        from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval_cached

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # Cache miss
        mock_redis.set = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_expand.return_value = "freshly expanded query"

        result = await expand_query_for_retrieval_cached("test query")

        assert result == "freshly expanded query"
        mock_expand.assert_called_once()
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval")
    @patch("app.infra.redis_client.get_redis")
    async def test_redis_read_error_still_expands(self, mock_get_redis, mock_expand):
        from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval_cached

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_redis.set = AsyncMock(side_effect=Exception("Redis down"))
        mock_get_redis.return_value = mock_redis
        mock_expand.return_value = "expanded anyway"

        result = await expand_query_for_retrieval_cached("my query")

        assert result == "expanded anyway"


# ---------------------------------------------------------------------------
# rerank_with_llm  (sync)
# ---------------------------------------------------------------------------

class TestRerankWithLLMSync:
    def test_empty_chunks_returns_empty(self):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        result = rerank_with_llm("query", [])
        assert result == []

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response")
    def test_success_returns_sorted_scores(
        self, mock_extract, mock_schedule, mock_get_client
    ):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        mock_extract.return_value = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        chunks = [_make_chunk(id_="0"), _make_chunk(id_="1")]
        scores_json = '{"0": 0.3, "1": 0.9}'

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_chat_response(scores_json))
        mock_get_client.return_value = mock_client

        result = rerank_with_llm("query", chunks)

        assert len(result) == 2
        # Higher score first
        assert result[0][1] >= result[1][1]

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    def test_json_parse_error_falls_back_to_vector_scores(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        chunks = [
            _make_chunk(id_="0", vector_score=0.8),
            _make_chunk(id_="1", vector_score=0.6),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response("not valid json {{{")
        )
        mock_get_client.return_value = mock_client

        with patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log"), \
             patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response") as me:
            me.return_value = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            result = rerank_with_llm("query", chunks)

        # Falls back to vector scores
        assert len(result) == 2
        assert result[0][1] >= result[1][1]  # Sorted by vector score

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    def test_llm_exception_falls_back_to_vector_scores(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        chunks = [_make_chunk(id_="0", vector_score=0.5)]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
        mock_get_client.return_value = mock_client

        result = rerank_with_llm("query", chunks)

        assert len(result) == 1

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    def test_empty_llm_response_falls_back(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        chunks = [_make_chunk(id_="0", vector_score=0.7)]
        resp = MagicMock()
        resp.choices = []  # Empty choices
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 0
        resp.usage.total_tokens = 100

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=resp)
        mock_get_client.return_value = mock_client

        with patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log"), \
             patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response") as me:
            me.return_value = {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100}
            result = rerank_with_llm("query", chunks)

        assert len(result) == 1

    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    def test_markdown_code_block_stripped(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm

        chunks = [_make_chunk(id_="0")]
        scores_wrapped = '```json\n{"0": 0.8}\n```'

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_chat_response(scores_wrapped))
        mock_get_client.return_value = mock_client

        with patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log"), \
             patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response") as me:
            me.return_value = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            result = rerank_with_llm("query", chunks)

        assert len(result) == 1
        assert result[0][1] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# rerank_with_llm_async
# ---------------------------------------------------------------------------

class TestRerankWithLLMAsync:
    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty(self):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm_async

        result = await rerank_with_llm_async("query", [])
        assert result == []

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    @patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log")
    @patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response")
    async def test_success_sorted_by_score(
        self, mock_extract, mock_schedule, mock_get_client
    ):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm_async

        mock_extract.return_value = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        chunks = [
            _make_chunk(id_="0", vector_score=0.5),
            _make_chunk(id_="1", vector_score=0.4),
        ]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response('{"0": 0.4, "1": 0.9}')
        )
        mock_get_client.return_value = mock_client

        result = await rerank_with_llm_async("query", chunks)

        assert len(result) == 2
        assert result[0][1] >= result[1][1]  # Sorted descending

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    async def test_json_decode_error_fallback(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm_async

        chunks = [_make_chunk(id_="0", vector_score=0.7)]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response("INVALID JSON !!!")
        )
        mock_get_client.return_value = mock_client

        with patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log"), \
             patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response") as me:
            me.return_value = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            result = await rerank_with_llm_async("query", chunks)

        assert len(result) == 1  # Fallback to vector scores

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    async def test_exception_fallback(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm_async

        chunks = [_make_chunk(id_="0", vector_score=0.8)]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))
        mock_get_client.return_value = mock_client

        result = await rerank_with_llm_async("query", chunks)

        assert len(result) == 1
        assert result[0][1] == pytest.approx(0.8)

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_async_openai_client")
    async def test_none_response_content_fallback(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import rerank_with_llm_async

        chunks = [_make_chunk(id_="0", vector_score=0.6)]

        resp = _make_chat_response(None)
        resp.choices[0].message.content = None

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=resp)
        mock_get_client.return_value = mock_client

        with patch("app.agents.optimus.smartqna.usage_logger.schedule_usage_log"), \
             patch("app.agents.optimus.smartqna.usage_logger.extract_usage_from_openai_response") as me:
            me.return_value = {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100}
            result = await rerank_with_llm_async("query", chunks)

        assert len(result) == 1


# ---------------------------------------------------------------------------
# AdaptiveRetriever._apply_crag
# ---------------------------------------------------------------------------

class TestApplyCRAG:
    def setup_method(self):
        # Create retriever without real clients
        with patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client"):
            self.retriever = AdaptiveRetriever()

    def _chunks_with_scores(self, scores: list[float]) -> list[RetrievedChunk]:
        chunks = []
        for i, score in enumerate(scores):
            c = _make_chunk(id_=str(i), rerank_score=score)
            chunks.append(c)
        return chunks

    def test_no_chunks_returns_insufficient(self):
        from app.agents.optimus import config

        result = self.retriever._apply_crag(
            [], top_k=5, min_score=config.CRAG_FILTER_MIN_SCORE,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert result.confidence == CRAGConfidence.INSUFFICIENT
        assert result.chunks == []

    def test_high_score_returns_sufficient(self):
        from app.agents.optimus import config

        # Score above SUFFICIENT threshold
        chunks = self._chunks_with_scores([config.CRAG_SUFFICIENT_THRESHOLD + 0.05])
        result = self.retriever._apply_crag(
            chunks, top_k=5, min_score=config.CRAG_FILTER_MIN_SCORE,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert result.confidence == CRAGConfidence.SUFFICIENT

    def test_medium_score_returns_partial(self):
        from app.agents.optimus import config

        # Score between PARTIAL and SUFFICIENT thresholds
        mid = (config.CRAG_PARTIAL_THRESHOLD + config.CRAG_SUFFICIENT_THRESHOLD) / 2
        chunks = self._chunks_with_scores([mid])
        result = self.retriever._apply_crag(
            chunks, top_k=5, min_score=0.0,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert result.confidence == CRAGConfidence.PARTIAL

    def test_low_score_returns_insufficient(self):
        from app.agents.optimus import config

        # Score below PARTIAL threshold
        chunks = self._chunks_with_scores([config.CRAG_PARTIAL_THRESHOLD - 0.05])
        result = self.retriever._apply_crag(
            chunks, top_k=5, min_score=0.0,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert result.confidence == CRAGConfidence.INSUFFICIENT

    def test_top_k_limits_results(self):
        chunks = self._chunks_with_scores([0.9, 0.8, 0.7, 0.6, 0.5])
        result = self.retriever._apply_crag(
            chunks, top_k=3, min_score=0.0,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert len(result.chunks) <= 3

    def test_adaptive_stopping_on_score_drop(self):
        from app.agents.optimus import config

        # Large gap between chunk 2 and chunk 3 should trigger adaptive stop
        gap = config.CRAG_DROP_GAP + 0.1
        chunks = self._chunks_with_scores([0.9, 0.8, 0.8 - gap, 0.1])
        result = self.retriever._apply_crag(
            chunks, top_k=10, min_score=0.0,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        # Should stop after chunk 2 due to gap
        assert len(result.chunks) <= 3

    def test_below_min_score_filtered_but_fallback_returned(self):
        # All chunks below min_score → fallback returns top 3
        chunks = self._chunks_with_scores([0.1, 0.1, 0.1])
        result = self.retriever._apply_crag(
            chunks, top_k=5, min_score=0.8,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        # Fallback: returns some chunks anyway
        assert len(result.chunks) > 0

    def test_debug_info_populated(self):
        chunks = self._chunks_with_scores([0.8, 0.7])
        result = self.retriever._apply_crag(
            chunks, top_k=5, min_score=0.0,
            query_type=QueryType.KNOWLEDGE_QUERY
        )
        assert "top_score" in result.debug_info
        assert "filtered_count" in result.debug_info
        assert "total_candidates" in result.debug_info


# ---------------------------------------------------------------------------
# AdaptiveRetriever._vector_search
# ---------------------------------------------------------------------------

class TestVectorSearch:
    def setup_method(self):
        with patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client"):
            self.retriever = AdaptiveRetriever()

    @pytest.mark.asyncio
    async def test_success_returns_chunks(self):
        hit = MagicMock()
        hit.id = str(uuid.uuid4())
        hit.score = 0.85
        hit.payload = {
            "text": "Some chunk text",
            "doc_id": "doc-1",
            "filename": "test.pdf",
            "section_title": "Section A",
        }

        mock_results = MagicMock()
        mock_results.points = [hit]
        self.retriever.async_client = MagicMock()
        self.retriever.async_client.query_points = AsyncMock(return_value=mock_results)

        result = await self.retriever._vector_search([0.1] * 1536, "chunk", 5, {"doc-1"})

        assert len(result) == 1
        assert result[0].text == "Some chunk text"
        assert result[0].vector_score == 0.85

    @pytest.mark.asyncio
    async def test_exception_returns_empty_list(self):
        self.retriever.async_client = MagicMock()
        self.retriever.async_client.query_points = AsyncMock(
            side_effect=Exception("Qdrant error")
        )

        result = await self.retriever._vector_search([0.1] * 1536, "chunk", 5, None)

        assert result == []


# ---------------------------------------------------------------------------
# AdaptiveRetriever._rerank
# ---------------------------------------------------------------------------

class TestRerankMethod:
    def setup_method(self):
        with patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client"):
            self.retriever = AdaptiveRetriever()

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty(self):
        result = await self.retriever._rerank("query", [])
        assert result == []

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.rerank_with_llm_async")
    async def test_assigns_rerank_scores(self, mock_rerank):
        chunk = _make_chunk(id_="0", vector_score=0.5)
        mock_rerank.return_value = [(chunk, 0.87)]

        result = await self.retriever._rerank("query", [chunk])

        assert len(result) == 1
        assert result[0].rerank_score == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# AdaptiveRetriever.retrieve  (full integration)
# ---------------------------------------------------------------------------

class TestRetrieve:
    def _make_retriever(self) -> AdaptiveRetriever:
        with patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client"):
            return AdaptiveRetriever()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_active_document_ids")
    async def test_no_active_documents_returns_insufficient(self, mock_ids):
        retriever = self._make_retriever()
        mock_ids.return_value = set()

        result = await retriever.retrieve("test query")

        assert result.confidence == CRAGConfidence.INSUFFICIENT
        assert result.chunks == []
        assert "No active documents" in result.debug_info.get("reason", "")

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_active_document_ids")
    @patch("app.agents.optimus.smartqna.retriever.get_embedding_async")
    @patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval_cached")
    async def test_no_chunks_found_returns_insufficient(
        self, mock_expand, mock_embed, mock_ids
    ):
        retriever = self._make_retriever()
        mock_ids.return_value = {"doc-1"}
        mock_expand.return_value = "expanded query"
        mock_embed.return_value = [0.1] * 1536
        retriever._vector_search = AsyncMock(return_value=[])

        result = await retriever.retrieve("test query")

        assert result.confidence == CRAGConfidence.INSUFFICIENT
        assert result.chunks == []

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_active_document_ids")
    @patch("app.agents.optimus.smartqna.retriever.get_embedding_async")
    @patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval_cached")
    async def test_high_vector_score_skips_reranking(
        self, mock_expand, mock_embed, mock_ids
    ):
        from app.agents.optimus import config
        from app.agents.optimus.smartqna.query_classifier import KnowledgeSubType

        retriever = self._make_retriever()
        mock_ids.return_value = {"doc-1"}
        mock_expand.return_value = "expanded"
        mock_embed.return_value = [0.1] * 1536

        # Score above skip threshold
        high_score = config.RERANK_SKIP_THRESHOLD_SIMPLE + 0.05
        chunk = _make_chunk(vector_score=high_score, rerank_score=0.0)
        retriever._vector_search = AsyncMock(return_value=[chunk])
        retriever._rerank = AsyncMock(return_value=[chunk])  # Should NOT be called

        result = await retriever.retrieve(
            "test query", subtype=KnowledgeSubType.SIMPLE,
            query_embedding=[0.1] * 1536
        )

        retriever._rerank.assert_not_called()
        assert result.debug_info["reranking_skipped"] is True

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_active_document_ids")
    @patch("app.agents.optimus.smartqna.retriever.get_embedding_async")
    @patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval_cached")
    async def test_low_vector_score_performs_reranking(
        self, mock_expand, mock_embed, mock_ids
    ):
        from app.agents.optimus import config
        from app.agents.optimus.smartqna.query_classifier import KnowledgeSubType

        retriever = self._make_retriever()
        mock_ids.return_value = {"doc-1"}
        mock_expand.return_value = "expanded"
        mock_embed.return_value = [0.1] * 1536

        # Score below skip threshold — should trigger reranking
        low_score = config.RERANK_SKIP_THRESHOLD_SIMPLE - 0.05
        chunk = _make_chunk(vector_score=low_score, rerank_score=0.0)

        reranked_chunk = _make_chunk(rerank_score=0.8)
        retriever._vector_search = AsyncMock(return_value=[chunk])
        retriever._rerank = AsyncMock(return_value=[reranked_chunk])

        result = await retriever.retrieve(
            "test query", subtype=KnowledgeSubType.SIMPLE,
            query_embedding=[0.1] * 1536
        )

        retriever._rerank.assert_called_once()
        assert result.debug_info["reranking_skipped"] is False

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.retriever.get_active_document_ids")
    async def test_precomputed_embedding_skips_expand_and_embed(self, mock_ids):
        from app.agents.optimus import config

        retriever = self._make_retriever()
        mock_ids.return_value = {"doc-1"}

        chunk = _make_chunk(
            vector_score=config.RERANK_SKIP_THRESHOLD_SIMPLE + 0.1
        )
        retriever._vector_search = AsyncMock(return_value=[chunk])

        with patch("app.agents.optimus.smartqna.retriever.expand_query_for_retrieval_cached") as mock_expand, \
             patch("app.agents.optimus.smartqna.retriever.get_embedding_async") as mock_embed:

            result = await retriever.retrieve(
                "test query",
                query_embedding=[0.1] * 1536,  # Pre-computed
            )

            mock_expand.assert_not_called()
            mock_embed.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_collection_exists
# ---------------------------------------------------------------------------

class TestEnsureCollectionExists:
    @patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client")
    def test_collection_already_exists(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import ensure_collection_exists

        mock_client = MagicMock()
        mock_client.get_collection = AsyncMock(return_value=MagicMock())
        mock_get_client.return_value = mock_client

        ensure_collection_exists()  # Should not raise

        mock_client.create_collection.assert_not_called()

    @patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client")
    def test_collection_not_found_creates_it(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import ensure_collection_exists
        from qdrant_client.http.exceptions import UnexpectedResponse

        mock_client = MagicMock()

        error = UnexpectedResponse(
            status_code=404,
            reason_phrase="Not Found",
            content=b"Not found",
            headers=MagicMock(),
        )
        mock_client.get_collection = AsyncMock(side_effect=error)
        mock_client.create_collection = AsyncMock()
        mock_client.create_payload_index = AsyncMock()
        mock_get_client.return_value = mock_client

        ensure_collection_exists()

        mock_client.create_collection.assert_called_once()
        assert mock_client.create_payload_index.await_count >= 3

    @patch("app.agents.optimus.smartqna.retriever.get_async_qdrant_client")
    def test_non_404_error_re_raised(self, mock_get_client):
        from app.agents.optimus.smartqna.retriever import ensure_collection_exists
        from qdrant_client.http.exceptions import UnexpectedResponse

        mock_client = MagicMock()
        error = UnexpectedResponse(
            status_code=500,
            reason_phrase="Server Error",
            content=b"Error",
            headers=MagicMock(),
        )
        mock_client.get_collection = AsyncMock(side_effect=error)
        mock_get_client.return_value = mock_client

        with pytest.raises(UnexpectedResponse):
            ensure_collection_exists()


# ---------------------------------------------------------------------------
# get_active_document_ids
# ---------------------------------------------------------------------------

class TestGetActiveDocumentIds:
    @pytest.mark.asyncio
    @patch("app.db.session.AsyncSessionLocal")
    async def test_returns_set_of_ids(self, mock_session_factory):
        from app.agents.optimus.smartqna.retriever import get_active_document_ids

        doc_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [doc_id]
        mock_session.scalars = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = mock_session

        result = await get_active_document_ids()

        assert str(doc_id) in result

    @pytest.mark.asyncio
    @patch("app.db.session.AsyncSessionLocal")
    async def test_empty_database_returns_empty_set(self, mock_session_factory):
        from app.agents.optimus.smartqna.retriever import get_active_document_ids

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.scalars = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = mock_session

        result = await get_active_document_ids()

        assert result == set()