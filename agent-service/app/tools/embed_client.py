"""Embed 容器客户端：卡片文本 → dense + sparse。

容器接口（R4 部署）：
    POST /embed  {"texts": ["..."]}
    → {"dense": [[...1024 floats...]], "sparse": [{"2130": 0.31, ...}]}

sparse 的 key 是字符串，这里统一转成 int，排序的工作交给 Qdrant 写入时做。
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbedError(Exception):
    """Embed 服务不可达、返回非 200、或数量对不上。"""


class EmbedClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.embed_url,
            timeout=timeout or settings.index_timeout,
            # 服务间固定地址，不走桌面代理（和 java_client 同一个理由）
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[tuple[list[float], dict[int, float]]]:
        if not texts:
            return []
        try:
            resp = await self._client.post("/embed", json={"texts": texts})
        except httpx.TransportError as e:
            raise EmbedError(f"embed 服务不可达: {e}") from e
        if resp.status_code != 200:
            raise EmbedError(f"embed 返回 HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        dense = data.get("dense") or []
        sparse = data.get("sparse") or []
        if len(dense) != len(texts) or len(sparse) != len(texts):
            raise EmbedError(
                f"embed 返回数量不匹配: texts={len(texts)}, dense={len(dense)}, sparse={len(sparse)}"
            )
        return [(dense[i], to_sparse(sparse[i])) for i in range(len(texts))]


def to_sparse(raw: dict[str, Any]) -> dict[int, float]:
    """容器返回 {"2130": 0.31}；转成 token_id → 权重，后面写入时再排序。"""
    return {int(k): float(v) for k, v in raw.items()}
