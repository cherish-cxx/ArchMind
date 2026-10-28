"""语义检索 —— 用户的一句话 → 若干候选类。

**它在整条链路里的位置**：`run_turn` 里「LLM1 判意图」之前，且只在这一轮
没有锚点时跑。有 focus 时用户的指代本来就有明确对象，检索纯属浪费。

**为什么是独立模块而不是塞进 loop**：检索有两件事和编排无关 ——
「这一轮能不能查」（索引状态）和「payload 怎么变成领域对象」。把它们放在这里，
`loop` 只需要决定"要不要调"，测试也能脱离 Redis/Qdrant 单独跑。

**失败一律返回空候选，绝不抛到主链路**：检索是让问答更好的附加路径，
embed 或 Qdrant 挂掉时应该退化成"和没做检索一样"，而不是让整轮 500。
"""

import asyncio
import logging
import time

from app.config import settings
from app.models.retrieval import Candidate, Retrieval
from app.store.index_store import IndexStore
from app.tools.embed_client import EmbedClient
from app.tools.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)

#: 失败日志节流：embed / Qdrant 长时间不可用时，每一轮都打一条会淹没日志。
_WARN_INTERVAL_SECONDS = 60.0


class Retriever:
    def __init__(
        self,
        embed: EmbedClient,
        qdrant: QdrantStore,
        store: IndexStore,
        *,
        top_k: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self._embed = embed
        self._qdrant = qdrant
        self._store = store
        self._top_k = top_k or settings.retrieve_top_k
        self._timeout = timeout or settings.retrieve_timeout
        self._last_warn: dict[str, float] = {}

    async def search(self, project_id: int, query: str, k: int | None = None) -> Retrieval:
        """问题文本进，候选类出。任何失败都退化成空候选。"""
        try:
            state = await self._store.get_state(project_id)
        except Exception as e:
            # 连状态都读不到（Redis 抖动）—— 和"没索引"同样处理，绝不往上抛
            self._warn_throttled("state", "读取索引状态失败，本轮无候选: %s", e)
            return Retrieval(reason="state_unavailable")

        active_rev = (state or {}).get("activeRev")
        if active_rev is None:
            # 从没成功建过索引 —— 这是唯一真正"没得查"的情况。
            # 失败过但有上一版的项目会带着 activeRev，照常可查。
            return Retrieval(reason=(state or {}).get("status") or "absent")

        partial = None
        if (state or {}).get("partial"):
            partial = f"{state.get('actual')}/{state.get('expected')}"

        vectors = await self._embed_query(query)
        if vectors is None:
            return Retrieval(reason="embed_failed", partial=partial)

        dense, sparse = vectors
        try:
            points = await asyncio.wait_for(
                self._qdrant.query(
                    project_id, dense, sparse, rev=active_rev, limit=k or self._top_k
                ),
                timeout=self._timeout,
            )
        except Exception as e:
            self._warn_throttled("qdrant", "检索查询失败，本轮无候选: %s", e)
            return Retrieval(attempted=True, reason="qdrant_failed", partial=partial)

        candidates = [c for c in (to_candidate(p) for p in points) if c is not None]
        logger.info("检索 projectId=%s rev=%s → %d 个候选", project_id, active_rev, len(candidates))
        return Retrieval(candidates=candidates, attempted=True, partial=partial)

    async def _embed_query(self, query: str) -> tuple[list[float], dict[int, float]] | None:
        try:
            results = await asyncio.wait_for(
                self._embed.embed([query], timeout=self._timeout), timeout=self._timeout
            )
        except Exception as e:
            self._warn_throttled("embed", "问题向量化失败，本轮无候选: %s", e)
            return None
        if not results:
            return None
        return results[0]

    def _warn_throttled(self, key: str, fmt: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warn.get(key, 0.0) < _WARN_INTERVAL_SECONDS:
            return
        self._last_warn[key] = now
        logger.warning(fmt, exc)


def to_candidate(point) -> Candidate | None:
    """Qdrant 的 ScoredPoint → Candidate。payload 缺关键字段的一律丢弃。"""
    payload = point.payload or {}
    uid = payload.get("uid")
    if not uid:
        return None
    head, _, _ = uid.partition("#")
    simple = payload.get("simpleName") or head.rsplit(".", 1)[-1]
    package = payload.get("packageName") or head.rsplit(".", 1)[0]
    return Candidate(
        uid=uid,
        simple_name=simple,
        package_name=package,
        kind=payload.get("kind") or "CLASS",
        description=payload.get("description"),
        rev=int(payload.get("rev") or 0),
        score=float(getattr(point, "score", 0.0) or 0.0),
    )


def resolve_entity_ref(
    entity_ref: str | None, candidates: list[Candidate], message: str
) -> str | None:
    """校验 LLM1 填的 entityRef。

    prompt 里已经写了"只能从候选里选"，但那是**约束**不是**保证** ——
    和 UICommand 一样，模型有权说，代码负责核对。三条规则：

      1. 候选里精确命中 → 接受
      2. 简单名在候选里**唯一**命中 → 换算成全限定名（模型常只写短名）
      3. 不在候选里、但简单名在**用户原话**里出现过 → 接受
         （用户自己点名的类，不能因为检索没排进前五就丢掉）
      4. 其余 → 丢弃

    候选为空时**不做约束** —— 那是"没检索"，行为必须和 R4 之前完全一致。
    """
    if not entity_ref:
        return None
    if not candidates:
        return entity_ref

    ref = entity_ref.strip()
    if not ref:
        return None
    if ref in {c.uid for c in candidates}:
        return ref

    simple = ref.partition("#")[0].rsplit(".", 1)[-1].lower()
    if not simple:
        return None

    hits = [c for c in candidates if c.simple_name.lower() == simple]
    if len(hits) == 1:
        return hits[0].uid
    if len(hits) > 1:
        # 同名类在候选里不止一个，指代不明时不猜 —— 交给澄清
        return None

    if simple in message.lower():
        return ref
    return None
