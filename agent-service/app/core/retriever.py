"""语义检索 —— 用户的一句话 → 若干候选类。

**它在整条链路里的位置**：每一轮的第一次模型调用之前，且只在这一轮
没有锚点时跑。有 focus 时用户的指代本来就有明确对象，检索纯属浪费。
命中的候选会以 retrieval 消息进入上下文，同时成为模型下一次工具调用的可选起点。

**为什么是独立模块而不是塞进循环**：检索有两件事和编排无关 ——
「这一轮能不能查」（索引状态）和「payload 怎么变成领域对象」。把它们放在这里，
循环只需要决定"要不要调"，测试也能脱离 Redis/Qdrant 单独跑。

**失败一律返回空候选，绝不抛到主链路**：检索是让问答更好的附加路径，
embed 或 Qdrant 挂掉时应该退化成"和没做检索一样"，而不是让整轮 500。
"""

import asyncio
import logging
import time

from app.config import settings
from app.models.retrieval import Candidate, CapabilityCandidate, Retrieval
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
        capability_qdrant: QdrantStore | None = None,
        capability_store: IndexStore | None = None,
    ) -> None:
        self._embed = embed
        self._qdrant = qdrant
        self._store = store
        self._top_k = top_k or settings.retrieve_top_k
        self._timeout = timeout or settings.retrieve_timeout
        # 能力检索是可选装配：三个参数构造必须与 R4 完全一致。
        # `capability_qdrant is None` = 功能没接线，`capability_reason` 保持 None，
        # 不能伪装成「没索引」（absent）。
        self._capability_qdrant = capability_qdrant
        self._capability_store = capability_store
        self._capability_enabled = capability_qdrant is not None
        self._last_warn: dict[str, float] = {}

    async def search(self, project_id: int, query: str, k: int | None = None) -> Retrieval:
        """问题文本进，候选类 + 候选能力出。任何失败都退化成空候选。"""
        # 两路状态独立读取：任一路读失败只影响自己。
        class_state: dict | None = None
        class_state_failed = False
        try:
            class_state = await self._store.get_state(project_id)
        except Exception as e:
            # 类状态读不到：类是主目标，但不能因此掐掉健康的能力检索。
            self._warn_throttled("state", "读取索引状态失败，本轮无类候选: %s", e)
            class_state_failed = True

        class_rev = (class_state or {}).get("activeRev")
        if class_state_failed:
            class_status = "state_unavailable"
        else:
            class_status = (class_state or {}).get("status") or "absent"
        # 只有一边有 activeRev 时另一边记 absent；状态没读到则如实记 state_unavailable。
        class_missing_reason = "state_unavailable" if class_state_failed else "absent"

        cap_state: dict | None = None
        cap_state_failed = False
        if self._capability_enabled and self._capability_store is not None:
            try:
                cap_state = await self._capability_store.get_state(project_id)
            except Exception as e:
                # 能力状态读不到只影响能力这一路；类候选照常返回。
                self._warn_throttled(
                    "capability_state", "读取能力索引状态失败，本轮无能力候选: %s", e
                )
                cap_state_failed = True

        cap_rev = (cap_state or {}).get("activeRev")
        if cap_state_failed:
            cap_status = "state_unavailable"
        else:
            cap_status = (cap_state or {}).get("status") or "absent"
        cap_missing_reason = "state_unavailable" if cap_state_failed else "absent"

        if class_state_failed:
            # 能力健康才继续 embed 并只查能力；否则维持 R4「立刻返回、不 embed」。
            if not (self._capability_enabled and cap_rev is not None):
                return Retrieval(
                    reason="state_unavailable",
                    capability_reason=(cap_status if self._capability_enabled else None),
                )
        elif class_rev is None and cap_rev is None:
            # 两边都没有 activeRev 才是真正"没得查"。只有一边有时，
            # 仍然 embed 一次并只查那一边，另一边记 absent。
            return Retrieval(
                reason=class_status,
                capability_reason=(cap_status if self._capability_enabled else None),
            )

        partial = None
        if not class_state_failed and (class_state or {}).get("partial"):
            partial = f"{class_state.get('actual')}/{class_state.get('expected')}"

        vectors = await self._embed_query(query)
        if vectors is None:
            # embed 是两路共用的：只要那一路本来要查，就记 embed_failed。
            return Retrieval(
                reason=("embed_failed" if class_rev is not None else class_missing_reason),
                capability_reason=(
                    ("embed_failed" if cap_rev is not None else cap_missing_reason)
                    if self._capability_enabled
                    else None
                ),
                partial=partial,
            )

        dense, sparse = vectors

        # 两路并发，各自一个超时：能力慢/挂不会拖住类结果（总耗时不超过一次超时）。
        queries = {}
        if class_rev is not None:
            queries["class"] = self._query(
                self._qdrant, project_id, dense, sparse, class_rev, k
            )
        if cap_rev is not None:
            queries["capability"] = self._query(
                self._capability_qdrant, project_id, dense, sparse, cap_rev, k
            )
        results = dict(zip(queries.keys(), await asyncio.gather(
            *queries.values(), return_exceptions=True
        )))

        # ── 类这一路（attempted/reason/partial 只描述它）──
        if class_rev is not None:
            class_result = results["class"]
            if isinstance(class_result, BaseException):
                self._warn_throttled("qdrant", "检索查询失败，本轮无候选: %s", class_result)
                candidates: list[Candidate] = []
                attempted, class_reason = True, "qdrant_failed"
            else:
                candidates = [
                    c for c in (to_candidate(p) for p in class_result) if c is not None
                ]
                attempted, class_reason = True, None
        else:
            candidates, attempted, class_reason = [], False, class_missing_reason

        # ── 能力这一路（独立原因，互不覆盖）──
        capabilities: list[CapabilityCandidate] = []
        cap_reason: str | None = None
        if self._capability_enabled:
            if cap_rev is None:
                cap_reason = cap_missing_reason
            else:
                cap_result = results["capability"]
                if isinstance(cap_result, BaseException):
                    self._warn_throttled(
                        "capability_qdrant", "能力检索失败，本轮无能力候选: %s", cap_result
                    )
                    cap_reason = "qdrant_failed"
                else:
                    capabilities = [
                        c for c in (to_capability(p) for p in cap_result) if c is not None
                    ]

        logger.info(
            "检索 projectId=%s rev=%s → %d 个候选类 / %d 个能力",
            project_id, class_rev, len(candidates), len(capabilities),
        )
        return Retrieval(
            candidates=candidates,
            capabilities=capabilities,
            attempted=attempted,
            reason=class_reason,
            partial=partial,
            capability_reason=cap_reason,
        )

    async def _query(
        self,
        qdrant: QdrantStore,
        project_id: int,
        dense: list[float],
        sparse: dict[int, float],
        rev: int,
        k: int | None,
    ):
        """单路查询 + 单路超时。异常交给 `gather(return_exceptions=True)`。"""
        return await asyncio.wait_for(
            qdrant.query(project_id, dense, sparse, rev=rev, limit=k or self._top_k),
            timeout=self._timeout,
        )

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
    method_names = tuple(
        m.get("name") for m in (payload.get("methods") or [])
        if isinstance(m, dict) and m.get("name")
    )[:5]
    return Candidate(
        uid=uid,
        simple_name=simple,
        package_name=package,
        kind=payload.get("kind") or "CLASS",
        description=payload.get("description"),
        rev=int(payload.get("rev") or 0),
        score=float(getattr(point, "score", 0.0) or 0.0),
        methods=method_names,
    )


def _text(value, default=None):
    """只接受非空字符串；其他类型一律退回 default（绝不 `str()` 强转）。"""
    return value if isinstance(value, str) else default


def _int(value, default: int = 0) -> int:
    """int-like 才收：bool 不算，浮点只收整数值，其余退回 default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def _float(value, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def to_capability(point) -> CapabilityCandidate | None:
    """Qdrant 的 ScoredPoint → CapabilityCandidate。

    `uid` / `entryUid` 必须是**非空字符串**（没有入口类，能力候选就不可执行）；
    其余字段按类型收：文本只收字符串，计数只收 int-like，坏类型退回默认值。
    **绝不抛异常**：一个坏 payload 不能把整轮检索打挂。
    """
    try:
        payload = point.payload or {}
        if not isinstance(payload, dict):
            return None
        uid = _text(payload.get("uid"))
        entry_uid = _text(payload.get("entryUid"))
        if not uid or not entry_uid:
            return None
        return CapabilityCandidate(
            uid=uid,
            name=_text(payload.get("name")) or uid,
            description=_text(payload.get("description")),
            area_uid=_text(payload.get("areaUid"), ""),
            area_name=_text(payload.get("areaName"), ""),
            entry_uid=entry_uid,
            entry_kind=_text(payload.get("entryKind")),
            member_count=_int(payload.get("memberCount")),
            rev=_int(payload.get("rev")),
            score=_float(getattr(point, "score", 0.0)),
        )
    except Exception:  # noqa: BLE001 —— payload 形状不可控，宁愿丢这一个点
        return None
