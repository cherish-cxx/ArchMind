"""MySQL 连接与事务边界（R5）。

**MySQL 是真相源，Redis 是热缓存。** 这里只负责"怎么连、怎么开事务"，
不掺任何业务读写 —— 那些在 `conversation_repo.py`。

和 `ContextStore` / `IndexStore` 一致：构造时接受可选注入（测试用），
提供 `aclose()`。区别是连接池参数必须显式给：容器跑一夜后，
MySQL 会按 `wait_timeout` 掐掉空闲连接，`pool_pre_ping` 是唯一免踩坑的办法。
"""

import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """ORM 基类。迁移脚本靠 `Base.metadata` 生成表。"""


class Database:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self.engine: AsyncEngine = engine or create_async_engine(
            settings.mysql_dsn,
            pool_size=settings.agent_mysql_pool_size,
            max_overflow=settings.agent_mysql_max_overflow,
            # 空闲一夜后 MySQL 会掐连接；没有 pre_ping，第二天第一条 SQL 直接 Lost connection
            pool_pre_ping=True,
            # 比 MySQL 默认 wait_timeout(8h) 小，提前回收
            pool_recycle=3600,
            # /health 每 10s 一次；MySQL 挂掉时不能让探针卡住十几秒
            pool_timeout=5,
            connect_args={"connect_timeout": 3},
            echo=settings.agent_mysql_echo,
        )
        # expire_on_commit=False：commit 之后还要读对象属性（比如刚回填的 turn_no），
        # 否则会触发一次额外 SELECT，而且在异步下容易漏 await
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession,
        )

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        """`/health` 用的轻量探针。连不上返回 False，绝不抛。"""
        from sqlalchemy import text

        try:
            async with self.session() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001 —— 探针不该因为任何异常把健康检查打挂
            logger.warning("MySQL 探针失败：%s", e)
            return False
