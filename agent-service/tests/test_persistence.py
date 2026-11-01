"""R5 持久化集成测试。

**默认跳过**：只在显式设置 `AGENT_TEST_MYSQL=1` 时运行，避免误写生产库。
运行前需要先 `alembic upgrade head`。

    AGENT_TEST_MYSQL=1 .venv/Scripts/python -m pytest tests/test_persistence.py -v

铁律和 `java_alive` 一致：**环境问题不该制造假红** —— 连不上就 skip。
"""

import os
import uuid

import pytest
from sqlalchemy import delete, text

from app.models.context import AgentContext
from app.store.db import Database
from app.store.conversation_repo import ConversationRepo
from app.store.orm import Conversation

pytestmark = pytest.mark.asyncio

_USER_ID = 2001
_PROJECT_ID = 999_900_001


@pytest.fixture
async def repo():
    if os.getenv("AGENT_TEST_MYSQL") != "1":
        pytest.skip("未设置 AGENT_TEST_MYSQL=1，跳过持久化集成测试")

    db = Database()
    try:
        async with db.session() as s:
            await s.execute(text("SELECT 1"))
            has_table = (await s.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'agent_turn'"
            ))).scalar_one()
    except Exception as e:  # noqa: BLE001
        await db.aclose()
        pytest.skip(f"MySQL 不可达或未迁移：{e}")

    if not has_table:
        await db.aclose()
        pytest.skip("agent_turn 不存在，先跑 alembic upgrade head")

    r = ConversationRepo(db)
    try:
        yield r
    finally:
        async with db.session() as s:
            async with s.begin():
                await s.execute(delete(Conversation).where(
                    Conversation.project_id == _PROJECT_ID
                ))
        await r.aclose()


def _ctx(cid: str) -> AgentContext:
    return AgentContext(conversation_id=cid, project_id=_PROJECT_ID)


async def test_turn_no_is_contiguous_and_context_round_trips(repo):
    cid = f"c_test_{uuid.uuid4().hex[:8]}"

    ctx = _ctx(cid)
    ctx.begin_turn("第一个问题")
    ctx.append_answer("第一个回答")
    ctx.finish_turn("OK", 12)
    assert await repo.commit_turn(ctx, _USER_ID, _PROJECT_ID) == 1

    ctx.begin_turn("第二个问题")
    ctx.append_answer("第二个回答")
    ctx.finish_turn("OK", 9)
    assert await repo.commit_turn(ctx, _USER_ID, _PROJECT_ID) == 2

    loaded = await repo.load_context(_USER_ID, _PROJECT_ID, cid)
    assert loaded is not None
    users = [m.content for m in loaded.messages if m.role.value == "user"]
    assert users == ["第一个问题", "第二个问题"]
    assert loaded.budget.turns == 2


async def test_cross_tenant_read_returns_none(repo):
    cid = f"c_test_{uuid.uuid4().hex[:8]}"
    ctx = _ctx(cid)
    ctx.begin_turn("私有问题")
    ctx.finish_turn("OK", 1)
    await repo.commit_turn(ctx, _USER_ID, _PROJECT_ID)

    assert await repo.load_context(_USER_ID + 1, _PROJECT_ID, cid) is None
    assert await repo.load_context(_USER_ID, _PROJECT_ID + 1, cid) is None


async def test_commit_to_foreign_conversation_is_forbidden(repo):
    from app.store.conversation_repo import ConversationForbidden

    cid = f"c_test_{uuid.uuid4().hex[:8]}"
    ctx = _ctx(cid)
    ctx.begin_turn("A 的问题")
    ctx.finish_turn("OK", 1)
    await repo.commit_turn(ctx, _USER_ID, _PROJECT_ID)

    other = _ctx(cid)
    other.begin_turn("B 想写进 A 的会话")
    other.finish_turn("OK", 1)
    with pytest.raises(ConversationForbidden):
        await repo.commit_turn(other, _USER_ID + 1, _PROJECT_ID)
