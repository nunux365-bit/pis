"""Fixed-window rate limiting per key (IP or user id)."""

from app.infra.redis_client import get_redis


async def check_rate_limit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """
    Returns True if under limit (request allowed).
    Uses INCR + EXPIRE on first hit.
    """
    if limit <= 0:
        return True
    r = get_redis()
    redis_key = f"rl:{key}"
    try:
        n = await r.incr(redis_key)
        if n == 1:
            await r.expire(redis_key, window_seconds)
        return n <= limit
    except Exception:
        # Fail open if Redis unavailable (degraded mode)
        return True
