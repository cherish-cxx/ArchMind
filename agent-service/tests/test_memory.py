"""R5 记忆：显式判定、触发规则、mark_stale 的纯函数与单元级验证。

不碰 MySQL / Qdrant / embed。循环里的记忆接入（召回注入、显式写入、确认行）
由 `test_agent_loop.py` 覆盖 —— 六选一删除后不再需要两套接入测试。
"""

from app.config import settings
from app.core.memory import (
    MemoryService,
    detect_explicit,
    explicit_draft,
    pick_subjects,
    render_memories,
    should_recall,
)
from app.models.memory import MAX_TEXT_CHARS, MemoryHit, MemoryType

ORDER_CONTROLLER = "com.hmall.trade.controller.OrderController"
ORDER_SERVICE = "com.hmall.trade.service.impl.OrderServiceImpl"


# ==================== 纯函数 ====================


def test_detect_explicit_prefix_and_suffix():
    assert detect_explicit("记住：订单入口是 OrderController") == "订单入口是 OrderController"
    assert detect_explicit("订单入口是 OrderController，记住") == "订单入口是 OrderController"
    assert detect_explicit("记一下，支付回调要从 OrderClient 走") == "支付回调要从 OrderClient 走"


def test_detect_explicit_absent_or_empty():
    assert detect_explicit("订单入口是什么") is None
    # 只有触发词：正文为空，由 headline 兜底
    assert detect_explicit("记住") == ""


def test_pick_subjects_prefers_message_then_focus():
    evidence = [ORDER_CONTROLLER, ORDER_SERVICE]
    assert pick_subjects("订单入口是 OrderController", evidence, None) == (ORDER_CONTROLLER,)
    assert pick_subjects("记住这个", evidence, ORDER_SERVICE) == (ORDER_SERVICE,)
    # 挑不出就不写 —— 一条指错类的记忆比没有记忆更糟
    assert pick_subjects("记住这个", evidence, None) == ()
    # focus 不在本轮 evidence 里，也不写
    assert pick_subjects("记住这个", [ORDER_CONTROLLER], ORDER_SERVICE) == ()


def test_explicit_draft_requires_trigger_evidence_and_subject():
    common = dict(graph_rev=1, conversation_id="c1", turn_no=1)

    assert explicit_draft(
        message="订单入口是什么", evidence_uids=[ORDER_CONTROLLER],
        focus_uid=None, **common,
    ) is None

    assert explicit_draft(
        message="记住：订单入口是 OrderController", evidence_uids=[],
        focus_uid=None, **common,
    ) is None

    assert explicit_draft(
        message="记住：这个很重要", evidence_uids=[ORDER_CONTROLLER],
        focus_uid=None, **common,
    ) is None


def test_explicit_draft_fields_and_headline_fallback():
    draft = explicit_draft(
        message="记住：订单入口是 OrderController",
        evidence_uids=[ORDER_CONTROLLER, ORDER_SERVICE],
        focus_uid=None, graph_rev=1, conversation_id="c1", turn_no=3,
    )
    assert draft is not None
    assert draft.type is MemoryType.FACT
    assert draft.subject_uids == (ORDER_CONTROLLER,)
    assert draft.source_turn_no == 3
    assert draft.trust.value == "explicit"

    # 只说"记住"，正文用 headline 兜底，subject 用焦点
    fallback = explicit_draft(
        message="记住", evidence_uids=[ORDER_CONTROLLER],
        focus_uid=ORDER_CONTROLLER, graph_rev=1, conversation_id="c1", turn_no=1,
        headline="订单入口是 OrderController",
    )
    assert fallback is not None
    assert fallback.text == "订单入口是 OrderController"


def test_explicit_draft_truncates_long_text():
    draft = explicit_draft(
        message="记住：" + "字" * 300,
        evidence_uids=[ORDER_CONTROLLER],
        focus_uid=ORDER_CONTROLLER, graph_rev=1, conversation_id="c1", turn_no=1,
    )
    assert draft is not None
    assert len(draft.text) <= MAX_TEXT_CHARS


def test_render_memories_empty_is_empty_string():
    assert render_memories([]) == ""
    assert render_memories(None) == ""
    block = render_memories([
        MemoryHit(id=1, text="支付回调入口是 PayNotifyController", type=MemoryType.FACT)
    ])
    assert "[事实]" in block
    assert "支付回调入口是 PayNotifyController" in block


# ==================== 触发判定 ====================


def _recall_params(**over):
    params = dict(
        message="订单流程", turns=3, last_outcome="OK",
        has_anchor=False, candidate_count=3,
        window_entities={"com.x.Old"},
    )
    params.update(over)
    return should_recall(**params)


def test_should_recall_precedence_table():
    assert _recall_params(
        turns=0, last_outcome=None, candidate_count=0, window_entities=set()
    ) == "cold_start"
    assert _recall_params(message="它呢", has_anchor=True) is None
    assert _recall_params(message="它呢", last_outcome="CLARIFY") == "after_clarify"
    # 跨会话说法不看窗口
    assert _recall_params(message="上次那个支付回调") == "cross_session_reference"
    # 弱指代要求窗口里没有实体
    assert _recall_params(message="它呢", window_entities=set()) == "unresolved_reference"
    assert _recall_params(message="它呢") is None
    # 类检索空手 → 记一次
    assert _recall_params(candidate_count=0) == "no_candidates"


# ==================== mark_stale ====================


class _MarkRepo:
    async def mark_stale(self, project_id: int, current_rev: int) -> list[int]:
        return [11, 12]


class _DropStore:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_ids(self, ids: list[int]) -> None:
        self.deleted.extend(ids)


async def test_mark_stale_also_drops_index_points(monkeypatch):
    """stale 只改库不删点的话，召回过滤仍会把它放出来。"""
    monkeypatch.setattr(settings, "memory_enabled", True)
    store = _DropStore()
    service = MemoryService(_MarkRepo(), store, embed=None)  # type: ignore[arg-type]

    assert await service.mark_stale(project_id=7, current_rev=8) == 2
    assert store.deleted == [11, 12]
