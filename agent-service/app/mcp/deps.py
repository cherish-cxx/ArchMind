"""MCP 进程的服务容器。

**一个进程一个实例**：`configure()` 幂等，第一次按需构建默认服务；
测试/工具用 `configure(McpServices(...))` 注入替身，结束后 `reset()`。

关键约束：`Retriever` 与 `McpServices.index_store` 必须是**同一个**
`IndexStore` 对象 —— 否则检索读到的索引状态和工具层看到的可能来自两套连接/缓存。
"""

import threading

import redis.asyncio

from app.config import settings
from app.core.retriever import Retriever
from app.mcp.config import mcp_settings
from app.mcp.context_client import McpContextClient
from app.mcp.rate_limit import RateLimiter
from app.store.index_store import IndexStore
from app.tools.embed_client import EmbedClient
from app.tools.java_client import JavaClient
from app.tools.qdrant_store import QdrantStore


class McpServices:
    def __init__(
        self,
        java: JavaClient,
        retriever: Retriever,
        index_store: IndexStore,
        context: McpContextClient,
        limiter: RateLimiter,
    ) -> None:
        self.java = java
        self.retriever = retriever
        self.index_store = index_store
        self.context = context
        self.limiter = limiter


_lock = threading.Lock()
_services: McpServices | None = None


def _build_defaults() -> McpServices:
    # 所有组件共用一个 IndexStore：检索的闸门与工具层的状态必须同源。
    index_store = IndexStore()
    retriever = Retriever(EmbedClient(), QdrantStore(), index_store)
    limiter_client = redis.asyncio.from_url(
        settings.redis_url,
        db=settings.redis_db,
        password=settings.redis_password or None,
        decode_responses=True,
    )
    return McpServices(
        java=JavaClient(),
        retriever=retriever,
        index_store=index_store,
        context=McpContextClient(),
        limiter=RateLimiter(limiter_client, mcp_settings.rate_limit_per_minute),
    )


def configure(services: McpServices | None = None) -> McpServices:
    """注入或惰性构建服务；重复调用返回同一实例。"""
    global _services
    with _lock:
        if services is not None:
            _services = services
        elif _services is None:
            _services = _build_defaults()
        return _services


def services() -> McpServices:
    return configure()


def reset() -> None:
    """清掉容器（主要供测试使用）。不关闭底层客户端，避免同步/异步混用。"""
    global _services
    with _lock:
        _services = None
