"""MCP 层限流：Redis 固定窗口，按 pat_id 计数。

放在 MCP 进程而不是 Java：`search_entities` 直连 Qdrant，不经过 Java，
只有这里能看到全部请求。
"""

import logging

import redis.asyncio

logger = logging.getLogger(__name__)

_KEY_PREFIX = "archmind:mcp:ratelimit:"


class RateLimiter:
    def __init__(
        self,
        redis: redis.asyncio.Redis,
        limit: int,
        window_seconds: int = 60,
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds

    async def allow(self, pat_id: int) -> bool:
        """INCR 后判断是否超限；第一次计数时设置窗口 TTL。

        Redis 不可用时**放行**（fail-open）：限流是保护，不是鉴权；
        让它变成「Redis 抖动 → 全部 500」比偶尔放开更糟。
        """
        key = f"{_KEY_PREFIX}{pat_id}"
        try:
            count = await self._redis.incr(key)
            # 首次计数，或上次 EXPIRE 没成功留下无 TTL 的 key —— 补一次。
            if count == 1 or await self._redis.ttl(key) < 0:
                await self._redis.expire(key, self._window)
        except Exception as e:  # noqa: BLE001 —— Redis 任何故障都不该拖垮工具
            # 只记异常类型与 pat_id，不记 token。
            logger.warning("MCP 限流不可用，放行 patId=%s: %s", pat_id, type(e).__name__)
            return True
        return count <= self._limit
