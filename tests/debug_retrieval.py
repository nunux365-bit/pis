#!/usr/bin/env python3
"""Debug script to inspect retrieval chunks for a given query.

This script helps diagnose retrieval quality issues by showing:
1. Query classification
2. Vector search results (before reranking)
3. Reranked chunks with scores
4. Final filtered chunks that go to the LLM
5. Why certain chunks might be filtered out

Usage:
    cd agents_platform/agentos-backend
    python tests/debug_retrieval.py "your question here"

    # With verbose mode (shows chunk text)
    python tests/debug_retrieval.py "your question here" --verbose

    # Show more chunks
    python tests/debug_retrieval.py "your question here" --top 20
"""

import argparse
import asyncio
import sys

# Add parent directory to path for imports
sys.path.insert(0, ".")


def truncate(text: str, max_len: int = 150) -> str:
    """Truncate text for display."""
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


async def debug_retrieval(query: str, verbose: bool = False, top_k: int = 10):
    """Debug retrieval pipeline for a query."""
    from app.agents.optimus.smartqna.query_classifier import (
        classify_query,
        classify_knowledge_subtype,
        QueryType,
    )
    from app.agents.optimus.smartqna.retriever import (
        AdaptiveRetriever,
        get_embedding_async,
        rerank_with_llm_async,
        RetrievedChunk,
        CRAGConfidence,
    )
    from app.agents.optimus.smartqna.agent import get_active_document_names
    from app.agents.optimus import config

    print("=" * 80)
    print(f"DEBUG RETRIEVAL: {query}")
    print("=" * 80)

    # Step 1: Query Classification
    print("\n[1] QUERY CLASSIFICATION")
    print("-" * 40)

    doc_catalog = await get_active_document_names()
    print(f"Active documents: {len(doc_catalog)}")
    if verbose and doc_catalog:
        for doc in doc_catalog[:5]:
            print(f"  - {doc}")
        if len(doc_catalog) > 5:
            print(f"  ... and {len(doc_catalog) - 5} more")

    query_type = await classify_query(query, doc_catalog)
    print(f"Query type: {query_type.value}")

    if query_type != QueryType.KNOWLEDGE_QUERY:
        print(f"\nQuery classified as {query_type.value} - not a knowledge query.")
        print("Retrieval would be skipped. Reclassify or check classifier prompt.")
        return

    subtype = await classify_knowledge_subtype(query)
    print(f"Knowledge subtype: {subtype.value}")

    # Step 2: Query Expansion + Embedding
    print("\n[2] QUERY EXPANSION + EMBEDDING")
    print("-" * 40)

    from app.agents.optimus.smartqna.retriever import expand_query_for_retrieval
    expanded_query = await expand_query_for_retrieval(query)
    print(f"Original query: {query}")
    print(f"LLM-Expanded query: {expanded_query}")

    embedding = await get_embedding_async(expanded_query)
    print(f"Embedding dimensions: {len(embedding)}")
    print(f"First 5 values: {embedding[:5]}")

    # Step 3: Vector Search (BEFORE reranking)
    print("\n[3] VECTOR SEARCH (before reranking)")
    print("-" * 40)

    from app.agents.optimus.smartqna.retriever import get_active_document_ids
    from app.agents.optimus import config as optimus_config

    retriever = AdaptiveRetriever()

    # Get retrieval parameters (hardcoded since method doesn't exist)
    vector_candidates = optimus_config.VECTOR_SEARCH_CANDIDATES
    print(f"Vector search candidates: {vector_candidates}")
    print(f"Search types: ['chunk']")

    # Get active document IDs
    active_doc_ids = await get_active_document_ids()
    print(f"Active doc IDs in Qdrant filter: {len(active_doc_ids)}")

    if not active_doc_ids:
        print("ERROR: No active document IDs found!")
        return

    # Execute vector search
    all_chunks = []
    chunks = await retriever._vector_search(
        embedding,
        "chunk",
        vector_candidates,
        active_doc_ids,
    )
    print(f"  chunk: {len(chunks)} chunks")
    all_chunks.extend(chunks)

    # Deduplicate
    seen_ids = set()
    unique_chunks = []
    for chunk in all_chunks:
        if chunk.id not in seen_ids:
            seen_ids.add(chunk.id)
            unique_chunks.append(chunk)

    print(f"\nTotal unique chunks from vector search: {len(unique_chunks)}")

    if not unique_chunks:
        print("ERROR: No chunks returned from vector search!")
        print("Check: Is the query too different from document content?")
        print("       Are documents properly ingested in Qdrant?")
        return

    # Show top vector search results
    print(f"\nTop {min(top_k, len(unique_chunks))} by vector score:")
    sorted_by_vector = sorted(unique_chunks, key=lambda c: c.vector_score, reverse=True)
    for i, chunk in enumerate(sorted_by_vector[:top_k]):
        print(f"\n  [{i+1}] Score: {chunk.vector_score:.4f}")
        print(f"      Doc: {chunk.filename}")
        print(f"      Type: {chunk.chunk_type}")
        if verbose:
            print(f"      Text: {truncate(chunk.text, 300)}")
        else:
            print(f"      Text: {truncate(chunk.text, 100)}")

    # Step 4: Reranking
    print("\n[4] LLM RERANKING")
    print("-" * 40)

    # Take top chunks for reranking (all vector search results go to reranker)
    rerank_candidates = sorted_by_vector[:vector_candidates]
    print(f"Reranking top {len(rerank_candidates)} chunks...")

    # Call reranker directly to see raw response
    from openai import AsyncOpenAI
    from app.config.settings import settings
    import json as json_module

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Build prompt (same as retriever)
    chunk_descriptions = []
    for i, chunk in enumerate(rerank_candidates):
        text_preview = chunk.text[:800] + "..." if len(chunk.text) > 800 else chunk.text
        text_preview = " ".join(text_preview.split())
        chunk_descriptions.append(f"[Passage {i}]\n{text_preview}")

    chunks_text = "\n\n".join(chunk_descriptions)
    num_passages = len(rerank_candidates)

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

    print(f"Prompt length: {len(prompt)} chars")
    print(f"Model: {optimus_config.RERANKER_MODEL}")

    try:
        response = await client.chat.completions.create(
            model=optimus_config.RERANKER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,
        )

        raw_response = response.choices[0].message.content
        print(f"\n=== RAW LLM RESPONSE (first 1000 chars) ===")
        print(raw_response[:1000] if raw_response else "None")
        print(f"=== END RAW RESPONSE ===\n")

        if raw_response:
            # Parse it
            response_text = raw_response.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            response_text = response_text.strip()

            scores_dict = json_module.loads(response_text)
            print(f"Parsed {len(scores_dict)} scores")

            # Apply scores
            for i, chunk in enumerate(rerank_candidates):
                chunk.rerank_score = float(scores_dict.get(str(i), 0.0))

            reranked_chunks = sorted(rerank_candidates, key=lambda c: c.rerank_score, reverse=True)

            print(f"\nTop {min(top_k, len(reranked_chunks))} by rerank score:")
            for i, chunk in enumerate(reranked_chunks[:top_k]):
                print(f"\n  [{i+1}] Rerank: {chunk.rerank_score:.4f} (Vector: {chunk.vector_score:.4f})")
                print(f"      Doc: {chunk.filename}")
                if verbose:
                    print(f"      Text: {truncate(chunk.text, 300)}")
                else:
                    print(f"      Text: {truncate(chunk.text, 100)}")
        else:
            print("ERROR: LLM returned None")
            reranked_chunks = rerank_candidates
            for chunk in reranked_chunks:
                chunk.rerank_score = chunk.vector_score

    except Exception as e:
        print(f"Reranking failed: {e}")
        import traceback
        traceback.print_exc()
        reranked_chunks = rerank_candidates
        for chunk in reranked_chunks:
            chunk.rerank_score = chunk.vector_score

    # Step 5: CRAG Filtering
    print("\n[5] CRAG FILTERING")
    print("-" * 40)
    print(f"Min score threshold: {optimus_config.CRAG_FILTER_MIN_SCORE}")
    print(f"Drop gap threshold: {optimus_config.CRAG_DROP_GAP}")
    final_top_k = 5  # Default final results
    print(f"Target top_k: {final_top_k}")

    # Apply same filtering logic as retriever
    filtered = []
    prev_score = None
    min_score = optimus_config.CRAG_FILTER_MIN_SCORE

    for chunk in reranked_chunks:
        # Skip below minimum threshold
        if chunk.rerank_score < min_score and filtered:
            print(f"  FILTERED OUT (below min {min_score}): {chunk.rerank_score:.4f} - {truncate(chunk.text, 50)}")
            continue

        # Adaptive stopping
        if prev_score is not None and len(filtered) >= 2:
            gap = prev_score - chunk.rerank_score
            if gap > optimus_config.CRAG_DROP_GAP:
                print(f"  ADAPTIVE STOP: gap {gap:.4f} > {optimus_config.CRAG_DROP_GAP}")
                break

        filtered.append(chunk)
        prev_score = chunk.rerank_score

        if len(filtered) >= final_top_k:
            print(f"  REACHED top_k limit: {final_top_k}")
            break

    print(f"\nFinal filtered chunks: {len(filtered)}")

    # Determine confidence
    if not filtered:
        print("\nCONFIDENCE: INSUFFICIENT (no chunks passed filters)")
    elif filtered[0].rerank_score >= optimus_config.CRAG_SUFFICIENT_THRESHOLD:
        print(f"\nCONFIDENCE: SUFFICIENT (top score {filtered[0].rerank_score:.4f} >= {optimus_config.CRAG_SUFFICIENT_THRESHOLD})")
    elif filtered[0].rerank_score >= optimus_config.CRAG_PARTIAL_THRESHOLD:
        print(f"\nCONFIDENCE: PARTIAL (top score {filtered[0].rerank_score:.4f} >= {optimus_config.CRAG_PARTIAL_THRESHOLD})")
    else:
        print(f"\nCONFIDENCE: INSUFFICIENT (top score {filtered[0].rerank_score:.4f} < {optimus_config.CRAG_PARTIAL_THRESHOLD})")

    # Step 6: Show what goes to LLM
    print("\n[6] CHUNKS SENT TO LLM FOR ANSWER GENERATION")
    print("-" * 40)

    if not filtered:
        print("NO CHUNKS - LLM will respond with 'insufficient information'")
    else:
        for i, chunk in enumerate(filtered):
            print(f"\n  [{i+1}] Score: {chunk.rerank_score:.4f}")
            print(f"      Doc: {chunk.filename}")
            if verbose:
                print(f"      Full Text:\n{chunk.text[:500]}...")
            else:
                print(f"      Text: {truncate(chunk.text, 150)}")

    # Analysis
    print("\n" + "=" * 80)
    print("ANALYSIS")
    print("=" * 80)

    # Check for potential issues
    issues = []

    if not unique_chunks:
        issues.append("Vector search returned no results. Check if documents are ingested.")
    elif all(c.vector_score < 0.5 for c in unique_chunks):
        issues.append("All vector scores are low (<0.5). Query may not match document vocabulary.")

    if reranked_chunks and filtered:
        top_vector = sorted_by_vector[0].text[:100] if sorted_by_vector else ""
        top_final = filtered[0].text[:100] if filtered else ""
        if top_vector != top_final:
            print("Note: Top vector result differs from top reranked result (reranking changed order)")

    if not filtered and reranked_chunks:
        issues.append(f"All chunks filtered out by CRAG. Min score={optimus_config.CRAG_FILTER_MIN_SCORE}, top rerank={reranked_chunks[0].rerank_score:.4f}")

    if issues:
        print("\nPotential Issues:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\nNo obvious issues detected.")

    # Suggestions
    print("\nSuggestions:")
    print("  - If relevant content exists but wasn't found, try different keywords")
    print("  - 'remote work' vs 'work from home' - different embeddings, same meaning")
    print("  - Consider adding synonyms to document during ingestion")
    print("  - Check if CRAG thresholds are too aggressive")


def main():
    parser = argparse.ArgumentParser(description="Debug Optimus retrieval pipeline")
    parser.add_argument("query", help="The query to debug")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full chunk text")
    parser.add_argument("--top", "-k", type=int, default=10, help="Number of top chunks to show")

    args = parser.parse_args()

    asyncio.run(debug_retrieval(args.query, verbose=args.verbose, top_k=args.top))


if __name__ == "__main__":
    main()
