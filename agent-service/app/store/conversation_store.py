"""会话存储门面：Redis（热缓存） + MySQL（真相）（R5）。

对外只暴露四个动作，路由层不关心底下有几层存储：

    load_or_none   Redis → miss → MySQL → 回填 Redis
    save           只写 Redis（`/viewport` 用：屏幕快照不进 MySQL）
    commit_turn    MySQL 事务 → 成功后才写 Redis
    evict_project  Redis + MySQL 级联

**顺序是刻意的**：MySQL 先落、Redis 后写。失败时 Redis 仍是本轮开始前的状态，
等于自动回滚（方案 A），不需要任何补偿代码。
"""

import logging

from app.config import settings
from app.models.context import AgentContext, ChatMessage
from app.store.context_store import ContextStore
from app.store.conversation_repo import ConversationRepo

logger = logging.getLogger(__name__)


class ConversationStore:
    def __init__(
        self,
        context_store: ContextStore | None = None,
        repo: ConversationRepo | None = None,
    ) -> None:
        self._redis = context_store or ContextStore()
        self._repo = repo or ConversationRepo()

    async def aclose(self) -> None:
        await self._redis.aclose()
        await self._repo.aclose()

    # ==================== 读 ====================

    async def load_or_none(
        self, user_id: int, project_id: int, conversation_id: str
    ) -> AgentContext | None:
        ctx = await self._redis.load_or_none(user_id, project_id, conversation_id)
        if ctx is not None:
            return ctx

        ctx = await self._repo.load_context(user_id, project_id, conversation_id)
        if ctx is not None:
            ctx.user_id = user_id          # 老数据可能没存 user_id，回填后再进缓存
            await self._redis.save(ctx)
            return ctx

        # 快照没了但流水还在 —— 降级重建（focus_stack / viewport 推不出来，只能置空）
        messages = await self._repo.load_recent_messages(
            user_id, project_id, conversation_id
        )
        if not messages:
            return None
        logger.warning(
            "agent_context 缺失，按流水降级重建 cid=%s（focus/viewport 置空）",
            conversation_id,
        )
        ctx = AgentContext(conversation_id=conversation_id, project_id=project_id,
                           user_id=user_id)
        ctx.messages = [ChatMessage.model_validate(m) for m in messages]
        ctx.turn_start = len(ctx.messages)
        ctx.budget.turns = await self._repo.load_turn_count(
            user_id, project_id, conversation_id
        )
        await self._redis.save(ctx)
        return ctx

    async def load(
        self, user_id: int, project_id: int, conversation_id: str
    ) -> AgentContext:
        ctx = await self.load_or_none(user_id, project_id, conversation_id)
        if ctx is not None:
            return ctx
        return AgentContext(conversation_id=conversation_id, project_id=project_id,
                            user_id=user_id)

    async def list_conversations(
        self, user_id: int, project_id: int | None = None, limit: int = 50
    ):
        """会话索引：只读 MySQL，不碰 Redis（索引页不需要每条的完整 Context）。"""
        return await self._repo.list_conversations(user_id, project_id, limit)

    async def load_turns(
        self, user_id: int, project_id: int, conversation_id: str
    ):
        """会话历史：返回按 turn_no 正序的有效回合。"""
        return await self._repo.load_turns(user_id, project_id, conversation_id)

    # ==================== 写 ====================

    async def save(self, ctx: AgentContext) -> None:
        """只写 Redis。`/viewport` 走这条路：屏幕快照不进 MySQL。"""
        await self._redis.save(ctx)

    async def commit_turn(
        self, ctx: AgentContext, user_id: int, project_id: int
    ) -> bool:
        """一轮落库。返回是否成功。

        失败时**什么都不做**：Redis 仍是本轮开始前的状态，
        下一轮 load 到的就是"这一轮没发生过"。
        """
        try:
            turn_no = await self._repo.commit_turn(ctx, user_id, project_id)
        except Exception as e:  # noqa: BLE001 —— 持久化不该阻断回答，但必须可见
            logger.error(
                "本轮落库失败，Redis 保持上一轮状态 cid=%s user=%s project=%s: %s",
                ctx.conversation_id, user_id, project_id, e,
            )
            return False

        ctx.budget.turns = turn_no
        ctx.prune_window(
            settings.message_window_max_messages,
            settings.message_window_max_chars,
        )
        try:
            await self._redis.save(ctx)
        except Exception as e:  # noqa: BLE001 —— 缓存失败无害，下次会回落到 MySQL
            logger.warning("Redis 回写失败（下次读会回落到 MySQL）：%s", e)
        return True

    async def evict_project(self, project_id: int) -> int:
        n = await self._redis.evict_project(project_id)
        try:
            await self._repo.purge_project(project_id)
        except Exception as e:  # noqa: BLE001 —— MySQL 清理失败不能阻止项目删除
            logger.error("清理 MySQL 会话失败 projectId=%s: %s", project_id, e)
        return n
