"""Shared async Redis client — rate limits, optional cache.

PERFORMANCE: Uses connection pooling for better concurrency.
"""

from redis.asyncio import Redis, ConnectionPool

from app.config.settings import settings

_redis: Redis | None = None
_pool: ConnectionPool | None = None


def get_redis() -> Redis:
    """Get Redis client with connection pooling for high concurrency."""
    global _redis, _pool
    if _redis is None:
        # Create connection pool with higher limits for concurrency
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            max_connections=50,  # Allow more concurrent connections
            retry_on_timeout=True,
        )
        _redis = Redis(connection_pool=_pool)
    return _redis


async def close_redis() -> None:
    """Close Redis client and connection pool. Call on app shutdown."""
    global _redis, _pool
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
