"""记忆仓库（R5 阶段 0）。

**这里只做数据库的事**：不生成记忆、不调 LLM、不碰 Qdrant。
"这条记忆能不能写"（subject ⊆ evidence）在写入方；"状态变了索引怎么同步"
在调用方。仓库只保证三件事：

1. 同样的文本不会长出第二行（唯一键 + 合并）；
2. 失效只改 `status`，不删行（审计 / 复活 / 评测）；
3. 复活只发生在 `stale` 且新版本不旧于旧版本时。

并发写的兜底是 `uk_memory_dedup`：两个会话同时蒸出同一条事实时，后写的那条
撞唯一键报错 —— 阶段 0 不自动重试，由调用方决定（重试一次即变成合并）。
"""

import logging

from sqlalchemy import delete, func, select, update

from app.models.memory import (
    MemoryDraft,
    MemoryStatus,
    MemoryTrust,
    MemoryType,
    normalize_text,
)
from app.store.db import Database
from app.store.orm import Memory

logger = logging.getLogger(__name__)


def decide_merge(status: str | None, existing_rev: int, draft_rev: int) -> str:
    """命中已有行时怎么办。

    - 没命中 → `insert`
    - `active` → `update`（刷新文本、证据、置信度）
    - `stale` 且新 rev **更高** → `revive`
    - 其余（`expired` / `rejected` / `superseded`）→ `ignore`

    为什么是严格大于：`stale` 意味着图已经翻到了更新的版本。同样的结论若来自
    **旧 rev 的积压回合**（`draft_rev == existing_rev`），它证明不了"新图里还有它"，
    复活了就是拿旧证据冒充新证据。只有在新 rev 下重新推导出来（`>`）才算数。
    """
    if status is None:
        return "insert"
    if status == MemoryStatus.ACTIVE:
        return "update"
    if status == MemoryStatus.STALE and draft_rev > existing_rev:
        return "revive"
    return "ignore"


class MemoryRepo:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or Database()

    async def aclose(self) -> None:
        await self._db.aclose()

    # ==================== 写 ====================

    async def upsert(
        self, user_id: int, project_id: int, draft: MemoryDraft
    ) -> tuple[Memory, str]:
        """写入一条记忆，返回 `(行, 动作)`，动作为 `inserted/updated/revived/ignored`。

        用 `SELECT ... FOR UPDATE` 锁住去重键对应的行：并发下两个写入方
        至少有一个会因为唯一键失败，而不是双双插入。
        """
        async with self._db.session() as s:
            async with s.begin():
                row = (await s.execute(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        Memory.project_id == project_id,
                        Memory.scope == draft.scope.value,
                        Memory.type == draft.type.value,
                        Memory.text_hash == draft.text_hash,
                    )
                    .with_for_update()
                )).scalar_one_or_none()

                action = decide_merge(
                    row.status if row else None,
                    row.graph_rev if row else 0,
                    draft.graph_rev,
                )

                if action == "insert":
                    row = Memory(
                        user_id=user_id,
                        project_id=project_id,
                        scope=draft.scope.value,
                        type=draft.type.value,
                        text=normalize_text(draft.text),
                        text_hash=draft.text_hash,
                        primary_subject_uid=draft.primary_subject_uid,
                        subject_uids=list(draft.subject_uids),
                        graph_rev=draft.graph_rev,
                        confidence=draft.confidence,
                        trust=draft.trust.value,
                        status=MemoryStatus.ACTIVE.value,
                        source_conversation_id=draft.source_conversation_id,
                        source_turn_no=draft.source_turn_no,
                        expires_at=draft.expires_at,
                    )
                    s.add(row)
                elif action == "update":
                    _apply(row, draft, revive=False)
                elif action == "revive":
                    _apply(row, draft, revive=True)
                else:
                    return row, "ignored"

                await s.flush()

        return row, ("inserted" if action == "insert" else action + "d")

    async def mark_stale(self, project_id: int, current_rev: int) -> list[int]:
        """重新分析后：旧 rev 的事实置 `stale`，行保留。**返回被置 stale 的 id**。

        同 `expire_capacity`：Qdrant 的 payload 还是 `active`，调用方必须删点，
        否则过滤条件会把已经失效的记忆继续召回。

        只动 `FACT` —— 未决问题不一定绑图，偏好/纠正（v1 不写）更不该跟着 rev 走。
        """
        async with self._db.session() as s:
            async with s.begin():
                ids = (await s.execute(
                    select(Memory.id).where(
                        Memory.project_id == project_id,
                        Memory.type == MemoryType.FACT.value,
                        Memory.status == MemoryStatus.ACTIVE.value,
                        Memory.graph_rev != current_rev,
                    )
                )).scalars().all()
                if not ids:
                    return []
                await s.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(status=MemoryStatus.STALE.value)
                )
        logger.info("projectId=%s 标记 %s 条记忆为 stale（rev=%s）", project_id, len(ids), current_rev)
        return [int(i) for i in ids]

    async def mark_used(self, ids: list[int]) -> None:
        """召回后回写使用次数 —— 容量淘汰和"哪些记忆真有用"的唯一依据。"""
        if not ids:
            return
        async with self._db.session() as s:
            async with s.begin():
                await s.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(use_count=Memory.use_count + 1, last_used_at=func.now(3))
                )

    async def expire_capacity(
        self, user_id: int, project_id: int, memory_type: MemoryType, cap: int
    ) -> list[int]:
        """某类型超过上限时，淘汰最久未使用的一批。**返回被淘汰的 id**。

        返回 id 而不是条数：Qdrant 的 payload 还写着 `active`，
        调用方必须拿这些 id 去删点，否则召回过滤会继续把失效记忆放出来。
        """
        async with self._db.session() as s:
            async with s.begin():
                ids = (await s.execute(
                    select(Memory.id)
                    .where(
                        Memory.user_id == user_id,
                        Memory.project_id == project_id,
                        Memory.type == memory_type.value,
                        Memory.status == MemoryStatus.ACTIVE.value,
                    )
                    # **降序**：先保留最近用过的 cap 条，offset 之后才是最久未用的。
                    # 同一毫秒创建的多条必须再按 id 定序，否则淘汰哪条是随机的。
                    .order_by(
                        func.coalesce(Memory.last_used_at, Memory.created_at).desc(),
                        Memory.id.desc(),
                    )
                    .offset(cap)
                    .limit(1000)
                )).scalars().all()

                if not ids:
                    return []
                await s.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(status=MemoryStatus.EXPIRED.value)
                )
        logger.info(
            "projectId=%s 淘汰 %s 条 %s 记忆（上限 %s）",
            project_id, len(ids), memory_type.value, cap,
        )
        return [int(i) for i in ids]

    async def expire_due(self) -> list[int]:
        """到期扫描：`expires_at < now` 的 active 记忆置 expired。返回被淘汰的 id。

        同 `expire_capacity`：调用方必须删点，不能只看 MySQL。
        """
        async with self._db.session() as s:
            async with s.begin():
                ids = (await s.execute(
                    select(Memory.id).where(
                        Memory.status == MemoryStatus.ACTIVE.value,
                        Memory.expires_at.is_not(None),
                        Memory.expires_at < func.now(3),
                    )
                )).scalars().all()
                if not ids:
                    return []
                await s.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(status=MemoryStatus.EXPIRED.value)
                )
        if ids:
            logger.info("到期记忆 %s 条已置 expired", len(ids))
        return [int(i) for i in ids]

    async def purge_project(self, project_id: int) -> int:
        """项目删除：记忆随项目一起清掉（**硬删，这是唯一会删行的路径**）。"""
        async with self._db.session() as s:
            async with s.begin():
                n = (await s.execute(
                    delete(Memory).where(Memory.project_id == project_id)
                )).rowcount
        logger.info("清掉 projectId=%s 的 %s 条记忆", project_id, n)
        return int(n or 0)

    # ==================== 读 ====================

    async def get(self, memory_id: int) -> Memory | None:
        async with self._db.session() as s:
            return await s.get(Memory, memory_id)

    async def list_active(
        self,
        user_id: int,
        project_id: int,
        *,
        types: tuple[MemoryType, ...] | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """冷启动与候选补充用。**只返回 active** —— 失效的一律不出现。"""
        async with self._db.session() as s:
            stmt = select(Memory).where(
                Memory.user_id == user_id,
                Memory.project_id == project_id,
                Memory.status == MemoryStatus.ACTIVE.value,
            )
            if types:
                stmt = stmt.where(Memory.type.in_([t.value for t in types]))
            stmt = stmt.order_by(Memory.updated_at.desc()).limit(limit)
            return list((await s.execute(stmt)).scalars().all())

    async def list_by_subject(
        self, user_id: int, project_id: int, subject_uid: str, *, limit: int = 20
    ) -> list[Memory]:
        """按 subject 精确查（未决续接、同一实体的多条结论）。"""
        async with self._db.session() as s:
            stmt = (
                select(Memory)
                .where(
                    Memory.user_id == user_id,
                    Memory.project_id == project_id,
                    Memory.primary_subject_uid == subject_uid,
                    Memory.status == MemoryStatus.ACTIVE.value,
                )
                .order_by(Memory.updated_at.desc())
                .limit(limit)
            )
            return list((await s.execute(stmt)).scalars().all())


def _apply(row: Memory, draft: MemoryDraft, *, revive: bool) -> None:
    """把 draft 的内容写到已有行上。`graph_rev` 跟随新证据，不是保留旧值。"""
    row.text = normalize_text(draft.text)
    row.subject_uids = list(draft.subject_uids)
    row.primary_subject_uid = draft.primary_subject_uid
    row.graph_rev = draft.graph_rev
    row.confidence = draft.confidence
    # 信任级别只升不降：一条用户确认过的记忆，不该被后来的蒸馏降级
    if draft.trust == MemoryTrust.EXPLICIT:
        row.trust = MemoryTrust.EXPLICIT.value
    if draft.expires_at is not None:
        row.expires_at = draft.expires_at
    if revive:
        row.status = MemoryStatus.ACTIVE.value
