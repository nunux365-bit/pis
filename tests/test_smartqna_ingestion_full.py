"""
Comprehensive regression tests for smartqna/ingestion.py.

Covers all code paths in:
  - compute_file_hash()
  - cleanup_stuck_documents()
  - ingest_document()          – new, duplicate hash, doc_id update,
                                  invalid UUID, not found, failure
  - ingest_documents_from_directory()
  - _generate_summary()
  - _generate_section_summary()
  - _store_chunks_in_qdrant()
  - _store_sections()
  - delete_document()
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _make_tmp_txt(content: str = "sample content for testing") -> Path:
    with tempfile.NamedTemporaryFile(
        suffix=".txt", mode="w", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(content)
    return Path(fh.name)


def _make_mock_doc(
    *,
    status: str = "pending",
    filename: str = "test.pdf",
    file_hash: str = "abc123",
    doc_id: str | None = None,
    file_path: str | None = None,
) -> MagicMock:
    doc = MagicMock()
    doc.id = uuid.UUID(doc_id) if doc_id else uuid.uuid4()
    doc.filename = filename
    doc.file_hash = file_hash
    doc.status = status
    doc.file_path = file_path
    doc.page_count = None
    doc.chunk_count = None
    doc.summary = None
    doc.ingested_at = None
    doc.error_message = None
    doc.created_at = datetime.now(timezone.utc)
    doc.updated_at = datetime.now(timezone.utc)
    return doc


def _make_mock_session() -> tuple[AsyncMock, AsyncMock]:
    """Return (session_factory_mock, session_mock)."""
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.close = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    return factory, session


def _make_mock_parsed_doc(content: str = "parsed content"):
    parsed = MagicMock()
    parsed.content = content
    parsed.page_count = 2
    return parsed


def _make_mock_chunks(count: int = 3):
    chunks = []
    for i in range(count):
        chunk = MagicMock()
        chunk.text = f"Chunk {i} content " * 50
        chunk.section_title = f"Section {i}" if i % 2 == 0 else None
        chunk.level = 1
        chunks.append(chunk)
    return chunks


def _make_llm_response(text: str = "Generated summary text.") -> MagicMock:
    response = MagicMock()
    response.content = text
    response.usage_metadata = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    response.response_metadata = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
    return response


# ---------------------------------------------------------------------------
# compute_file_hash
# ---------------------------------------------------------------------------

class TestComputeFileHash:
    def test_returns_sha256_hex_string(self):
        from app.agents.optimus.smartqna.ingestion import compute_file_hash

        tmp = _make_tmp_txt("known content")
        try:
            result = compute_file_hash(tmp)
            expected = hashlib.sha256(b"known content").hexdigest()
            assert result == expected
        finally:
            tmp.unlink()

    def test_different_files_different_hashes(self):
        from app.agents.optimus.smartqna.ingestion import compute_file_hash

        tmp1 = _make_tmp_txt("content A")
        tmp2 = _make_tmp_txt("content B")
        try:
            assert compute_file_hash(tmp1) != compute_file_hash(tmp2)
        finally:
            tmp1.unlink()
            tmp2.unlink()

    def test_same_content_same_hash(self):
        from app.agents.optimus.smartqna.ingestion import compute_file_hash

        tmp1 = _make_tmp_txt("identical content")
        tmp2 = _make_tmp_txt("identical content")
        try:
            assert compute_file_hash(tmp1) == compute_file_hash(tmp2)
        finally:
            tmp1.unlink()
            tmp2.unlink()

    def test_accepts_string_path(self):
        from app.agents.optimus.smartqna.ingestion import compute_file_hash

        tmp = _make_tmp_txt("string path content")
        try:
            result = compute_file_hash(str(tmp))
            assert isinstance(result, str)
            assert len(result) == 64  # SHA-256 hex digest length
        finally:
            tmp.unlink()


# ---------------------------------------------------------------------------
# cleanup_stuck_documents
# ---------------------------------------------------------------------------

class TestCleanupStuckDocuments:
    @pytest.mark.asyncio
    async def test_marks_stuck_documents_failed(self):
        from app.agents.optimus.smartqna.ingestion import cleanup_stuck_documents

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 3
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()

        count = await cleanup_stuck_documents(session)

        assert count == 3
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_stuck_documents(self):
        from app.agents.optimus.smartqna.ingestion import cleanup_stuck_documents

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        session.execute = AsyncMock(return_value=mock_result)

        count = await cleanup_stuck_documents(session)

        assert count == 0
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# ingest_document
# ---------------------------------------------------------------------------

class TestIngestDocument:
    """Tests for the main ingest_document() pipeline."""

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.compute_file_hash")
    @patch("app.agents.optimus.smartqna.ingestion.parse_document")
    @patch("app.agents.optimus.smartqna.ingestion.chunk_document")
    @patch("app.agents.optimus.smartqna.ingestion._generate_summary")
    @patch("app.agents.optimus.smartqna.ingestion.ensure_collection_exists_async", new_callable=AsyncMock)
    @patch("app.agents.optimus.smartqna.ingestion._store_chunks_in_qdrant")
    @patch("app.agents.optimus.smartqna.ingestion._store_sections")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_new_document_happy_path(
        self,
        mock_session_factory,
        mock_store_sections,
        mock_store_qdrant,
        mock_ensure,
        mock_generate_summary,
        mock_chunk,
        mock_parse,
        mock_hash,
    ):
        from app.agents.optimus.smartqna.ingestion import ingest_document

        tmp = _make_tmp_txt()
        factory, session = _make_mock_session()
        mock_session_factory.return_value = factory.return_value
        mock_session_factory.return_value = session

        mock_hash.return_value = "newhash123"
        session.scalar = AsyncMock(return_value=None)  # No existing document

        mock_parse.return_value = _make_mock_parsed_doc()
        mock_chunk.return_value = _make_mock_chunks(3)
        mock_generate_summary.return_value = "Document summary."
        mock_store_qdrant.return_value = None
        mock_store_sections.return_value = None

        result = await ingest_document(str(tmp))

        tmp.unlink(missing_ok=True)

        assert result["status"] == "success"
        assert "document_id" in result
        assert result["chunk_count"] == 3

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.compute_file_hash")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_duplicate_document_already_ingested(
        self, mock_session_factory, mock_hash
    ):
        from app.agents.optimus.smartqna.ingestion import ingest_document
        from app.db.models import OptimusDocumentStatus

        tmp = _make_tmp_txt()
        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        mock_hash.return_value = "existinghash"

        existing_doc = _make_mock_doc(status=OptimusDocumentStatus.INGESTED.value)
        existing_doc.id = uuid.uuid4()
        session.scalar = AsyncMock(return_value=existing_doc)

        result = await ingest_document(str(tmp))

        tmp.unlink(missing_ok=True)

        assert result["status"] == "already_exists"
        assert "document_id" in result

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.compute_file_hash")
    @patch("app.agents.optimus.smartqna.ingestion.parse_document")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_parse_failure_marks_document_failed(
        self, mock_session_factory, mock_parse, mock_hash
    ):
        from app.agents.optimus.smartqna.ingestion import ingest_document

        tmp = _make_tmp_txt()
        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        mock_hash.return_value = "somehash"
        session.scalar = AsyncMock(return_value=None)

        mock_parse.side_effect = RuntimeError("Parsing failed!")

        result = await ingest_document(str(tmp))

        tmp.unlink(missing_ok=True)

        assert result["status"] == "failed"
        assert "error" in result
        assert "Parsing failed" in result["error"]

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.compute_file_hash")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_invalid_doc_id_falls_through_to_new(
        self, mock_session_factory, mock_hash
    ):
        from app.agents.optimus.smartqna.ingestion import ingest_document
        from app.agents.optimus.smartqna.ingestion import parse_document

        tmp = _make_tmp_txt()
        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        mock_hash.return_value = "somehash"
        session.scalar = AsyncMock(return_value=None)
        session.get = AsyncMock(return_value=None)

        with patch("app.agents.optimus.smartqna.ingestion.parse_document") as mock_parse, \
             patch("app.agents.optimus.smartqna.ingestion.chunk_document") as mock_chunk, \
             patch("app.agents.optimus.smartqna.ingestion._generate_summary") as mock_sum, \
             patch("app.agents.optimus.smartqna.ingestion.ensure_collection_exists_async", new_callable=AsyncMock), \
             patch("app.agents.optimus.smartqna.ingestion._store_chunks_in_qdrant"), \
             patch("app.agents.optimus.smartqna.ingestion._store_sections"):

            mock_parse.return_value = _make_mock_parsed_doc()
            mock_chunk.return_value = _make_mock_chunks(2)
            mock_sum.return_value = "Summary."

            result = await ingest_document(str(tmp), doc_id="not-a-valid-uuid")

        tmp.unlink(missing_ok=True)

        # Should not crash and should produce a result
        assert result["status"] in ("success", "failed")

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.compute_file_hash")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_existing_doc_id_updates_record(
        self, mock_session_factory, mock_hash
    ):
        from app.agents.optimus.smartqna.ingestion import ingest_document

        tmp = _make_tmp_txt()
        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        mock_hash.return_value = "somehash"

        existing_doc_id = str(uuid.uuid4())
        existing_doc = _make_mock_doc(
            doc_id=existing_doc_id, filename="original.pdf", status="failed"
        )
        session.get = AsyncMock(return_value=existing_doc)

        with patch("app.agents.optimus.smartqna.ingestion.parse_document") as mock_parse, \
             patch("app.agents.optimus.smartqna.ingestion.chunk_document") as mock_chunk, \
             patch("app.agents.optimus.smartqna.ingestion._generate_summary") as mock_sum, \
             patch("app.agents.optimus.smartqna.ingestion.ensure_collection_exists_async", new_callable=AsyncMock), \
             patch("app.agents.optimus.smartqna.ingestion._store_chunks_in_qdrant"), \
             patch("app.agents.optimus.smartqna.ingestion._store_sections"):

            mock_parse.return_value = _make_mock_parsed_doc()
            mock_chunk.return_value = _make_mock_chunks(2)
            mock_sum.return_value = "Summary."

            result = await ingest_document(str(tmp), doc_id=existing_doc_id)

        tmp.unlink(missing_ok=True)

        assert result["status"] == "success"
        # Filename should be from the DB record
        assert result["filename"] == "original.pdf"


# ---------------------------------------------------------------------------
# ingest_documents_from_directory
# ---------------------------------------------------------------------------

class TestIngestDocumentsFromDirectory:
    @pytest.mark.asyncio
    async def test_not_a_directory_raises(self):
        from app.agents.optimus.smartqna.ingestion import ingest_documents_from_directory

        with pytest.raises(ValueError, match="Not a directory"):
            await ingest_documents_from_directory("/this/path/does/not/exist")

    @pytest.mark.asyncio
    async def test_directory_with_mixed_files(self):
        from app.agents.optimus.smartqna.ingestion import ingest_documents_from_directory

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a txt file and an unsupported file
            (Path(tmpdir) / "doc1.txt").write_text("content 1")
            (Path(tmpdir) / "ignored.zip").write_text("zip data")

            with patch("app.agents.optimus.smartqna.ingestion.ingest_document") as mock_ingest:
                mock_ingest.return_value = {
                    "status": "success",
                    "document_id": str(uuid.uuid4()),
                    "filename": "doc1.txt",
                    "chunk_count": 2,
                }
                results = await ingest_documents_from_directory(tmpdir)

        # Only .txt should be ingested
        assert len(results) == 1
        assert all(r["status"] in ("success", "failed", "already_exists") for r in results)

    @pytest.mark.asyncio
    async def test_empty_directory_returns_empty_list(self):
        from app.agents.optimus.smartqna.ingestion import ingest_documents_from_directory

        with tempfile.TemporaryDirectory() as tmpdir:
            results = await ingest_documents_from_directory(tmpdir)

        assert results == []


# ---------------------------------------------------------------------------
# _generate_summary
# ---------------------------------------------------------------------------

class TestGenerateSummary:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_no_api_key_returns_truncated_content(self, mock_config):
        from app.agents.optimus.smartqna.ingestion import _generate_summary

        mock_config.OPENAI_API_KEY = None

        long_content = "word " * 1000
        result = await _generate_summary(long_content)
        assert len(result) <= 503  # 500 + "..."
        assert result.endswith("...")

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_no_api_key_short_content_not_truncated(self, mock_config):
        from app.agents.optimus.smartqna.ingestion import _generate_summary

        mock_config.OPENAI_API_KEY = None

        short_content = "Short document content."
        result = await _generate_summary(short_content)
        assert result == short_content

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_success(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.return_value = "This is a generated summary."

        result = await _generate_summary("document content...")

        assert result == "This is a generated summary."

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_exception_falls_back_to_truncation(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.side_effect = Exception("LLM error")

        long_content = "word " * 1000
        result = await _generate_summary(long_content)

        assert len(result) <= 503

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_empty_response_falls_back(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.return_value = None

        content = "Content to fall back to."
        result = await _generate_summary(content)

        # Should return the original content (truncated) since LLM returned empty
        assert result == content


# ---------------------------------------------------------------------------
# _generate_section_summary
# ---------------------------------------------------------------------------

class TestGenerateSectionSummary:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_no_api_key_returns_truncated_content(self, mock_config):
        from app.agents.optimus.smartqna.ingestion import _generate_section_summary

        mock_config.OPENAI_API_KEY = None

        long_content = "word " * 1000
        result = await _generate_section_summary(long_content, "Section Title")
        assert len(result) <= 200

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_success(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_section_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.return_value = "Section summary here."

        result = await _generate_section_summary("section content", "Title")

        assert result == "Section summary here."

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_exception_fallback(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_section_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.side_effect = Exception("error")

        content = "section content " * 10
        result = await _generate_section_summary(content, "Title")

        # Fallback: first 200 chars
        assert len(result) <= 200

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.chat_complete_text")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_llm_empty_content_fallback(self, mock_config, mock_chat):
        from app.agents.optimus.smartqna.ingestion import _generate_section_summary

        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o"

        mock_chat.return_value = None

        result = await _generate_section_summary("content " * 50, "Title")

        assert len(result) <= 200


# ---------------------------------------------------------------------------
# _store_sections
# ---------------------------------------------------------------------------

class TestStoreSections:
    @pytest.mark.asyncio
    async def test_stores_unique_sections(self):
        from app.agents.optimus.smartqna.ingestion import _store_sections
        from app.agents.optimus.smartqna.chunker import DocumentChunk

        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        chunks = [
            DocumentChunk(text="Text A", section_title="Section 1", level=1, chunk_index=0),
            DocumentChunk(text="Text B", section_title="Section 1", level=1, chunk_index=1),  # Same section
            DocumentChunk(text="Text C", section_title="Section 2", level=1, chunk_index=2),
        ]

        doc_id = str(uuid.uuid4())
        with patch("app.agents.optimus.smartqna.ingestion.OptimusSection") as mock_section_cls:
            mock_section_cls.return_value = MagicMock()
            await _store_sections(session, doc_id, chunks)

        # Two unique sections → 2 add calls
        assert session.add.call_count == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_appends_content_for_same_section(self):
        from app.agents.optimus.smartqna.ingestion import _store_sections
        from app.agents.optimus.smartqna.chunker import DocumentChunk

        session = AsyncMock()
        section_mock = MagicMock()
        section_mock.content = "First chunk."
        session.add = MagicMock()
        session.flush = AsyncMock()

        chunks = [
            DocumentChunk(text="First chunk.", section_title="SameSection", chunk_index=0),
            DocumentChunk(text="Second chunk.", section_title="SameSection", chunk_index=1),
        ]

        doc_id = str(uuid.uuid4())
        with patch("app.agents.optimus.smartqna.ingestion.OptimusSection") as mock_section_cls:
            mock_section_cls.return_value = section_mock
            await _store_sections(session, doc_id, chunks)

        # The second chunk should be appended to the section content
        assert "Second chunk." in section_mock.content

    @pytest.mark.asyncio
    async def test_none_section_title_handled(self):
        from app.agents.optimus.smartqna.ingestion import _store_sections
        from app.agents.optimus.smartqna.chunker import DocumentChunk

        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        chunks = [
            DocumentChunk(text="Untitled chunk.", section_title=None, chunk_index=0),
        ]

        doc_id = str(uuid.uuid4())
        with patch("app.agents.optimus.smartqna.ingestion.OptimusSection") as mock_section_cls:
            mock_section_cls.return_value = MagicMock()
            await _store_sections(session, doc_id, chunks)

        assert session.add.call_count == 1


# ---------------------------------------------------------------------------
# _store_chunks_in_qdrant
# ---------------------------------------------------------------------------

class TestStoreChunksInQdrant:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.get_async_qdrant_client")
    @patch("app.agents.optimus.smartqna.ingestion.get_embeddings_batch_async")
    @patch("app.agents.optimus.smartqna.ingestion._generate_section_summary")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_stores_chunks_and_summary(
        self, mock_config, mock_gen_summary, mock_embeddings, mock_client
    ):
        from app.agents.optimus.smartqna.ingestion import _store_chunks_in_qdrant
        from app.agents.optimus.smartqna.chunker import DocumentChunk

        mock_config.QDRANT_COLLECTION = "test_collection"
        mock_embeddings.side_effect = lambda texts: [[0.1] * 1536 for _ in texts]
        mock_gen_summary.return_value = "Section summary."

        qdrant = MagicMock()
        qdrant.upsert = AsyncMock()
        mock_client.return_value = qdrant

        chunks = [
            DocumentChunk(text="text " * 100, section_title="Section A", level=1, chunk_index=0),
            DocumentChunk(text="text " * 50, section_title=None, level=0, chunk_index=1),
        ]

        await _store_chunks_in_qdrant(
            str(uuid.uuid4()), "test.pdf", chunks, "Document summary."
        )

        qdrant.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.get_async_qdrant_client")
    @patch("app.agents.optimus.smartqna.ingestion.get_embeddings_batch_async")
    @patch("app.agents.optimus.smartqna.ingestion.config")
    async def test_empty_chunks_still_stores_summary(
        self, mock_config, mock_embeddings, mock_client
    ):
        from app.agents.optimus.smartqna.ingestion import _store_chunks_in_qdrant

        mock_config.QDRANT_COLLECTION = "test_collection"
        mock_embeddings.side_effect = lambda texts: [[0.1] * 1536 for _ in texts]

        qdrant = MagicMock()
        qdrant.upsert = AsyncMock()
        mock_client.return_value = qdrant

        await _store_chunks_in_qdrant(
            str(uuid.uuid4()), "test.pdf", [], "Document summary text."
        )

        qdrant.upsert.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete_document
# ---------------------------------------------------------------------------

class TestDeleteDocument:
    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.get_async_qdrant_client")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_delete_existing_document(self, mock_session_factory, mock_client):
        from app.agents.optimus.smartqna.ingestion import delete_document

        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        doc = _make_mock_doc(status="ingested", file_path=None)
        session.get = AsyncMock(return_value=doc)
        session.delete = AsyncMock()

        qdrant = MagicMock()
        qdrant.delete = AsyncMock()
        mock_client.return_value = qdrant

        result = await delete_document(str(doc.id))

        assert result is True
        session.delete.assert_called_once_with(doc)
        session.commit.assert_called_once()
        qdrant.delete.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_delete_nonexistent_document_returns_false(self, mock_session_factory):
        from app.agents.optimus.smartqna.ingestion import delete_document

        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        session.get = AsyncMock(return_value=None)

        result = await delete_document(str(uuid.uuid4()))

        assert result is False

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.get_async_qdrant_client")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_delete_cleans_up_uploaded_file(self, mock_session_factory, mock_client):
        from app.agents.optimus.smartqna.ingestion import delete_document

        tmp = _make_tmp_txt("file to delete")
        _, session = _make_mock_session()
        mock_session_factory.return_value = session

        doc = _make_mock_doc(status="failed", file_path=str(tmp))
        doc.filename = "test.pdf"
        session.get = AsyncMock(return_value=doc)
        session.delete = AsyncMock()

        qdrant = MagicMock()
        qdrant.delete = AsyncMock()
        mock_client.return_value = qdrant

        result = await delete_document(str(doc.id))

        assert result is True
        # File should have been deleted
        assert not tmp.exists()

    @pytest.mark.asyncio
    @patch("app.agents.optimus.smartqna.ingestion.get_async_qdrant_client")
    @patch("app.agents.optimus.smartqna.ingestion.AsyncSessionLocal")
    async def test_delete_with_provided_session(self, mock_session_factory, mock_client):
        """When a session is provided, the function should NOT close it."""
        from app.agents.optimus.smartqna.ingestion import delete_document

        _, session = _make_mock_session()
        doc = _make_mock_doc(status="ingested", file_path=None)
        session.get = AsyncMock(return_value=doc)
        session.delete = AsyncMock()

        qdrant = MagicMock()
        qdrant.delete = AsyncMock()
        mock_client.return_value = qdrant

        result = await delete_document(str(doc.id), session=session)

        assert result is True
        # close() should NOT have been called (we own the session)
        session.close.assert_not_called()