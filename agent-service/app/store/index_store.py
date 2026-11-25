"""向量索引状态与项目级锁（Redis db1）。

状态是「项目 + rev」维度的：
    ready     当前 rev 已完整索引，可以用于检索
    indexing  正在写，检索不应使用（v1 的闸门）
    failed    上次失败，等重试
    not_ready 描述尚未补齐，不应建索引

锁保证同一项目同时只有一个索引任务；TTL 防止进程崩溃后永久锁死。
"""

import json
import logging
import time
import uuid

import redis.asyncio

from app.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "archmind:index:"

_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class IndexStore:
    def __init__(
        self,
        client: redis.asyncio.Redis | None = None,
        *,
        namespace: str = "",
    ) -> None:
        self._redis = client or redis.asyncio.from_url(
            settings.redis_url,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        # namespace 为空时保持 R4 现有键：archmind:index:state:{pid} / ...:lock:{pid}。
        # 能力索引用 namespace="capability"，键变成
        # archmind:index:capability:state:{pid} / ...:lock:{pid}，两套状态互不干扰。
        self._prefix = f"{_PREFIX}{namespace}:" if namespace else _PREFIX

    async def aclose(self) -> None:
        await self._redis.aclose()

    def _state_key(self, project_id: int) -> str:
        return f"{self._prefix}state:{project_id}"

    def _lock_key(self, project_id: int) -> str:
        return f"{self._prefix}lock:{project_id}"

    async def get_state(self, project_id: int) -> dict | None:
        raw = await self._redis.get(self._state_key(project_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("索引状态反序列化失败 projectId=%s", project_id)
            return None

    async def set_state(self, project_id: int, state: dict) -> None:
        state = {**state, "projectId": project_id, "updatedAt": int(time.time())}
        await self._redis.set(
            self._state_key(project_id), json.dumps(state, ensure_ascii=False)
        )

    async def clear_state(self, project_id: int) -> None:
        await self._redis.delete(self._state_key(project_id))

    async def acquire_lock(self, project_id: int) -> str | None:
        token = uuid.uuid4().hex
        ok = await self._redis.set(
            self._lock_key(project_id), token, nx=True, ex=settings.index_lock_ttl
        )
        return token if ok else None

    async def release_lock(self, project_id: int, token: str) -> None:
        # 只删自己的锁：TTL 到期后可能有新任务持锁，不能盲删
        await self._redis.eval(_RELEASE_LOCK, 1, self._lock_key(project_id), token)
