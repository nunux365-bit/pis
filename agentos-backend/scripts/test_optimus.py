#!/usr/bin/env python3
"""Quick local test script for Optimus SmartQnA."""

import asyncio
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_retriever():
    """Test the retriever with a sample query."""
    from app.agents.optimus.smartqna.retriever import (
        ensure_collection_exists_async,
        get_embedding_async,
        rerank_with_llm_async,
        RetrievedChunk,
    )

    print("Testing OpenAI embedding...")
    test_text = "What is the company leave policy?"
    embedding = await get_embedding_async(test_text)
    print(f"  Embedding generated: {len(embedding)} dimensions")

    print("Ensuring Qdrant collection exists...")
    await ensure_collection_exists_async()
    print("  Collection ready")

    # Test LLM reranker
    print("Testing LLM-based reranker...")
    test_chunks = [
        RetrievedChunk(
            id="1", text="Employees get 20 days of paid leave per year.",
            doc_id="doc1", filename="leave_policy.pdf", chunk_type="chunk", vector_score=0.8
        ),
        RetrievedChunk(
            id="2", text="The weather today is sunny.",
            doc_id="doc2", filename="weather.pdf", chunk_type="chunk", vector_score=0.5
        ),
    ]
    reranked = await rerank_with_llm_async(test_text, test_chunks)
    print(f"  Reranked results:")
    for chunk, score in reranked:
        print(f"    - Score {score:.2f}: {chunk.text[:50]}...")

    print("\nRetriever test passed!")


async def test_document_parsing():
    """Test document parsing."""
    from app.agents.optimus.smartqna.document_parser import parse_document
    from app.agents.optimus.smartqna.chunker import chunk_document

    # Create a test document
    test_file = Path("/tmp/test_optimus_doc.txt")
    test_file.write_text("""
# Company Leave Policy

## Annual Leave
Employees are entitled to 20 days of paid annual leave per year.
Leave must be approved by the reporting manager at least 2 weeks in advance.

## Sick Leave
Sick leave of up to 10 days per year is provided.
A medical certificate is required for sick leave exceeding 3 consecutive days.

## Maternity/Paternity Leave
- Maternity leave: 26 weeks
- Paternity leave: 2 weeks
""")

    print("Parsing test document...")
    parsed = parse_document(test_file)
    print(f"  Filename: {parsed.filename}")
    print(f"  Content length: {len(parsed.content)} chars")

    print("Chunking document...")
    chunks = chunk_document(parsed.content)
    print(f"  Created {len(chunks)} chunks")

    for i, chunk in enumerate(chunks[:3]):
        print(f"\n  Chunk {i + 1}:")
        print(f"    Section: {chunk.section_title}")
        print(f"    Text: {chunk.text[:100]}...")

    # Cleanup
    test_file.unlink()
    print("\nDocument parsing test passed!")


async def test_query_classifier():
    """Test query classification."""
    from app.agents.optimus.smartqna.query_classifier import classify_query, QueryType

    queries = [
        "What is the leave policy?",
        "List all types of leave available",
        "Compare annual leave vs sick leave",
        "How do I apply for leave?",
    ]

    print("Testing query classification...")
    for query in queries:
        qtype = await classify_query(query)
        print(f"  '{query}' -> {qtype.value}")

    print("\nQuery classifier test passed!")


async def main():
    print("=" * 60)
    print("OPTIMUS LOCAL TEST")
    print("=" * 60)

    try:
        await test_document_parsing()
        print()
        await test_retriever()
        print()
        await test_query_classifier()
        print()
        print("=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
