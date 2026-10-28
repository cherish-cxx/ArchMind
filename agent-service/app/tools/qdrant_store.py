"""Qdrant 读写：dense + sparse 混合向量，按 projectId + rev 隔离。

集合结构是固定的（bge-m3 输出 1024 维），换模型必须重建集合：
    dense  : 1024, Cosine
    sparse : 无维度声明
    payload index: projectId (keyword) / rev (integer)

**rev 参与过滤是 R4 的关键**：重新索引时先写新 rev 的点、成功后才把
`activeRev` 切过去，旧版本在此期间继续对外服务 —— 所以每次查询都必须
同时限定「哪个项目」和「哪一版」。少了 rev 就会把两版混在一起检索。
"""

import logging

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
        # 只缓存「存在」的结论：集合可能是索引任务里才建出来的，
        # 把 False 也缓存下来会让检索一直读不到刚建好的集合。
        self._exists = False

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
            logger.info("已创建 Qdrant 集合 %s", self._collection)
        self._exists = True
        await self._ensure_payload_index("projectId", PayloadSchemaType.KEYWORD)
        await self._ensure_payload_index("rev", PayloadSchemaType.INTEGER)
        self._ensured = True

    async def _ensure_payload_index(self, field: str, schema: PayloadSchemaType) -> None:
        """幂等地补建 payload 索引 —— 老集合里没有 rev 索引，建索引时顺手补上。"""
        try:
            await self._client.create_payload_index(
                collection_name=self._collection, field_name=field, field_schema=schema
            )
        except Exception as e:  # 已存在 / 版本差异都不该影响索引流程
            logger.debug("payload 索引 %s 未创建（可能已存在）: %s", field, e)

    async def delete_project(self, project_id: int) -> None:
        """清掉该项目的全部点（删除项目时用）。

        ⚠️ **重新索引不走这里** —— 那只删 `rev != 新版本` 的旧点，
        否则失败时会把还在服务的那一版一起删掉。
        """
        await self._client.delete(
            collection_name=self._collection,
            points_selector=self._project_filter(project_id),
            wait=True,
        )

    async def delete_rev(self, project_id: int, rev: int) -> None:
        """删掉某一次失败的索引留下的点（尽力而为）。"""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(must=[
                FieldCondition(key="projectId", match=MatchValue(value=project_id)),
                FieldCondition(key="rev", match=MatchValue(value=rev)),
            ]),
            wait=True,
        )

    async def delete_other_revs(self, project_id: int, active_rev: int) -> None:
        """新版本切换成功后，清掉所有非当前版本的点。"""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="projectId", match=MatchValue(value=project_id))],
                must_not=[FieldCondition(key="rev", match=MatchValue(value=active_rev))],
            ),
            wait=True,
        )

    async def upsert(self, points: list[PointStruct]) -> None:
        if not points:
            return
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=True
        )

    async def count_project(self, project_id: int, rev: int | None = None) -> int:
        """数点。带 rev 时只数那一版 —— 重新索引期间两版共存，不带 rev 会数多。"""
        clauses = [FieldCondition(key="projectId", match=MatchValue(value=project_id))]
        if rev is not None:
            clauses.append(FieldCondition(key="rev", match=MatchValue(value=rev)))
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=Filter(must=clauses),
            exact=True,
        )
        return result.count

    async def query(
        self,
        project_id: int,
        dense: list[float],
        sparse: dict[int, float],
        *,
        rev: int,
        limit: int | None = None,
        prefetch_limit: int | None = None,
    ) -> list[ScoredPoint]:
        """混合检索：dense 与 sparse 各取 prefetch_limit 个，RRF 融合后取 limit 个。

        **filter 同时传给两路 prefetch 和顶层 query**：范围必须在「取前 N」之前生效，
        只写在顶层就要赌 Qdrant 的下发语义 —— 赌错的表现是候选莫名其妙变少，不报错。
        """
        if not await self._collection_exists():
            return []

        limit = limit if limit is not None else settings.retrieve_top_k
        prefetch_limit = prefetch_limit or settings.retrieve_prefetch_limit
        flt = self._scope_filter(project_id, rev)

        prefetches = [Prefetch(query=dense, using="dense", limit=prefetch_limit, filter=flt)]
        if sparse:
            indices = sorted(sparse)
            prefetches.append(Prefetch(
                query=SparseVector(indices=indices, values=[sparse[i] for i in indices]),
                using="sparse",
                limit=prefetch_limit,
                filter=flt,
            ))

        resp = await self._client.query_points(
            collection_name=self._collection,
            prefetch=prefetches,
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=flt,
            limit=limit,
            with_payload=True,
        )
        return resp.points

    async def _collection_exists(self) -> bool:
        if self._exists:
            return True
        try:
            self._exists = await self._client.collection_exists(self._collection)
        except Exception as e:
            logger.warning("无法确认 Qdrant 集合是否存在: %s", e)
            return False
        return self._exists

    @staticmethod
    def _project_filter(project_id: int) -> Filter:
        return Filter(
            must=[FieldCondition(key="projectId", match=MatchValue(value=project_id))]
        )

    @staticmethod
    def _scope_filter(project_id: int, rev: int) -> Filter:
        return Filter(must=[
            FieldCondition(key="projectId", match=MatchValue(value=project_id)),
            FieldCondition(key="rev", match=MatchValue(value=rev)),
        ])
