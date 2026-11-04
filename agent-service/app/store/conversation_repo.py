"""MySQL 侧的会话仓库 —— 发号、事务、级联删除（R5）。

**这里只做数据库的事**：不调 LLM、不碰 Redis、不知道意图是什么。
上层 `ConversationStore` 负责"MySQL 成功之后才写 Redis"。

## 发号

`agent_conversation.turn_count` 是唯一发号来源。整个 `commit_turn` 在一个事务里：

    锁住会话行 → turn_no = turn_count + 1 → 插 turn → 覆盖 context

任何一步失败都回滚，计数器也退回去 —— 所以 `turn_no` 连续、不跳号、不重号。
唯一索引 `(conversation_id, turn_no)` 是最后一道保险。
"""

import dataclasses
import json
import logging
from typing import Any

from sqlalchemy import and_, delete, func, select, text, update

from app.config import settings
from app.models.context import AgentContext, ChatMessage, MessageRole
from app.store.db import Database
from app.store.orm import ContextSnapshot, Conversation, Turn

logger = logging.getLogger(__name__)


class ConversationForbidden(Exception):
    """会话存在，但不属于这个 user + project。"""


class ConversationRepo:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or Database()
        self.window_max_messages = settings.message_window_max_messages
        self.window_max_chars = settings.message_window_max_chars
        self.max_payload_bytes = settings.agent_turn_max_payload_bytes

    async def aclose(self) -> None:
        await self._db.aclose()

    # ==================== 读 ====================

    async def load_context(
        self, user_id: int, project_id: int, conversation_id: str
    ) -> AgentContext | None:
        """从 `agent_context.state_json` 读回快照。租户过滤在 SQL 里，不在 Python 里。"""
        async with self._db.session() as s:
            row = (await s.execute(
                select(ContextSnapshot.state_json).where(
                    ContextSnapshot.conversation_id == conversation_id,
                    ContextSnapshot.user_id == user_id,
                    ContextSnapshot.project_id == project_id,
                )
            )).scalar_one_or_none()
        if row is None:
            return None
        try:
            return AgentContext.model_validate(row)
        except Exception as e:  # noqa: BLE001 —— 坏了就当不存在，由上层重建
            logger.warning(
                "agent_context 反序列化失败，按不存在处理 cid=%s: %s", conversation_id, e
            )
            return None

    async def load_turn_count(
        self, user_id: int, project_id: int, conversation_id: str
    ) -> int:
        async with self._db.session() as s:
            n = (await s.execute(
                select(Conversation.turn_count).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.project_id == project_id,
                )
            )).scalar_one_or_none()
        return int(n or 0)

    async def load_recent_messages(        self, user_id: int, project_id: int, conversation_id: str, limit_turns: int = 8
    ) -> list[dict[str, Any]]:
        """冷加载时重建窗口用：按 turn_no 正序取最近 N 轮，拼接 transcript。"""
        async with self._db.session() as s:
            rows = (await s.execute(
                select(Turn.transcript_json).where(
                    Turn.conversation_id == conversation_id,
                    Turn.user_id == user_id,
                    Turn.project_id == project_id,
                    Turn.status == "ACTIVE",
                ).order_by(Turn.turn_no.desc()).limit(limit_turns)
            )).scalars().all()
        messages: list[dict[str, Any]] = []
        for chunk in reversed(rows):
            messages.extend(chunk or [])
        return messages

    # ==================== 记忆蒸馏（R5 阶段 3）====================

    async def idle_conversations_for_distill(
        self, idle_minutes: int, limit: int = 10, max_age_days: int = 7
    ) -> list[tuple[str, int, int]]:
        """找"已经空闲、且还有未蒸馏回合"的会话。

        空闲判定在 **SQL 里**用 `NOW()` 做：`created_at` 由 MySQL 服务器时钟写入，
        在 Python 里算截止时间会遇到时区差（本机 UTC vs 服务器本地时间）。

        `max_age_days` 是**下限**：首次打开开关时，不该把几个月前的历史全部蒸一遍，
        那会在一夜之间烧掉大量 LLM 调用。
        """
        cutoff = func.date_sub(
            func.now(3), text(f"INTERVAL {int(idle_minutes)} MINUTE")
        )
        floor = func.date_sub(
            func.now(3), text(f"INTERVAL {int(max_age_days)} DAY")
        )
        async with self._db.session() as s:
            rows = (await s.execute(
                select(Turn.conversation_id, Turn.user_id, Turn.project_id)
                .where(Turn.memory_distilled_at.is_(None), Turn.status == "ACTIVE")
                .group_by(Turn.conversation_id, Turn.user_id, Turn.project_id)
                .having(and_(
                    func.max(Turn.created_at) < cutoff,
                    func.max(Turn.created_at) > floor,
                ))
                .order_by(func.max(Turn.created_at))
                .limit(limit)
            )).all()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    async def load_undistilled_turns(
        self, user_id: int, project_id: int, conversation_id: str, limit: int = 30
    ) -> list[Turn]:
        async with self._db.session() as s:
            rows = (await s.execute(
                select(Turn)
                .where(
                    Turn.conversation_id == conversation_id,
                    Turn.user_id == user_id,
                    Turn.project_id == project_id,
                    Turn.memory_distilled_at.is_(None),
                    Turn.status == "ACTIVE",
                )
                .order_by(Turn.turn_no)
                .limit(limit)
            )).scalars().all()
        return list(rows)

    async def mark_turns_distilled(self, ids: list[int]) -> int:
        """标记已蒸过。**只在这一轮处理完之后调用** —— 失败不标，下次重试。"""
        if not ids:
            return 0
        async with self._db.session() as s:
            async with s.begin():
                n = (await s.execute(
                    update(Turn)
                    .where(Turn.id.in_(ids))
                    .values(memory_distilled_at=func.now(3))
                )).rowcount
        return int(n or 0)

    # ==================== 写 ====================

    async def commit_turn(
        self, ctx: AgentContext, user_id: int, project_id: int
    ) -> int:
        """一轮落库。返回权威的 `turn_no`。

        失败抛异常，**由上层决定是否写 Redis**（方案 A：不写，Redis 保留上一轮）。
        """
        delta = ctx.messages[ctx.turn_start:]
        payload = build_turn_payload(ctx, delta, self.max_payload_bytes)

        async with self._db.session() as s:
            async with s.begin():
                conv = (await s.execute(
                    select(Conversation)
                    .where(Conversation.id == ctx.conversation_id)
                    .with_for_update()
                )).scalar_one_or_none()

                if conv is not None and (
                    conv.user_id != user_id or conv.project_id != project_id
                ):
                    raise ConversationForbidden(ctx.conversation_id)

                if conv is None:
                    conv = Conversation(
                        id=ctx.conversation_id,
                        user_id=user_id,
                        project_id=project_id,
                        title=_title(ctx),
                        turn_count=0,
                        graph_rev_at_start=payload["graph_rev"],
                        status="ACTIVE",
                    )
                    s.add(conv)
                    await s.flush()          # PK 冲突会在这里炸，交给上层
                    conv.turn_count = 1
                    turn_no = 1
                else:
                    conv.turn_count += 1
                    turn_no = conv.turn_count

                s.add(Turn(
                    conversation_id=ctx.conversation_id,
                    user_id=user_id,
                    project_id=project_id,
                    turn_no=turn_no,
                    **payload,
                ))

                # 权威值必须在序列化**之前**回填：快照里的 user_id / budget.turns
                # 是给下一次 load 用的，晚一步就永远是上一轮的旧值。
                ctx.user_id = user_id
                ctx.budget.turns = turn_no
                state = ctx.model_dump(mode="json", by_alias=True)
                snap = await s.get(ContextSnapshot, ctx.conversation_id)
                if snap is None:
                    s.add(ContextSnapshot(
                        conversation_id=ctx.conversation_id,
                        user_id=user_id,
                        project_id=project_id,
                        state_json=state,
                        model_version=ctx.version,
                    ))
                elif snap.user_id != user_id or snap.project_id != project_id:
                    raise ConversationForbidden(ctx.conversation_id)
                else:
                    snap.state_json = state
                    snap.model_version = ctx.version
                    snap.updated_at = func.now(3)

        return turn_no

    async def purge_project(self, project_id: int) -> int:
        """项目删除：级联清掉会话 / 流水 / 快照。返回删掉的会话数。"""
        async with self._db.session() as s:
            async with s.begin():
                n = (await s.execute(
                    select(func.count()).select_from(Conversation)
                    .where(Conversation.project_id == project_id)
                )).scalar_one()
                # FK ON DELETE CASCADE 带走 agent_turn / agent_context
                await s.execute(
                    delete(Conversation).where(Conversation.project_id == project_id)
                )
        logger.info("清掉 projectId=%s 的 %d 个会话", project_id, n)
        return int(n)

    async def mark_stale(self, project_id: int, current_rev: int) -> int:
        """重新分析后：旧 rev 的记录保留但标 STALE，不删。"""
        async with self._db.session() as s:
            async with s.begin():
                n = (await s.execute(
                    update(Turn)
                    .where(Turn.project_id == project_id, Turn.graph_rev != current_rev)
                    .values(status="STALE")
                )).rowcount
                await s.execute(
                    update(Conversation)
                    .where(Conversation.project_id == project_id,
                           Conversation.graph_rev_at_start != current_rev)
                    .values(status="STALE")
                )
        logger.info("projectId=%s 标记 %s 条旧回合为 STALE（rev=%s）", project_id, n, current_rev)
        return int(n or 0)


# ==================== 行构造 ====================


def build_turn_payload(
    ctx: AgentContext, delta: list[ChatMessage], max_bytes: int
) -> dict[str, Any]:
    """把 `ctx.turn` + 本轮 delta 变成 `agent_turn` 的列值。

    **这是"写入路径直接从 ctx 读"的落点。**
    """
    turn = ctx.turn
    transcript = [_dump(m) for m in delta]
    tool_calls = _dump_tool_calls(turn.tool_results)
    sections = [_dump(s) for s in turn.sections]
    citations = [_dump(c) for c in turn.citations]
    ui_commands = [_dump(c) for c in turn.ui_commands]
    steps = [_dump(s) for s in turn.steps]
    graph = _dump(turn.graph)
    retrieval = _dump_retrieval(turn.retrieval)

    graph_rev = 0
    if turn.tool_results:
        graph_rev = turn.tool_results[-1].envelope.graph_rev

    payload: dict[str, Any] = {
        "transcript_json": transcript,
        "llm_calls_json": turn.llm_calls,
        "question": turn.question,
        "rewritten_query": turn.rewritten_query,
        "focus_before": turn.focus_before,
        "method_uid": turn.method_uid,
        # 六选一已删除，新行不再有意图；列保留给历史数据。
        "intent": None,
        "confidence": turn.confidence,
        "degraded": turn.degraded,
        "outcome": turn.outcome,
        "retrieval_json": retrieval,
        "tool_calls_json": tool_calls,
        "evidence_json": turn.evidence,
        "answer_message": turn.answer_message,
        "headline": turn.headline,
        "sections_json": sections,
        "citations_json": citations,
        "ui_commands_json": ui_commands,
        "graph_patch_json": graph,
        "steps_json": steps,
        "graph_rev": graph_rev,
        "latency_ms": turn.latency_ms,
        "truncated": False,
        "status": "ACTIVE",
    }
    size = _size(payload)
    if size > max_bytes:
        # 超上限：只丢工具返回的 data 体，其余（转录、引用、步骤）全部保留
        payload["tool_calls_json"] = [
            {**t, "data": None} for t in payload["tool_calls_json"]
        ]
        payload["truncated"] = True
        size = _size(payload)
        logger.warning(
            "单轮 payload 超限，已裁掉工具返回体 cid=%s turn=%s size=%s",
            ctx.conversation_id, turn.question[:30], size,
        )
    payload["payload_bytes"] = size
    return payload


def _title(ctx: AgentContext) -> str:
    for m in ctx.messages:
        if m.role is MessageRole.USER and m.content:
            return m.content.strip().replace("\n", " ")[:120]
    return "新会话"


def _size(payload: dict[str, Any]) -> int:
    blob = json.dumps(
        {k: v for k, v in payload.items() if k.endswith("_json")},
        ensure_ascii=False, default=str,
    )
    return len(blob.encode("utf-8"))


def _dump(obj: Any) -> Any:
    """Pydantic / dataclass / 基本类型 → JSON 安全结构。"""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            return repr(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _dump(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def _dump_retrieval(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "attempted": r.attempted,
        "reason": r.reason,
        "partial": r.partial,
        "candidates": [_dump(c) for c in r.candidates],
    }


def _dump_tool_calls(results: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in results:
        env = r.envelope
        out.append({
            "tool": r.name,
            "ok": env.ok,
            "truncated": env.truncated,
            "note": env.note,
            "graphRev": env.graph_rev,
            "evidence": [_dump(e) for e in env.evidence],
            "data": _dump(env.data),
        })
    return out
