#!/usr/bin/env python3
"""
Reset Optimus SmartQnA embeddings for migration to OpenAI embeddings.

This script:
1. Deletes the Qdrant collection (optimus_smartqna)
2. Resets document status in PostgreSQL so they can be re-ingested

Run with: python scripts/reset_optimus_embeddings.py
"""

import asyncio
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def reset_qdrant_collection() -> bool:
    """Delete the Optimus Qdrant collection."""
    from qdrant_client import QdrantClient
    from app.config.settings import settings

    print("\n=== Resetting Qdrant Collection ===")
    print(f"Qdrant URL: {settings.qdrant_url}")

    try:
        client = QdrantClient(url=settings.qdrant_url, timeout=30)
        collection_name = settings.qdrant_collection_optimus

        # Check existing collections
        collections = client.get_collections()
        existing = [c.name for c in collections.collections]
        print(f"Existing collections: {existing}")

        if collection_name in existing:
            # Get info before deleting
            info = client.get_collection(collection_name)
            print(f"Collection: {collection_name}")
            print(f"  Points: {info.points_count}")
            print(f"  Dimensions: {info.config.params.vectors.size}")

            # Delete
            client.delete_collection(collection_name)
            print(f"✓ Deleted collection: {collection_name}")
        else:
            print(f"Collection '{collection_name}' does not exist (already clean)")

        return True
    except Exception as e:
        print(f"✗ Qdrant error: {e}")
        return False


async def reset_database_documents(delete_all: bool = False) -> bool:
    """Reset document status in PostgreSQL."""
    from sqlalchemy import select, delete, update
    from app.db.session import AsyncSessionLocal
    from app.db.models import OptimusDocument, OptimusSection

    print("\n=== Resetting Database Documents ===")

    try:
        async with AsyncSessionLocal() as session:
            # Count current documents
            result = await session.execute(
                select(OptimusDocument.id, OptimusDocument.filename, OptimusDocument.status)
            )
            docs = result.all()
            print(f"Found {len(docs)} documents in database")

            if not docs:
                print("No documents to reset")
                return True

            # Show current status
            for doc in docs[:5]:
                print(f"  - {doc.filename}: {doc.status}")
            if len(docs) > 5:
                print(f"  ... and {len(docs) - 5} more")

            if delete_all:
                # Delete all sections first (foreign key constraint)
                await session.execute(delete(OptimusSection))
                print("✓ Deleted all sections")

                # Delete all documents
                await session.execute(delete(OptimusDocument))
                print("✓ Deleted all documents")
            else:
                # Reset status to pending for re-ingestion
                await session.execute(
                    update(OptimusDocument)
                    .where(OptimusDocument.status == "ingested")
                    .values(status="pending", chunk_count=None, ingested_at=None)
                )
                print("✓ Reset document status to 'pending'")

            await session.commit()
            print("✓ Changes committed")
            return True

    except Exception as e:
        print(f"✗ Database error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print("=" * 60)
    print("OPTIMUS EMBEDDINGS RESET SCRIPT")
    print("Migrating from BGE (768 dim) to OpenAI (1536 dim)")
    print("=" * 60)

    # Ask user preference
    print("\nOptions:")
    print("  1. Reset documents (keep records, re-ingest)")
    print("  2. Delete all documents (start completely fresh)")
    print("  3. Only reset Qdrant (keep database as-is)")

    choice = input("\nChoose option [1/2/3] (default: 1): ").strip() or "1"

    qdrant_ok = True
    db_ok = True

    if choice in ["1", "2", "3"]:
        qdrant_ok = reset_qdrant_collection()

    if choice == "1":
        db_ok = await reset_database_documents(delete_all=False)
    elif choice == "2":
        db_ok = await reset_database_documents(delete_all=True)
    elif choice == "3":
        print("\n=== Skipping Database Reset ===")
        print("Note: Documents still show 'ingested' status but vectors are gone")

    print("\n" + "=" * 60)
    if qdrant_ok and db_ok:
        print("✓ RESET COMPLETE!")
        print("\nNext steps:")
        print("  1. Start the backend: uvicorn app.main:app --reload")
        print("  2. Re-ingest documents via API or upload UI")
        print("  3. New embeddings will use OpenAI text-embedding-3-small (1536 dim)")
    else:
        print("✗ RESET HAD ERRORS - check output above")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
