"""记忆的写入与召回（R5 阶段 1）。

两条路径各一个入口：

- `MemoryService.remember` —— 用户显式"记住…"时写入；**语言触发**的判定在
  `explicit_draft`，是纯函数，可以脱离数据库和 LLM 测。
- `MemoryService.recall` —— 冷启动时按第一问召回。

**记忆永远是附加路径**：embed / Qdrant / MySQL 任何一步失败都退化成"这轮没有记忆"，
绝不让 `/ask` 多一个新的 500 来源。

写入侧的硬约束（B5）：`subject_uids` 必须是当轮 `evidence_json` 的子集。
这里只做"从 evidence 里挑"，不做"造一个 uid"—— 挑不到就不写。
"""

import asyncio
import logging
import re

from qdrant_client.models import PointStruct, SparseVector

from app.config import settings
from app.models.memory import (
    MAX_TEXT_CHARS,
    MemoryDraft,
    MemoryHit,
    MemoryTrust,
    MemoryType,
    normalize_text,
)
from app.store.memory_repo import MemoryRepo
from app.tools.embed_client import EmbedClient, EmbedError
from app.tools.memory_store import MemoryStore, point_id

logger = logging.getLogger(__name__)

#: 触发词。只做"用户让我记"这一类，不做"这句话看起来值得记"——
#: 后者属于蒸馏，不让模型在回答时顺手写记忆。
_EXPLICIT_MARKERS = ("记住", "记一下", "记下来", "备注一下", "别忘了")
_TRIM = "：:，,。.、;；!！?？ \t\r\n"

#: 注入 prompt 的条数上限。再多就不是"提示"而是"新的一份上下文"。
PROMPT_MEMORY_LIMIT = 5

#: 指代表达。`(?<!其)它` 是为了不把「其他」当指代；「这个」故意不收 ——
#: 「这个项目」在概览问题里太常见，收了等于每轮都查记忆。
_REFERRAL = re.compile(r"(?<!其)它|那个|上次|刚才|之前|接着|继续")

#: 明确指向"窗口之外"的说法。这几类不要求窗口为空 —— 它们本来就在说以前的事。
_CROSS_SESSION = re.compile(r"上次|之前|刚才")


def has_referral(message: str) -> bool:
    return bool(_REFERRAL.search(message))


def has_cross_session_reference(message: str) -> bool:
    return bool(_CROSS_SESSION.search(message))


def should_recall(
    *,
    message: str,
    turns: int,
    last_outcome: str | None,
    has_anchor: bool,
    candidate_count: int,
    window_entities: set[str],
) -> str | None:
    """这一轮要不要查记忆。返回触发原因（写进 steps），`None` 表示不查。

    顺序即优先级：冷启动 → 上一轮澄清 → 指代无着落 → 类检索空手。
    **有锚点一律不查** —— 和 `_retrieve` 同一条规则：明确的指代不需要再猜。

    记忆是附加路径，这里宁多勿漏：漏掉一条纠正的代价，远大于多花一次召回。
    """
    if has_anchor:
        return None
    if turns == 0:
        return "cold_start"
    if last_outcome == "CLARIFY":
        return "after_clarify"
    # 「上次 / 之前」本身就在说窗口之外，不用等窗口变空
    if has_cross_session_reference(message):
        return "cross_session_reference"
    if has_referral(message) and not window_entities:
        return "unresolved_reference"
    if candidate_count == 0:
        return "no_candidates"
    return None


def simple_name(uid: str) -> str:
    head = uid.split("#", 1)[0]
    return head.rsplit(".", 1)[-1]


def detect_explicit(message: str) -> str | None:
    """命中"记住…"时返回要去记的正文，未命中返回 None。

    支持前后两种位置：`记住：X` 取 `X`；`X，记住` 取 `X`。
    返回空串是合法的 —— 它表示"记住这个结论"，正文由回答的 headline 兜底。
    """
    for marker in _EXPLICIT_MARKERS:
        idx = message.find(marker)
        if idx < 0:
            continue
        rest = message[idx + len(marker):].strip(_TRIM)
        if len(rest) < 2:
            rest = message[:idx].strip(_TRIM)
        return rest
    return None


def pick_subjects(
    message: str, evidence_uids: list[str], focus_uid: str | None
) -> tuple[str, ...]:
    """从**本轮 evidence** 里挑出这条记忆指向的实体。

    优先级：消息里点名的类 → 当前焦点（且它在 evidence 里）→ 不写。
    不写比瞎指好：一条指错类的记忆会在下一次会话里被当成事实注入。
    """
    matched = tuple(
        uid for uid in evidence_uids if simple_name(uid) and simple_name(uid) in message
    )
    if matched:
        return matched
    if focus_uid and focus_uid in evidence_uids:
        return (focus_uid,)
    return ()


def _truncate(text: str) -> str:
    """超过单条上限时，优先在句末截断，找不到句末才硬截。"""
    head = text[:MAX_TEXT_CHARS]
    for sep in "。！？；;":
        idx = head.rfind(sep)
        if idx >= 20:
            return head[: idx + 1]
    return head[: MAX_TEXT_CHARS - 1] + "…"


def explicit_draft(
    *,
    message: str,
    evidence_uids: list[str],
    focus_uid: str | None,
    graph_rev: int,
    conversation_id: str,
    turn_no: int,
    headline: str | None = None,
) -> MemoryDraft | None:
    """用户显式记忆 → `MemoryDraft`；不满足条件返回 None。

    三个不写的条件：没有触发词、没有 evidence、挑不出 subject。
    """
    rest = detect_explicit(message)
    if rest is None:
        return None

    text = normalize_text(rest if len(rest) >= 2 else (headline or ""))
    if len(text) < 2:
        return None
    if len(text) > MAX_TEXT_CHARS:
        text = _truncate(text)

    subjects = pick_subjects(message, evidence_uids, focus_uid)
    if not subjects:
        return None

    return MemoryDraft(
        type=MemoryType.FACT,
        text=text,
        subject_uids=subjects,
        graph_rev=graph_rev,
        trust=MemoryTrust.EXPLICIT,
        source_conversation_id=conversation_id,
        source_turn_no=turn_no,
    )


def render_memories(hits: list[MemoryHit] | None) -> str:
    """召回结果 → prompt 里的一段。空列表返回空串（保证没记忆时 prompt 不变）。"""
    if not hits:
        return ""
    lines = ["=== 记忆参考（过去会话的结论，未查证，不是本轮证据）==="]
    for hit in hits[:PROMPT_MEMORY_LIMIT]:
        lines.append(f"- [{hit.label}] {hit.text}")
    return "\n".join(lines)


class MemoryService:
    def __init__(
        self, repo: MemoryRepo, store: MemoryStore, embed: EmbedClient
    ) -> None:
        self._repo = repo
        self._store = store
        self._embed = embed

    @property
    def enabled(self) -> bool:
        """总开关。关掉时连 steps 都不该多一条 —— 审计里不该出现空跑的召回。"""
        return settings.memory_enabled

    # ==================== 写 ====================

    async def remember(
        self, user_id: int, project_id: int, draft: MemoryDraft
    ) -> tuple[object, str] | None:
        """写一条记忆并建索引。返回 `(行, 动作)`；未启用时返回 None。

        索引失败**不回滚 MySQL**：行是真相，索引可以重建。
        """
        if not settings.memory_enabled:
            return None
        row, action = await self._repo.upsert(user_id, project_id, draft)
        if action != "ignored":
            await self._index(row)
        return row, action

    async def _index(self, row) -> None:
        try:
            vectors = await self._embed.embed(
                [row.text], timeout=settings.memory_recall_timeout
            )
        except EmbedError as e:
            logger.warning("记忆 %s 已写入，但取向量失败（未建索引）：%s", row.id, e)
            return
        if not vectors:
            return
        dense, sparse = vectors[0]
        indices = sorted(sparse)
        try:
            await self._store.upsert([PointStruct(
                id=point_id(row.id),
                vector={
                    "dense": dense,
                    "sparse": SparseVector(
                        indices=indices, values=[sparse[i] for i in indices]
                    ),
                },
                payload={
                    "memoryId": row.id,
                    "userId": row.user_id,
                    "projectId": row.project_id,
                    "type": row.type,
                    "status": row.status,
                    "primarySubjectUid": row.primary_subject_uid,
                    "subjectUids": list(row.subject_uids or []),
                    "graphRev": row.graph_rev,
                    "text": row.text,
                },
            )])
        except Exception as e:  # noqa: BLE001 —— 索引是附加路径
            logger.warning("记忆 %s 写入 Qdrant 失败：%s", row.id, e)

    async def enforce_capacity(self, user_id: int, project_id: int) -> None:
        """按类型上限淘汰。失败只记日志 —— 容量是卫生，不是正确性。

        **淘汰必须删点**：Qdrant 的 payload 仍是 `status=active`，
        只改 MySQL 的话召回过滤会把刚淘汰的记忆继续放出来。
        """
        if not settings.memory_enabled:
            return
        try:
            expired = await self._repo.expire_capacity(
                user_id, project_id, MemoryType.FACT, settings.memory_fact_cap
            )
            expired += await self._repo.expire_capacity(
                user_id, project_id, MemoryType.OPEN_QUESTION,
                settings.memory_open_cap,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆容量淘汰失败：%s", e)
            return
        if expired:
            await self._drop_points(expired)

    async def _drop_points(self, memory_ids: list[int]) -> None:
        try:
            await self._store.delete_ids(memory_ids)
        except Exception as e:  # noqa: BLE001 —— 删点失败最多留下一条查不到的脏点
            logger.warning("删除失效记忆索引失败 ids=%s：%s", memory_ids[:10], e)

    async def mark_stale(self, project_id: int, current_rev: int) -> int:
        """图谱翻版后把旧 rev 的事实置失效，并删掉索引点。返回处理条数。

        **调用方还没有接上**：重新分析成功后的 Java 网关还没调它。
        在那之前，跨 rev 的失效只能靠这里手动触发（评测脚本就是这么做的）。
        """
        if not settings.memory_enabled:
            return 0
        try:
            ids = await self._repo.mark_stale(project_id, current_rev)
        except Exception as e:  # noqa: BLE001
            logger.warning("标记记忆 stale 失败 project=%s：%s", project_id, e)
            return 0
        if ids:
            await self._drop_points(ids)
        return len(ids)

    # ==================== 读 ====================

    async def recall(
        self, user_id: int, project_id: int, query: str, *, k: int | None = None
    ) -> list[MemoryHit]:
        """按一句话召回记忆。任何一步失败都返回空列表，绝不抛到主链路。"""
        if not settings.memory_enabled or not query.strip():
            return []
        try:
            vectors = await self._embed.embed(
                [query], timeout=settings.memory_recall_timeout
            )
        except EmbedError as e:
            logger.warning("记忆召回取向量失败，本轮不注入：%s", e)
            return []
        if not vectors:
            return []
        dense, sparse = vectors[0]

        try:
            points = await asyncio.wait_for(
                self._store.query(user_id, project_id, dense, sparse, limit=k),
                timeout=settings.memory_recall_timeout,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆召回查询失败，本轮不注入：%s", e)
            return []

        hits = [_to_hit(p) for p in points]
        ids = [h.id for h in hits if h.id]
        if ids:
            try:
                await self._repo.mark_used(ids)
            except Exception as e:  # noqa: BLE001 —— 统计失败不影响本轮
                logger.warning("回写记忆使用次数失败：%s", e)
        return hits


def _to_hit(point) -> MemoryHit:
    payload = point.payload or {}
    raw_type = payload.get("type") or MemoryType.FACT.value
    try:
        memory_type = MemoryType(raw_type)
    except ValueError:
        memory_type = MemoryType.FACT
    return MemoryHit(
        id=int(payload.get("memoryId") or 0),
        text=str(payload.get("text") or ""),
        type=memory_type,
        subject_uids=tuple(payload.get("subjectUids") or ()),
        score=float(point.score or 0.0),
    )
