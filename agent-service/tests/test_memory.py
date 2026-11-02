"""R5 记忆：显式判定（纯函数）+ loop 接入。

不碰 MySQL / Qdrant / embed：

- `detect_explicit` / `pick_subjects` / `explicit_draft` / `render_memories` 是纯函数；
- loop 那两条用手写的 `FakeMemory` 替身。

真库路径在 `test_memory_repo.py`（默认跳过）。
"""

import json

from app.config import settings
from app.core.loop import run_turn
from app.core.memory import (
    MemoryService,
    detect_explicit,
    explicit_draft,
    pick_subjects,
    render_memories,
    should_recall,
)
from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.memory import MAX_TEXT_CHARS, MemoryHit, MemoryType
from app.models.response import NodeRef, NodeRefType
from fakes import (
    ORDER_CONTROLLER,
    ORDER_SERVICE,
    FakeJava,
    FakeLLM,
    FakeMemory,
    FakeRetriever,
    candidate,
    ok_chain,
)

PROJECT_ID = 7
PAY = "com.hmall.pay.controller.PayNotifyController"


def _ctx(focus_uid: str | None = ORDER_CONTROLLER) -> AgentContext:
    focus = NodeRef(type=NodeRefType.CLASS, uid=focus_uid) if focus_uid else None
    return AgentContext(conversation_id="c1", project_id=PROJECT_ID, focus=focus)


def _intent(intent: Intent, confidence: float = 0.95, **slots) -> str:
    payload: dict = {"intent": intent.value, "confidence": confidence}
    if slots:
        payload["slots"] = slots
    return json.dumps(payload)


def _explain(message: str = "顺着调用链往下看。", uids: list[str] | None = None) -> str:
    return json.dumps({
        "message": message,
        "citedUids": uids if uids is not None else [ORDER_CONTROLLER],
    })


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


# ==================== loop 接入 ====================


async def test_cold_start_recalls_and_injects_memory_block():
    mem = FakeMemory([
        MemoryHit(id=1, text="支付回调入口是 PayNotifyController", type=MemoryType.FACT)
    ])
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW, entityRef=ORDER_CONTROLLER, anchorScore=3),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER, ORDER_SERVICE]),
    ])
    retriever = FakeRetriever([candidate(ORDER_CONTROLLER)], skipped=None)

    await run_turn(_ctx(focus_uid=None), "订单流程是什么", java, llm, retriever, mem)

    assert mem.recall_calls == [(0, PROJECT_ID, "订单流程是什么")]
    system, user = llm.calls[1]["messages"]
    assert "=== 记忆参考" in user["content"]
    assert "支付回调入口是 PayNotifyController" in user["content"]
    # 记忆是背景不是证据：规则必须随记忆一起出现
    assert "不能当作本轮证据" in system["content"]


async def test_no_recall_when_focus_exists():
    mem = FakeMemory()
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])

    await run_turn(_ctx(), "订单流程是什么", java, llm, None, mem)

    assert mem.recall_calls == [], "有锚点就不查记忆 —— 和 _retrieve 同一条规则"


async def test_explicit_remember_writes_and_appends_note():
    mem = FakeMemory()
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])

    r = await run_turn(_ctx(), "记住：订单入口是 OrderController", java, llm, None, mem)

    assert len(mem.remembered) == 1
    draft = mem.remembered[0]
    assert draft.text == "订单入口是 OrderController"
    assert draft.subject_uids == (ORDER_CONTROLLER,)
    assert draft.source_turn_no == 1
    assert r.message.endswith("（已记住：订单入口是 OrderController）")


async def test_without_memory_service_prompt_is_unchanged():
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])

    await run_turn(_ctx(), "订单流程是什么", java, llm)

    system, user = llm.calls[1]["messages"]
    assert "记忆参考" not in system["content"]
    assert "记忆参考" not in user["content"]


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


async def test_cross_session_reference_recalls_even_with_nonempty_window():
    mem = FakeMemory([
        MemoryHit(id=2, text="支付回调入口是 PayNotifyController",
                  type=MemoryType.FACT, subject_uids=(PAY,))
    ])
    ctx = _ctx(focus_uid=None)
    ctx.budget.turns = 3
    ctx.last_outcome = "OK"
    # 窗口里有实体，但「上次」说的是窗口之外的事
    ctx.append_retrieval("旧检索结果", uids=["com.x.Old"])

    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW, entityRef=PAY, anchorScore=3),
        _explain("支付回调往下走。", [ORDER_CONTROLLER]),
    ])
    retriever = FakeRetriever([candidate(ORDER_CONTROLLER)], skipped=None)

    await run_turn(ctx, "上次那个支付回调怎么走的", java, llm, retriever, mem)

    assert len(mem.recall_calls) == 1
    # 记忆里的实体被接受为锚点，工具真的查了它
    assert java.calls[0][2] == PAY


async def test_weak_reference_with_window_entity_does_not_recall():
    mem = FakeMemory()
    ctx = _ctx(focus_uid=None)
    ctx.budget.turns = 3
    ctx.last_outcome = "OK"
    ctx.append_retrieval("旧检索结果", uids=["com.x.Old"])

    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW, entityRef=ORDER_CONTROLLER, anchorScore=3),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])
    retriever = FakeRetriever([candidate(ORDER_CONTROLLER)], skipped=None)

    await run_turn(ctx, "它呢", java, llm, retriever, mem)

    assert mem.recall_calls == [], "窗口里已经有着落，不该再查记忆"


async def test_after_clarify_triggers_recall():
    mem = FakeMemory()
    ctx = _ctx(focus_uid=None)
    ctx.budget.turns = 2
    ctx.last_outcome = "CLARIFY"

    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW, entityRef=ORDER_CONTROLLER, anchorScore=3),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])
    retriever = FakeRetriever([candidate(ORDER_CONTROLLER)], skipped=None)

    await run_turn(ctx, "订单流程是什么", java, llm, retriever, mem)

    assert len(mem.recall_calls) == 1


async def test_empty_candidates_triggers_recall():
    mem = FakeMemory()
    ctx = _ctx(focus_uid=None)
    ctx.budget.turns = 2
    ctx.last_outcome = "OK"

    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_OVERVIEW),
        _explain("这是个电商交易系统。", []),
    ])

    await run_turn(ctx, "这个项目是干嘛的", java, llm, None, mem)

    assert len(mem.recall_calls) == 1


async def test_memory_reaches_intent_prompt_on_cold_start():
    mem = FakeMemory([
        MemoryHit(id=3, text="支付回调入口是 PayNotifyController",
                  type=MemoryType.FACT, subject_uids=(PAY,))
    ])
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW, entityRef=ORDER_CONTROLLER, anchorScore=3),
        _explain("订单入口往下到 OrderServiceImpl。", [ORDER_CONTROLLER]),
    ])
    retriever = FakeRetriever([candidate(ORDER_CONTROLLER)], skipped=None)

    await run_turn(_ctx(focus_uid=None), "订单流程是什么", java, llm, retriever, mem)

    # 记忆块进的是意图判定的 *system*（候选区同段），不是用户消息
    intent_system = llm.calls[0]["messages"][0]["content"]
    assert "支付回调入口是 PayNotifyController" in intent_system
    assert PAY in intent_system
