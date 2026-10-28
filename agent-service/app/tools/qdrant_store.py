"""Qdrant 写入/清理：dense + sparse 混合向量，按 projectId 隔离。

集合结构是固定的（bge-m3 输出 1024 维），换模型必须重建集合：
    dense  : 1024, Cosine
    sparse : 无维度声明
    payload index: projectId (keyword)
"""

import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    SparseVectorParams,
    VectorParams,
)

from app.config import settings

logger = logging.getLogger(__name__)


class QdrantStore:
    def __init__(self, url: str | None = None, collection: str | None = None) -> None:
        self._client = AsyncQdrantClient(
            url=url or settings.qdrant_url,
            timeout=60,
            # 不在构造时探测服务端版本：Qdrant 暂时不可用时不该拖慢/污染启动日志
            check_compatibility=False,
        )
        self._collection = collection or settings.qdrant_collection
        self._ensured = False

    async def aclose(self) -> None:
        await self._client.close()

    async def ensure_collection(self) -> None:
        if self._ensured:
            return
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config={"dense": VectorParams(size=1024, distance=Distance.COSINE)},
                sparse_vectors_config={"sparse": SparseVectorParams()},
            )
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name="projectId",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info("已创建 Qdrant 集合 %s", self._collection)
        self._ensured = True

    async def delete_project(self, project_id: int) -> None:
        """清掉该项目的全部旧点。v1 用状态闸门挡住索引期间的半成品。"""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=self._project_filter(project_id),
            wait=True,
        )

    async def upsert(self, points: list[PointStruct]) -> None:
        if not points:
            return
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=True
        )

    async def count_project(self, project_id: int) -> int:
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=self._project_filter(project_id),
            exact=True,
        )
        return result.count

    @staticmethod
    def _project_filter(project_id: int) -> Filter:
        return Filter(
            must=[FieldCondition(key="projectId", match=MatchValue(value=project_id))]
        )
