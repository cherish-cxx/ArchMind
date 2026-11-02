"""R5 记忆仓库测试。

分两层：

- **纯逻辑**（归一化、哈希、合并决策）：不需要数据库，一直跑；
- **集成**：和 `test_persistence` 一样，只有 `AGENT_TEST_MYSQL=1` 时才运行。
  跑之前先 `alembic upgrade head`。

    AGENT_TEST_MYSQL=1 .venv/Scripts/python -m pytest tests/test_memory_repo.py -v
"""

import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, text

from app.models.context import AgentContext
from app.models.memory import (
    MAX_TEXT_CHARS,
    MemoryDraft,
    MemoryStatus,
    MemoryType,
    hash_text,
    normalize_text,
)
from app.store.db import Database
from app.store.conversation_repo import ConversationRepo
from app.store.memory_repo import MemoryRepo, decide_merge
from app.store.orm import Conversation, Memory

_USER_ID = 2002
_PROJECT_ID = 999_900_002


def _draft(
    text: str,
    *,
    memory_type: MemoryType = MemoryType.FACT,
    graph_rev: int = 3,
    subject: str | None = None,
    expires_at: datetime | None = None,
) -> MemoryDraft:
    return MemoryDraft(
        type=memory_type,
        text=text,
        subject_uids=(subject,) if subject else (),
        graph_rev=graph_rev,
        expires_at=expires_at,
    )


# ==================== 纯逻辑 ====================


def test_normalize_collapses_whitespace_without_case_folding():
    assert normalize_text("  a\n\tb  c ") == "a b c"
    assert normalize_text("OrderService") == "OrderService"


def test_hash_ignores_whitespace_but_not_case():
    assert hash_text("OrderService") == hash_text("  OrderService  ")
    assert hash_text("OrderService") != hash_text("orderservice")


def test_draft_rejects_empty_and_overlong_text():
    with pytest.raises(ValueError):
        _draft("   ")
    with pytest.raises(ValueError):
        _draft("字" * (MAX_TEXT_CHARS + 1))


def test_decide_merge_table():
    active = MemoryStatus.ACTIVE.value
    stale = MemoryStatus.STALE.value
    assert decide_merge(None, 0, 1) == "insert"
    assert decide_merge(active, 3, 4) == "update"
    # 同 rev 的积压回合不能复活：它证明不了新图里还有这条
    assert decide_merge(stale, 3, 3) == "ignore"
    assert decide_merge(stale, 3, 4) == "revive"
    assert decide_merge(stale, 5, 4) == "ignore"
    assert decide_merge(MemoryStatus.EXPIRED.value, 3, 4) == "ignore"
    assert decide_merge(MemoryStatus.REJECTED.value, 3, 4) == "ignore"
    assert decide_merge(MemoryStatus.SUPERSEDED.value, 3, 4) == "ignore"


# ==================== 集成（需要真实 MySQL）====================


@pytest.fixture
async def repo():
    if os.getenv("AGENT_TEST_MYSQL") != "1":
        pytest.skip("未设置 AGENT_TEST_MYSQL=1，跳过记忆仓库集成测试")

    db = Database()
    try:
        async with db.session() as s:
            await s.execute(text("SELECT 1"))
            has_table = (await s.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'agent_memory'"
            ))).scalar_one()
    except Exception as e:  # noqa: BLE001
        await db.aclose()
        pytest.skip(f"MySQL 不可达或未迁移：{e}")

    if not has_table:
        await db.aclose()
        pytest.skip("agent_memory 不存在，先跑 alembic upgrade head")

    r = MemoryRepo(db)
    try:
        yield r
    finally:
        async with db.session() as s:
            async with s.begin():
                await s.execute(delete(Memory).where(
                    Memory.project_id == _PROJECT_ID
                ))
        await r.aclose()


async def test_insert_then_merge_by_normalized_text(repo):
    row, action = await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("支付回调入口是 PayNotifyController")
    )
    assert action == "inserted"
    assert row.status == MemoryStatus.ACTIVE.value
    assert row.text_hash == _draft("支付回调入口是 PayNotifyController").text_hash

    # 空白不同、归一化相同 → 合并，不新增
    row2, action2 = await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("支付回调入口是   PayNotifyController")
    )
    assert action2 == "updated"
    assert row2.id == row.id


async def test_stale_revives_on_newer_rev_and_ignores_older(repo):
    row, _ = await repo.upsert(_USER_ID, _PROJECT_ID, _draft("结论 A", graph_rev=3))
    assert len(await repo.mark_stale(_PROJECT_ID, 4)) >= 1
    assert (await repo.get(row.id)).status == MemoryStatus.STALE.value

    # 新 rev 下同样的结论回来 → 复活
    revived, action = await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("结论 A", graph_rev=4)
    )
    assert action == "revived"
    assert revived.id == row.id
    assert revived.status == MemoryStatus.ACTIVE.value
    assert revived.graph_rev == 4

    # 再置 stale，然后用更旧的 rev 写入 → 不复活
    await repo.mark_stale(_PROJECT_ID, 5)
    _, action2 = await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("结论 A", graph_rev=4)
    )
    assert action2 == "ignored"
    assert (await repo.get(row.id)).status == MemoryStatus.STALE.value


async def test_mark_stale_only_touches_fact(repo):
    fact, _ = await repo.upsert(_USER_ID, _PROJECT_ID, _draft("结论 B", graph_rev=3))
    open_q, _ = await repo.upsert(
        _USER_ID, _PROJECT_ID,
        _draft("订单模块还没看完", memory_type=MemoryType.OPEN_QUESTION, graph_rev=3),
    )

    await repo.mark_stale(_PROJECT_ID, 4)

    assert (await repo.get(fact.id)).status == MemoryStatus.STALE.value
    assert (await repo.get(open_q.id)).status == MemoryStatus.ACTIVE.value


async def test_expire_capacity_evicts_least_recently_used(repo):
    rows = []
    for i in range(3):
        r, _ = await repo.upsert(_USER_ID, _PROJECT_ID, _draft(f"结论 {i}"))
        rows.append(r)

    # 后两条用过，第一条没用过 —— 淘汰必然落在第一条
    await repo.mark_used([rows[1].id, rows[2].id])
    expired = await repo.expire_capacity(
        _USER_ID, _PROJECT_ID, MemoryType.FACT, cap=2
    )
    assert expired == [rows[0].id]
    assert (await repo.get(rows[0].id)).status == MemoryStatus.EXPIRED.value
    assert (await repo.get(rows[1].id)).status == MemoryStatus.ACTIVE.value


async def test_expire_due_and_active_listing(repo):
    row, _ = await repo.upsert(
        _USER_ID, _PROJECT_ID,
        _draft("已经过期的未决", memory_type=MemoryType.OPEN_QUESTION,
               expires_at=datetime(2020, 1, 1)),
    )
    await repo.expire_due()

    assert (await repo.get(row.id)).status == MemoryStatus.EXPIRED.value
    assert await repo.list_active(_USER_ID, _PROJECT_ID) == []


async def test_list_by_subject_returns_only_active(repo):
    await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("关于支付的事实", subject="com.x.Pay")
    )
    await repo.upsert(
        _USER_ID, _PROJECT_ID, _draft("关于订单的事实", subject="com.x.Order")
    )

    hits = await repo.list_by_subject(_USER_ID, _PROJECT_ID, "com.x.Pay")
    assert [m.text for m in hits] == ["关于支付的事实"]


async def test_purge_project_removes_rows(repo):
    await repo.upsert(_USER_ID, _PROJECT_ID, _draft("会被一起删掉的结论"))
    assert await repo.purge_project(_PROJECT_ID) >= 1
    assert await repo.list_active(_USER_ID, _PROJECT_ID) == []


async def test_idle_scan_query_works_on_real_mysql():
    """验证蒸馏扫描的 SQL：`DATE_SUB(NOW(3), INTERVAL n MINUTE)` + 分组过滤。

    这条查询平时只在后台跑，单测里没有替身能覆盖它的语法 ——
    真库跑一次，才知道写法在 MySQL 上成立。
    """
    if os.getenv("AGENT_TEST_MYSQL") != "1":
        pytest.skip("未设置 AGENT_TEST_MYSQL=1，跳过蒸馏扫描查询测试")

    db = Database()
    repo = ConversationRepo(db)
    cid = f"c_distill_{uuid.uuid4().hex[:8]}"
    try:
        ctx = AgentContext(conversation_id=cid, project_id=_PROJECT_ID)
        ctx.begin_turn("支付回调从哪进")
        ctx.append_answer("从 PayNotifyController 进")
        ctx.finish_turn("OK", 1)
        await repo.commit_turn(ctx, _USER_ID, _PROJECT_ID)

        # 把这一轮拨到 31 分钟前，模拟空闲会话
        async with db.session() as s:
            async with s.begin():
                await s.execute(
                    text(
                        "UPDATE agent_turn SET created_at = NOW(3) - INTERVAL 31 MINUTE "
                        "WHERE conversation_id = :cid"
                    ),
                    {"cid": cid},
                )

        hits = await repo.idle_conversations_for_distill(30, limit=50)
        assert any(h[0] == cid for h in hits)
    finally:
        async with db.session() as s:
            async with s.begin():
                await s.execute(delete(Conversation).where(Conversation.id == cid))
        await repo.aclose()
