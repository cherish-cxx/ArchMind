"""项目级向量索引：Java 分页拉类 → 拼卡片 → embed → upsert Qdrant。

**是流式的**：一页处理完就写进 Qdrant，不把整个项目攒在内存里。
因此索引期间 Qdrant 会有半成品 —— v1 用 `IndexStore` 里的状态位挡住检索：
只有 `status=ready` 且 rev 匹配时才允许使用向量结果。

幂等：
  - point id = uuid5(projectId:uid)，重跑原地覆盖；
  - 每次索引前先按 projectId 删旧点；
  - 状态与锁都在 Redis（db1），进程重启不会留下永久半成品。
"""

import asyncio
import logging
import time
import uuid

from qdrant_client.models import PointStruct, SparseVector

from app.config import settings
from app.models.tool import ClassNode
from app.store.index_store import IndexStore
from app.tools.embed_client import EmbedClient
from app.tools.java_client import JavaClient
from app.tools.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)

_KIND_LABELS = {"CLASS": "类", "INTERFACE": "接口", "ENUM": "枚举"}


class IndexError(Exception):
    """索引过程中的业务错误（数量不一致、分页失败等）。"""


class StaleGraphError(IndexError):
    """拉取期间图被重建，本轮的 rev 已经失效。"""


class InsufficientCoverage(IndexError):
    """实际写入的类数少于预期太多 —— 这一版不能对外服务。"""


def coverage_of(processed: int, expected: int) -> float:
    """覆盖率。预期为 0（空项目）时算满分：没有类可索引，不存在"缺类"。"""
    return processed / expected if expected else 1.0


def build_card(node: ClassNode) -> tuple[str, str]:
    """返回 (卡片文本, descriptionSource)。

    有描述就用描述；没有时用结构化兜底，保证每个类都能进索引，
    且兜底文本对每个类不同，不会在向量空间里挤成一团。
    """
    description = (node.description or "").strip()
    if description:
        return f"{node.simple_name}：{description}", "LLM"

    kind = _KIND_LABELS.get(node.kind.value, "类")
    text = f"{kind} {node.simple_name}"
    if node.package_name:
        text += f"，位于包 {node.package_name}"
    return text, "FALLBACK"


def chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class Indexer:
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
        # 全局一次只跑一个索引：embed 容器只有 3 核，并发只会互相拖慢
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
            logger.error("索引任务异常退出: %s", exc)

    async def delete_project(self, project_id: int) -> None:
        """项目删除时清掉索引：先取消在跑的任务，再删点与状态。

        与 Java 的 Neo4j 清理同理：不删就会留下搜得到、但项目已不存在的孤儿点。
        """
        task = self._tasks.get(project_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        token = await self._store.acquire_lock(project_id)
        if token is None:
            raise IndexError(f"项目 {project_id} 的索引锁被占用，无法清理")
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
                logger.exception("索引失败 projectId=%s", project_id)
                # **保留 activeRev**：失败的是这一版，不是整个项目。
                # 覆盖掉它会让一个还能用旧索引的项目彻底失去检索能力。
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

        if status.analysis_status != "COMPLETED" or status.pending > 0:
            # 没补齐就不建索引：裸类名卡片会把语义检索的排序带偏
            await self._store.set_state(project_id, {
                "status": "not_ready",
                "activeRev": active_rev,
                "rev": active_rev,
                "analysisStatus": status.analysis_status,
                "pending": status.pending,
            })
            logger.info("项目未就绪，跳过索引 projectId=%s pending=%s",
                        project_id, status.pending)
            return

        rev = status.rev
        started = int(time.time())
        await self._store.set_state(project_id, {
            "status": "indexing",
            "pendingRev": rev,
            "activeRev": active_rev,
            # rev 字段始终表示「对外生效的版本」，检索按它过滤
            "rev": active_rev,
            "describable": status.describable,
            "startedAt": started,
        })

        await self._qdrant.ensure_collection()

        if active_rev is None:
            # 没有任何可保护的版本（这个项目从没成功索引过），先清掉遗留点。
            # 不清的话，上一次失败留下的点会和这一次混在同一个 rev 下，
            # 数量校验永远对不上 —— 而它们本来就没有服务价值。
            # active_rev 有值时**绝不能**走这里：那时旧点是唯一能对外服务的版本。
            await self._qdrant.delete_project(project_id)

        try:
            processed = await self._stream(project_id, rev, status.describable)
        except Exception:
            await self._discard_rev(project_id, rev, active_rev)
            raise

        partial = processed < status.describable
        await self._store.set_state(project_id, {
            "status": "ready",
            "rev": rev,
            "activeRev": rev,
            "pendingRev": None,
            "count": processed,
            "actual": processed,
            "expected": status.describable,
            # 部分覆盖也要能检索，但必须让上层看得见 —— 否则就是静默偏差
            "partial": partial,
            "describable": status.describable,
            "startedAt": started,
            "finishedAt": int(time.time()),
        })

        # ★ 先切换、后清理。顺序反了就会在切换前把还在服务的旧版本删掉。
        try:
            await self._qdrant.delete_other_revs(project_id, rev)
        except Exception:
            logger.warning("清理旧版本索引点失败 projectId=%s rev=%s",
                           project_id, rev, exc_info=True)

        logger.info("索引完成 projectId=%s rev=%s count=%s/%s partial=%s",
                    project_id, rev, processed, status.describable, partial)

    async def _stream(self, project_id: int, rev: int, expected: int) -> int:
        """流式拉取并写入，返回实际写入数。

        与旧版的差别：**不再先删旧点**。新点带新 rev 写进去，
        旧版本在 activeRev 切换之前一直可用。
        """
        cursor: str | None = None
        processed = 0
        pages = 0
        while True:
            page = await self._java.classes_page(
                project_id, cursor, settings.index_page_size
            )
            if not page.ok or page.data is None:
                raise IndexError(f"classes/page 失败: {page.note}")
            data = page.data
            if data.rev != rev:
                raise StaleGraphError(f"拉取期间图被重建: rev {rev} -> {data.rev}")
            if not data.items:
                break

            await self._index_page(project_id, rev, data.items)
            processed += len(data.items)
            pages += 1
            logger.info("索引进度 projectId=%s page=%s processed=%s/%s",
                        project_id, pages, processed, expected)

            cursor = data.next_cursor
            if cursor is None:
                break

        coverage = coverage_of(processed, expected)
        if coverage < settings.index_min_coverage:
            raise InsufficientCoverage(
                f"索引覆盖率不足: 写入 {processed}, 预期 {expected} "
                f"({coverage:.1%} < {settings.index_min_coverage:.0%})"
            )
        qdrant_count = await self._qdrant.count_project(project_id, rev)
        if qdrant_count != processed:
            raise IndexError(
                f"Qdrant 点数不一致: {qdrant_count} != {processed}"
            )
        return processed

    async def _discard_rev(
        self, project_id: int, rev: int, active_rev: int | None
    ) -> None:
        """删掉失败版本留下的点（尽力而为：删不掉也会被 activeRev 的过滤挡住）。

        ⚠️ **绝不删正在服务的那一版**。rev 与 activeRev 相同时（同一版本重跑），
        清掉它等于把对外可用的数据亲手删了 —— 宁可留一堆会被过滤挡住的半成品。
        """
        if rev == active_rev:
            logger.warning(
                "失败版本的 rev=%s 正是当前生效版本，跳过清理 projectId=%s",
                rev, project_id,
            )
            return
        try:
            await self._qdrant.delete_rev(project_id, rev)
        except Exception:
            logger.warning("清理失败版本的索引点失败 projectId=%s rev=%s",
                           project_id, rev, exc_info=True)

    async def _index_page(self, project_id: int, rev: int, items: list[ClassNode]) -> None:
        entries = [(item, *build_card(item)) for item in items]
        for start in range(0, len(entries), settings.index_embed_batch):
            batch = entries[start:start + settings.index_embed_batch]
            vectors = await self._embed.embed([text for _, text, _ in batch])
            points = [
                self._point(project_id, rev, item, source, dense, sparse)
                for (item, _, source), (dense, sparse) in zip(batch, vectors)
            ]
            for chunk in chunked(points, settings.index_upsert_batch):
                await self._qdrant.upsert(chunk)

    @staticmethod
    def _point(
        project_id: int,
        rev: int,
        item: ClassNode,
        source: str,
        dense: list[float],
        sparse: dict[int, float],
    ) -> PointStruct:
        indices = sorted(sparse)
        return PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"archmind:{project_id}:{item.qualified_name}")),
            vector={
                "dense": dense,
                "sparse": SparseVector(
                    indices=indices,
                    values=[sparse[i] for i in indices],
                ),
            },
            payload={
                "projectId": project_id,
                "uid": item.qualified_name,
                "simpleName": item.simple_name,
                "packageName": item.package_name,
                # kind 不进向量（卡片文本里没有「类/接口」字样），只给消歧时判断用：
                # 用户问「怎么实现的」时，OrderService 和 OrderServiceImpl 靠名字分不出来。
                "kind": item.kind.value,
                "description": item.description,
                "descriptionSource": source,
                "filePath": item.file_path,
                "rev": rev,
            },
        )
