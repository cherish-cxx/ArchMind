"""对话上下文的 Redis 存储（P4.1）。

本文件本质上对话上下文的缓存仓库，专门把大模型对话的历史消息，放到redis里存，读
不是业务接口，是底层存储工具类，服务于我的对话业务逻辑

**为什么 logger 不从这里之外 import**：store 是被 tools/core 依赖的底层，
反过来依赖它们（比如 `from app.tools.java_client import logger`）会让分层倒过来。
依赖方向应该是 `api → core → tools → models`，store 只依赖 config + models。
"""

import logging

import redis.asyncio
from pydantic import ValidationError

from app.config import settings
from app.models.context import AgentContext

logger = logging.getLogger(__name__)

_KEY_PREFIX = "agent:ctx"


class ContextStore:
    # 正常运行：不传client，自动读取配置，创建异步redis连接
    # 为什么需要这一段：这种叫依赖注入，如果写死了redis，那么测试时候就必须启动redis，单元测试就会很重，很慢。
    # 现在支持外部可以替换，业务代码和测试代码一套类就能用
    #
    # ⚠️ 参数**不能叫 `redis`** —— 那会遮住上面 import 的 redis 模块，
    # 于是下面 `redis.asyncio.from_url` 取到的是这个参数（默认 None）→
    # AttributeError。这是个只在实例化时才炸的坑。
    def __init__(self, client: redis.asyncio.Redis | None = None) -> None:
        self._redis = client or redis.asyncio.from_url(
            settings.redis_url,
            db=settings.redis_db,
            password=settings.redis_password or None,
            # Redis取出的数据自动转字符串
            # 对话上下文一般是json字符串，如果拿出来是bytes，每次都要手动.decode()
            decode_responses=True,
        )

    # 统一生成RedisKey，把项目ID+对话ID拼成一个Redis的Key
    # redis是KV数据库，需要靠key找数据
    @staticmethod
    def _key(project_id: int, conversation_id: str) -> str:
        return f"{_KEY_PREFIX}:{project_id}:{conversation_id}"

    # 用完ContextStore后，关闭底层Redis连接，由于异步连接不主动关闭，连接会一直占用，连接会一直占用，连接池耗尽，服务卡死
    async def aclose(self) -> None:
        await self._redis.aclose()

    """
    load用于会话上下文读取入口，自带降级策略：
    读不到数据，数据解析失败，
    """
    async def load(self, project_id: int, conversation_id: str) -> AgentContext:
        # 用上面写好的_key（）拼出redis的key
        raw = await self._redis.get(self._key(project_id, conversation_id))
        # 如果raw是None：Redis里没有这句会话记录（说明是第一次聊天，或者会话过期被删掉了）
        if raw is None:
            logger.info("新建 Context projectId=%s conversationId=%s", project_id, conversation_id)
            return AgentContext(conversation_id=conversation_id, project_id=project_id)
        try:
            # 读取到了字符串raw，这个字符串是之前存进去的JSON
            # model_validate_json是Pydantic的方法，把JOSN字符串直接解析成AgentCOntext
            return AgentContext.model_validate_json(raw)
        except ValidationError:
            # 这里和 java_client 的契约漂移**不一样**，别混：
            # 那边漂的是「和 Java 的契约」，必须炸；这边漂的是**自己的内部存储**，
            # 重建的代价只是「用户重开一句」—— 而这正是设计 §2.4 决定③的既定语义
            # 「丢了等于这次对话结束」。所以它同时是**版本迁移策略**：
            # 改模型 → 老 key 反序列化失败 → 自然当新会话 → 2h 内 TTL 淘汰干净。
            logger.warning("Context 反序列化失败，按新会话重建 projectId=%s conversationId=%s",
                           project_id, conversation_id)
            return AgentContext(conversation_id=conversation_id, project_id=project_id)

    async def save(self, ctx: AgentContext) -> None:
        await self._redis.set(
            self._key(ctx.project_id, ctx.conversation_id),
            # by_alias 显式写死：蛇形/驼峰两种都能 round-trip（base.py 的 populate_by_name），
            # 正因为都行，不写死就会依赖 pydantic 默认值 —— 那玩意儿容易记错、也可能随版本变
            ctx.model_dump_json(by_alias=True),
            # ex 每次 save 都重设 → TTL 语义是「最后一次活动后 2h」，不是「创建后 2h」
            ex=settings.context_ttl_seconds,
        )

    async def evict_project(self, project_id: int) -> int:
        """项目删除时清掉它全部的会话 Context。返回删掉的数量（路由层要回显）。"""
        # 必须用 scan_iter，不能用 KEYS —— KEYS 会阻塞整个 Redis 实例（单线程），
        # 连 db0 上 Java 的 overview 缓存都得等它扫完
        pattern = f"{_KEY_PREFIX}:{project_id}:*"
        deleted = 0
        async for key in self._redis.scan_iter(match=pattern, count=100):
            await self._redis.delete(key)
            deleted += 1
        logger.info("清掉 projectId=%s 的 %d 个 Context", project_id, deleted)
        return deleted
