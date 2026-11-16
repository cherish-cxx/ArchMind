"""RateLimiter：固定窗口的计数语义与 TTL 设置。

用假 Redis：这一层只该关心 INCR/EXPIRE 的用法，不连真 Redis。
"""

from app.mcp.rate_limit import RateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        self.expire_calls.append((key, seconds))
        return True


class BrokenRedis:
    async def incr(self, key: str) -> int:
        raise RuntimeError("redis down")


async def test_first_n_calls_allowed_then_denied():
    limiter = RateLimiter(FakeRedis(), limit=3, window_seconds=60)

    results = [await limiter.allow(1) for _ in range(4)]

    assert results == [True, True, True, False]


async def test_ttl_set_on_first_key_only():
    redis = FakeRedis()
    limiter = RateLimiter(redis, limit=5, window_seconds=60)

    await limiter.allow(42)
    await limiter.allow(42)

    assert redis.expire_calls == [("archmind:mcp:ratelimit:42", 60)]
    assert redis.ttls["archmind:mcp:ratelimit:42"] == 60


async def test_redis_failure_fails_open():
    """限流是保护不是鉴权：Redis 抖动时放行，日志里也没有 token。"""
    limiter = RateLimiter(BrokenRedis(), limit=1)

    assert await limiter.allow(7) is True
