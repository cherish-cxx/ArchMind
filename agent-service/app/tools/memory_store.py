"""记忆的 Qdrant 索引（R5）。

**和 `QdrantStore`（类索引）是两个集合，不是两个类。** 原因不是代码洁癖：

- 类索引按 `projectId + rev` 隔离，重新分析时 `delete_other_revs` 按 rev 清点；
- 记忆是纵向的，按 `userId + projectId + status` 隔离，**不带 rev 过滤**。

合在一个集合里，那条 `delete_other_revs` 会把所有记忆一起删掉。

失效即删点：MySQL 行只在 `active` 时应该出现在这里，调用方在状态迁移后
删掉对应 point。过滤条件漏写一次就会把失效记忆召回进 prompt，删点让这件事
在物理上不可能。
"""

import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Prefetch,
    ScoredPoint,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.config import settings

logger = logging.getLogger(__name__)


def point_id(memory_id: int) -> str:
    """MySQL 行 id → 确定的 Qdrant point id，重复 upsert 幂等。"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"archmind:memory:{memory_id}"))


class MemoryStore:
    def __init__(self, url: str | None = None, collection: str | None = None) -> None:
        self._client = AsyncQdrantClient(
            url=url or settings.qdrant_url,
            timeout=30,
            check_compatibility=False,
        )
        self._collection = collection or settings.qdrant_memory_collection
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
            logger.info("已创建 Qdrant 记忆集合 %s", self._collection)
        for field, schema in (
            ("userId", PayloadSchemaType.INTEGER),
            ("projectId", PayloadSchemaType.INTEGER),
            ("type", PayloadSchemaType.KEYWORD),
            ("status", PayloadSchemaType.KEYWORD),
            ("primarySubjectUid", PayloadSchemaType.KEYWORD),
        ):
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection, field_name=field, field_schema=schema
                )
            except Exception as e:  # 已存在 / 版本差异都不该影响写入
                logger.debug("记忆 payload 索引 %s 未创建: %s", field, e)
        self._ensured = True

    async def upsert(self, points: list[PointStruct]) -> None:
        if not points:
            return
        await self.ensure_collection()
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=True
        )

    async def query(
        self,
        user_id: int,
        project_id: int,
        dense: list[float],
        sparse: dict[int, float],
        *,
        limit: int | None = None,
    ) -> list[ScoredPoint]:
        """混合检索。**只返回 active** —— 失效记忆不该出现在任何召回里。"""
        if not await self._client.collection_exists(self._collection):
            return []

        flt = Filter(must=[
            FieldCondition(key="userId", match=MatchValue(value=user_id)),
            FieldCondition(key="projectId", match=MatchValue(value=project_id)),
            FieldCondition(key="status", match=MatchValue(value="active")),
        ])
        prefetch = settings.retrieve_prefetch_limit
        prefetches = [Prefetch(query=dense, using="dense", limit=prefetch, filter=flt)]
        if sparse:
            indices = sorted(sparse)
            prefetches.append(Prefetch(
                query=SparseVector(indices=indices, values=[sparse[i] for i in indices]),
                using="sparse",
                limit=prefetch,
                filter=flt,
            ))

        resp = await self._client.query_points(
            collection_name=self._collection,
            prefetch=prefetches,
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=flt,
            limit=limit or settings.memory_top_k,
            with_payload=True,
        )
        return resp.points

    async def delete_ids(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=[point_id(i) for i in memory_ids],
            wait=True,
        )

    async def delete_project(self, project_id: int) -> None:
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(must=[
                FieldCondition(key="projectId", match=MatchValue(value=project_id))
            ]),
            wait=True,
        )
