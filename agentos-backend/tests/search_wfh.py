#!/usr/bin/env python3
"""Search for "work from home" content in Qdrant."""

import asyncio
import sys
sys.path.insert(0, ".")

async def search_wfh():
    from app.agents.optimus.smartqna.retriever import (
        get_async_qdrant_client,
        get_embedding_async,
    )
    from app.agents.optimus import config
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    print("Searching for 'work from home' content in Qdrant...\n")

    # Get embedding for "work from home"
    wfh_embedding = await get_embedding_async("work from home policy")
    print(f"Got embedding for 'work from home policy'")

    # Search without any doc filter - get ALL chunks
    client = get_async_qdrant_client()

    results = await client.query_points(
        collection_name=config.QDRANT_COLLECTION,
        query=wfh_embedding,
        limit=100,  # Get top 100
        with_payload=True,
    )

    print(f"\nTop 20 results for 'work from home policy':\n")
    for i, point in enumerate(results.points[:20]):
        score = point.score
        text = point.payload.get("text", "")[:200]
        filename = point.payload.get("filename", "unknown")
        doc_id = point.payload.get("doc_id", "unknown")

        # Check if this chunk mentions "work from home"
        has_wfh = "work from home" in text.lower() or "wfh" in text.lower()
        marker = "<<< WFH FOUND!" if has_wfh else ""

        print(f"[{i+1}] Score: {score:.4f} | Doc: {filename}")
        print(f"    Text: {text}...")
        if marker:
            print(f"    {marker}")
        print()

    # Also do a keyword search in payloads
    print("\n" + "="*60)
    print("Now searching for ANY chunk containing 'work from home'...")
    print("="*60 + "\n")

    # Scroll through all chunks and find ones with "work from home"
    all_points, _ = await client.scroll(
        collection_name=config.QDRANT_COLLECTION,
        limit=1000,  # Get up to 1000 chunks
        with_payload=True,
        with_vectors=False,
    )

    wfh_chunks = []
    for point in all_points:
        text = point.payload.get("text", "").lower()
        if "work from home" in text or "remote work" in text or "wfh" in text:
            wfh_chunks.append(point)

    if wfh_chunks:
        print(f"Found {len(wfh_chunks)} chunks containing 'work from home' / 'remote work' / 'wfh':\n")
        for point in wfh_chunks:
            text = point.payload.get("text", "")[:300]
            filename = point.payload.get("filename", "unknown")
            doc_id = point.payload.get("doc_id", "unknown")
            print(f"Doc: {filename}")
            print(f"Doc ID: {doc_id}")
            print(f"Text: {text}...")
            print()
    else:
        print("NO chunks found containing 'work from home', 'remote work', or 'wfh'!")
        print("\nThis means the content was either:")
        print("  1. Never ingested")
        print("  2. In a document that's not active")
        print("  3. Chunked differently than expected")


if __name__ == "__main__":
    asyncio.run(search_wfh())
