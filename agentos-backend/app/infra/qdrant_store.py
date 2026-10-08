"""Qdrant — per-user payload filter for RAG / policy chunks."""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse

from app.config.settings import settings

_async_client: AsyncQdrantClient | None = None


def get_async_qdrant() -> AsyncQdrantClient:
    if not settings.qdrant_enabled:
        raise RuntimeError("Qdrant is disabled (set QDRANT_ENABLED=true to enable)")
    global _async_client
    if _async_client is None:
        _async_client = AsyncQdrantClient(
            url=settings.qdrant_url,
            timeout=settings.qdrant_timeout_seconds,
        )
    return _async_client


async def close_async_qdrant() -> None:
    """Close policy Qdrant client singleton. Safe to call multiple times."""
    global _async_client
    client = _async_client
    _async_client = None
    if client is not None:
        try:
            await client.close()
        except Exception:
            pass


async def ensure_policy_collection_async(vector_size: int = 1536) -> None:
    """Ensure the policies collection exists (async — safe for FastAPI lifespan)."""
    if not settings.qdrant_enabled:
        return
    client = get_async_qdrant()
    name = settings.qdrant_collection_policies
    try:
        await client.get_collection(name)
        return
    except UnexpectedResponse as e:
        if e.status_code != 404:
            raise

    await client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
    )
