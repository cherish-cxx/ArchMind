"""能力索引：Java 分页拉能力卡片 → 拼文本 → embed → 写入第二个集合。

与类的 `Indexer` 是同一条链路，差异只有四点：
  - 未就绪只看 `analysis_status == "COMPLETED"`（能力描述在分析成功前已写完，
    不需要等类描述补齐的 `pending`）；
  - 描述为空的能力**跳过**（裸能力名会把语义检索带偏），但计入 `expected`，
    并在状态里体现 `partial`；
  - 数量校验要求 `count_project(project_id, rev) == count`（不做百分比门槛）；
  - 独立集合 + 独立 Redis 状态命名空间，能力索引失败不影响类索引。

rev 闸门与失败隔离照抄 `Indexer`：只有全部写完才把 `activeRev` 切到新版本；
失败时只丢弃这一版，**绝不清理正在服务的旧版本**。
"""

import asyncio
import logging
import time
import uuid

from qdrant_client.models import PointStruct, SparseVector

from app.config import settings
from app.core.indexer import IndexError, StaleGraphError, chunked
from app.models.capability import CapabilityCard
from app.store.index_store import IndexStore
from app.tools.embed_client import EmbedClient
from app.tools.java_client import JavaClient
from app.tools.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)

#: 卡片文本里最多带几个成员类。再多会稀释描述，也拉长 embed 输入。
_MAX_CARD_MEMBERS = 8

_ROLE_LABELS = {
    "entry": "入口",
    "business": "业务",
    "domain-model": "领域模型",
    "infrastructure": "基础设施",
    "external": "外部依赖",
}


def role_label(role: str | None) -> str:
    """类角色 → 中文标签；认不出的统一叫「其他」。"""
    key = (role or "").lower().replace("_", "-")
    return _ROLE_LABELS.get(key, "其他")


def capability_card_text(card: CapabilityCard) -> str:
    """能力卡片的嵌入文本。描述为空返回 `""`，调用方据此跳过写入。"""
    desc = (card.description or "").strip()
    if not desc:
        return ""
    head = f"{card.area_name or card.area_uid} / {card.name or card.uid}"
    entry = card.entry_kind or "入口"
    member_text = "、".join(
        f"{m.uid.rsplit('.', 1)[-1]}（{role_label(m.role)}）"
        for m in card.members[:_MAX_CARD_MEMBERS]
    )
    text = f"{head}：{desc}。入口：{entry}。"
    return text + (f"成员：{member_text}" if member_text else "")


class CapabilityIndexer:
    def __init__(
        self,
        java: JavaClient,
        embed: EmbedClient,
        qdrant: QdrantStore,
        store: IndexStore,
    ) -> None:
        self._java = java
        self._embed = embed
        self._qdrant = qdrant
        self._store = store
        # 与类索引共用的资源约束：embed 容器只有 3 核，一次只跑一个索引任务
        self._semaphore = asyncio.Semaphore(1)
        # 持有 task 引用，避免被 GC；按 projectId 记，删除项目时可以定向取消
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(self, project_id: int, rev: int | None = None) -> dict:
        """入口只做「要不要跑」的判断，真正的索引放到后台任务。"""
        state = await self._store.get_state(project_id)
        if rev is not None and state and state.get("status") == "ready" \
                and state.get("rev") == rev:
            return {"status": "up_to_date", "projectId": project_id, "rev": rev}

        token = await self._store.acquire_lock(project_id)
        if token is None:
            return {"status": "running", "projectId": project_id}

        task = asyncio.create_task(self._run(project_id, token))
        self._tasks[project_id] = task
        task.add_done_callback(lambda t: self._on_task_done(project_id, t))
        return {"status": "accepted", "projectId": project_id}

    def _on_task_done(self, project_id: int, task: asyncio.Task) -> None:
        self._tasks.pop(project_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # _run 已经落过 failed 状态；这里只负责把漏网的异常读出来，避免
            # "Task exception was never retrieved" 静默吞掉
            logger.error("能力索引任务异常退出: %s", exc)

    async def delete_project(self, project_id: int) -> None:
        """项目删除时清掉能力索引：先取消在跑的任务，再删点与状态。"""
        task = self._tasks.get(project_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        token = await self._store.acquire_lock(project_id)
        if token is None:
            raise IndexError(f"项目 {project_id} 的能力索引锁被占用，无法清理")
        try:
            await self._qdrant.delete_project(project_id)
        finally:
            await self._store.clear_state(project_id)
            await self._store.release_lock(project_id, token)

    async def aclose(self) -> None:
        """关停时取消后台索引任务；项目锁会随 TTL 过期，不会永久卡死。"""
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, project_id: int, token: str) -> None:
        async with self._semaphore:
            try:
                await self._index(project_id)
            except Exception as e:
                logger.exception("能力索引失败 projectId=%s", project_id)
                # **保留 activeRev**：失败的是这一版，不是整个项目。
                # 覆盖掉它会让一个还能用旧能力索引的项目彻底失去检索能力。
                previous = await self._store.get_state(project_id) or {}
                await self._store.set_state(project_id, {
                    **previous,
                    "status": "failed",
                    "pendingRev": None,
                    "error": str(e)[:500],
                })
            finally:
                await self._store.release_lock(project_id, token)

    async def _index(self, project_id: int) -> None:
        status = await self._java.describe_status(project_id)
        previous = await self._store.get_state(project_id) or {}
        active_rev = previous.get("activeRev")

        # 能力卡片在分析成功时就已完整；不要求类描述补齐的 pending == 0
        if status.analysis_status != "COMPLETED":
            await self._store.set_state(project_id, {
                "status": "not_ready",
                "activeRev": active_rev,
                "rev": active_rev,
                "analysisStatus": status.analysis_status,
            })
            logger.info("项目未就绪，跳过能力索引 projectId=%s analysisStatus=%s",
                        project_id, status.analysis_status)
            return

        rev = status.rev
        started = int(time.time())
        await self._store.set_state(project_id, {
            "status": "indexing",
            "pendingRev": rev,
            "activeRev": active_rev,
            # rev 字段始终表示「对外生效的版本」，检索按它过滤
            "rev": active_rev,
            "startedAt": started,
        })

        await self._qdrant.ensure_collection()

        if active_rev is None:
            # 从没成功索引过：先清掉遗留点，否则旧点会和这次混在同一 rev 下，
            # 数量校验永远对不上。active_rev 有值时**绝不能**走这里 ——
            # 那时旧点是唯一能对外服务的版本。
            await self._qdrant.delete_project(project_id)

        try:
            expected, count = await self._stream(project_id, rev)
        except Exception:
            await self._discard_rev(project_id, rev, active_rev)
            raise

        # 能力数量小，少一个就是少一个，不设百分比门槛
        partial = count < expected
        await self._store.set_state(project_id, {
            "status": "ready",
            "rev": rev,
            "activeRev": rev,
            "pendingRev": None,
            "count": count,
            "actual": count,
            "expected": expected,
            # 空描述的能力没有向量：部分覆盖仍可检索，但必须让上层看得见
            "partial": partial,
            "startedAt": started,
            "finishedAt": int(time.time()),
        })

        # ★ 先切换、后清理。顺序反了就会在切换前把还在服务的旧版本删掉。
        try:
            await self._qdrant.delete_other_revs(project_id, rev)
        except Exception:
            logger.warning("清理旧版本能力索引点失败 projectId=%s rev=%s",
                           project_id, rev, exc_info=True)

        logger.info("能力索引完成 projectId=%s rev=%s count=%s/%s partial=%s",
                    project_id, rev, count, expected, partial)

    async def _stream(self, project_id: int, rev: int) -> tuple[int, int]:
        """流式拉取并写入，返回 (expected, count)。

        expected 计所有看到的能力卡片，count 计实际写入（跳过空描述）。
        """
        cursor: str | None = None
        expected = 0
        count = 0
        pages = 0
        while True:
            page = await self._java.capability_cards_page(
                project_id, cursor, settings.index_page_size
            )
            if not page.ok or page.data is None:
                raise IndexError(f"capabilities/cards 失败: {page.note}")
            data = page.data
            if data.rev != rev:
                raise StaleGraphError(f"拉取期间图被重建: rev {rev} -> {data.rev}")
            if not data.items:
                break

            expected += len(data.items)
            count += await self._index_page(project_id, rev, data.items)
            pages += 1
            logger.info("能力索引进度 projectId=%s page=%s written=%s/%s",
                        project_id, pages, count, expected)

            cursor = data.next_cursor
            if cursor is None:
                break

        qdrant_count = await self._qdrant.count_project(project_id, rev)
        if qdrant_count != count:
            raise IndexError(
                f"Qdrant 能力点数不一致: {qdrant_count} != {count}"
            )
        return expected, count

    async def _discard_rev(
        self, project_id: int, rev: int, active_rev: int | None
    ) -> None:
        """删掉失败版本留下的点（尽力而为：删不掉也会被 activeRev 的过滤挡住）。

        ⚠️ **绝不删正在服务的那一版**。rev 与 activeRev 相同时（同一版本重跑），
        清掉它等于把对外可用的数据亲手删了。
        """
        if rev == active_rev:
            logger.warning(
                "失败版本的能力索引 rev=%s 正是当前生效版本，跳过清理 projectId=%s",
                rev, project_id,
            )
            return
        try:
            await self._qdrant.delete_rev(project_id, rev)
        except Exception:
            logger.warning("清理失败版本的能力索引点失败 projectId=%s rev=%s",
                           project_id, rev, exc_info=True)

    async def _index_page(
        self, project_id: int, rev: int, items: list[CapabilityCard]
    ) -> int:
        """写入一页能力卡片，返回实际写入数。描述为空的卡片跳过。"""
        entries = [(card, text) for card in items if (text := capability_card_text(card))]
        written = 0
        for start in range(0, len(entries), settings.index_embed_batch):
            batch = entries[start:start + settings.index_embed_batch]
            vectors = await self._embed.embed([text for _, text in batch])
            points = [
                self._point(project_id, rev, card, dense, sparse)
                for (card, _), (dense, sparse) in zip(batch, vectors)
            ]
            for chunk in chunked(points, settings.index_upsert_batch):
                await self._qdrant.upsert(chunk)
            written += len(points)
        return written

    @staticmethod
    def _point(
        project_id: int,
        rev: int,
        card: CapabilityCard,
        dense: list[float],
        sparse: dict[int, float],
    ) -> PointStruct:
        """能力点。uid 是不透明字符串（可能含空格/斜杠/井号），原样进 id 与 payload。"""
        indices = sorted(sparse)
        return PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"archmind:cap:{project_id}:{card.uid}")),
            vector={
                "dense": dense,
                "sparse": SparseVector(
                    indices=indices,
                    values=[sparse[i] for i in indices],
                ),
            },
            payload={
                "projectId": project_id,
                "uid": card.uid,
                "name": card.name,
                "description": card.description,
                "areaUid": card.area_uid,
                "areaName": card.area_name,
                "areaDescription": card.area_description,
                "entryUid": card.entry_uid,
                "entryKind": card.entry_kind,
                "memberCount": card.member_count,
                "members": [m.model_dump(by_alias=True) for m in card.members],
                "rev": rev,
            },
        )
