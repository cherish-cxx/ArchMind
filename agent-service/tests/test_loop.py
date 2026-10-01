"""`core/loop.run_turn` 的单元测试 —— FakeLLM + FakeJava，不打网络。

这一层测的是**分支**：哪些情况该查图、哪些该拦住、LLM/Java 挂了各自退化成什么。
这些分支在真实的 LLM + Java 上没法稳定复现（得真把服务停掉才能测降级），
所以用替身把它们钉死在这里。
"""

import json

import pytest

from app.core.llm import LLMNotConfiguredError, LLMTransportError
from app.core.loop import run_turn
from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.response import NodeRef, NodeRefType
from app.tools.java_client import InternalAuthError, ToolTransportError
from fakes import ORDER_CONTROLLER, ORDER_SERVICE, FakeJava, FakeLLM, failed, ok_chain, ok_overview

PROJECT_ID = 7
GHOST = "com.hallucinated.GhostService"


def _ctx(focus_uid: str | None = ORDER_CONTROLLER) -> AgentContext:
    focus = NodeRef(type=NodeRefType.CLASS, uid=focus_uid) if focus_uid else None
    return AgentContext(conversation_id="c1", project_id=PROJECT_ID, focus=focus)


def _intent(intent: Intent, confidence: float = 0.95, **slots) -> str:
    payload: dict = {"intent": intent.value, "confidence": confidence}
    if slots:
        payload["slots"] = slots
    return json.dumps(payload)


def _explain(message: str = "顺着调用链往下看。", uids: list[str] | None = None) -> str:
    return json.dumps({"message": message, "citedUids": uids if uids is not None else [ORDER_CONTROLLER]})


# ==================== 主干 ====================


async def test_happy_path_message_citations_uicmd_steps():
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW),
        _explain("订单入口往下到 OrderServiceImpl，之后跨服务。", [ORDER_CONTROLLER, ORDER_SERVICE]),
    ])

    r = await run_turn(_ctx(), "订单创建的流程是什么", java, llm)

    assert java.calls == [("call-chain", PROJECT_ID, ORDER_CONTROLLER)]
    assert r.message == "订单入口往下到 OrderServiceImpl，之后跨服务。"
    assert {c.uid for c in r.citations} == {ORDER_CONTROLLER, ORDER_SERVICE}
    assert [c.op.value for c in r.ui_commands] == ["SHOW_PATH"]
    assert r.ui_commands[0].target.uid == ORDER_CONTROLLER
    assert [s.type.value for s in r.steps] == ["INTENT", "TOOL", "LLM"]
    assert r.meta.intent == "EXPLORE_FLOW"
    assert r.conversation_id == "c1"


async def test_overview_intent_works_without_anchor_and_emits_no_graph_command():
    java = FakeJava(ok_overview())
    llm = FakeLLM([_intent(Intent.EXPLORE_OVERVIEW), _explain("这是个电商交易系统。", [])])

    r = await run_turn(_ctx(focus_uid=None), "这个项目是干嘛的", java, llm)

    assert java.calls == [("overview", PROJECT_ID, None)]
    assert r.ui_commands == [], "overview 的数据不在图上，画不出图命令"


async def test_relation_intent_dispatches_to_class_relations():
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_RELATION), _explain("它的上游是控制器。")])

    await run_turn(_ctx(), "它的调用方呢", java, llm)

    assert java.calls[0][0] == "class-relations"


async def test_direction_from_slots_reaches_the_tool():
    """slots 里的方向必须一路传到工具 —— 中间任何一层改名都会静默变成默认值。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW, direction="UP"), _explain("上游是…")])

    await run_turn(_ctx(), "谁调用它", java, llm)

    assert java.kwargs[0]["direction"].value == "UP"


# ==================== 三条不查图的分支 ====================


async def test_low_confidence_asks_instead_of_calling_a_tool():
    """★ A3：置信度不够就**绝不查图** —— 错的图比不回答伤害大。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW, 0.4)])

    r = await run_turn(_ctx(), "那个啥", java, llm)

    assert java.call_count == 0, "低置信度竟然还是去查了图"
    assert llm.call_count == 1, "只该判意图，不该再调讲解"
    assert r.suggestions, "必须给出可点的选项，否则用户不知道该怎么改说法"
    assert [s.type.value for s in r.steps] == ["INTENT"]


async def test_navigate_pops_stack_and_never_calls_llm_twice():
    """★ E2：NAVIGATE 只动焦点 —— 不查图、不调 LLM #2，省掉一半成本。"""
    ctx = _ctx()
    ctx.focus_stack.append("com.a.Previous")
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.NAVIGATE)])

    r = await run_turn(ctx, "回到上一个", java, llm)

    assert llm.call_count == 1, "NAVIGATE 不该调第二次 LLM"
    assert java.call_count == 0
    assert ctx.focus.uid == "com.a.Previous"
    assert r.ui_commands[0].op.value == "FOCUS_NODE"
    assert r.ui_commands[0].target.uid == "com.a.Previous"


async def test_navigate_with_empty_stack_clears_view():
    ctx = _ctx()
    llm = FakeLLM([_intent(Intent.NAVIGATE)])

    r = await run_turn(ctx, "退回去", FakeJava(ok_chain()), llm)

    assert r.ui_commands[0].op.value == "CLEAR_VIEW"
    assert ctx.focus is None


async def test_out_of_scope_refuses_with_suggestions_and_no_tool():
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.OUT_OF_SCOPE)])

    r = await run_turn(_ctx(), "帮我写个登录接口", java, llm)

    assert java.call_count == 0
    assert "范围" in r.message
    assert r.suggestions
    assert llm.call_count == 1


async def test_flow_without_anchor_is_blocked_before_calling_tool():
    """没锚点时**提前拦住**，不空跑一次注定 ok:false 的工具调用（9月20 的入口约束）。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW)])

    r = await run_turn(_ctx(focus_uid=None), "订单流程是什么", java, llm)

    assert java.call_count == 0
    assert "点一个类" in r.message


# ==================== 降级与传播 ====================


async def test_intent_llm_down_falls_back_to_p2_rule():
    """判定挂了就退回 P2 的规则（有 focus 当流程问题）——退化的是一条**跑过的**路径。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([LLMTransportError("超时"), _explain("降级后仍然解释。")])

    r = await run_turn(_ctx(), "订单创建的流程是什么", java, llm)

    assert java.calls == [("call-chain", PROJECT_ID, ORDER_CONTROLLER)]
    assert "规则降级" in r.steps[0].detail


async def test_explain_llm_down_uses_template_and_mentions_gaps():
    """★ C2：讲解挂了，图查询照常 —— 退化成模板，且**断点仍然必须被说出来**。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW), LLMTransportError("超时")])

    r = await run_turn(_ctx(), "订单创建的流程是什么", java, llm)

    assert r.steps[-1].detail == "讲解(降级)"
    assert "别的服务" in r.message, "降级也不能把跨服务断点讲成「没有了」"


async def test_missing_api_key_raises_instead_of_degrading():
    """⚠️ 没配 key 是**配置错误** —— 必须炸，绝不能让配置问题伪装成正常运行。"""
    llm = FakeLLM([LLMNotConfiguredError("没配 key")])

    with pytest.raises(LLMNotConfiguredError):
        await run_turn(_ctx(), "订单流程", FakeJava(ok_chain()), llm)


async def test_tool_transport_error_degrades_without_second_llm_call():
    java = FakeJava(raises=ToolTransportError("连不上"))
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW)])

    r = await run_turn(_ctx(), "订单流程", java, llm)

    assert llm.call_count == 1, "工具都没拿到数据，讲解无话可讲，不该再调"
    assert "连不上" in r.message


async def test_internal_auth_error_is_not_degraded():
    """token 不一致是配置错误，必须一路炸出去变成 500。"""
    java = FakeJava(raises=InternalAuthError("token 不一致"))
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW)])

    with pytest.raises(InternalAuthError):
        await run_turn(_ctx(), "订单流程", java, llm)


async def test_tool_ok_false_is_reported_verbatim():
    """`ok:false` 是业务结果，原样转述原因，不猜不美化。"""
    java = FakeJava(failed("这个类不在图里"))
    llm = FakeLLM([_intent(Intent.EXPLORE_RELATION)])

    r = await run_turn(_ctx(), "它的调用方呢", java, llm)

    assert "这个类不在图里" in r.message
    assert java.calls[0][0] == "class-relations"


# ==================== 幻觉拦截 ====================


async def test_ghost_citation_is_dropped_but_real_one_survives():
    java = FakeJava(ok_chain())
    llm = FakeLLM([
        _intent(Intent.EXPLORE_FLOW),
        _explain("胡说的。", [GHOST, ORDER_CONTROLLER]),
    ])

    r = await run_turn(_ctx(), "订单流程", java, llm)

    assert {c.uid for c in r.citations} == {ORDER_CONTROLLER}


# ==================== 预算与历史 ====================


async def test_history_and_turn_count_accumulate_across_turns():
    java = FakeJava(ok_chain())
    llm = FakeLLM(
        [_intent(Intent.EXPLORE_FLOW), _explain("第一轮回答。")] * 2
    )
    ctx = _ctx()

    await run_turn(ctx, "第一次", java, llm)
    await run_turn(ctx, "第二次", java, llm)

    assert ctx.budget.turns == 2
    assert [h.q for h in ctx.history] == ["第一次", "第二次"]
    assert ctx.history[0].intent is Intent.EXPLORE_FLOW


async def test_per_turn_counters_reset():
    """`toolCallsThisTurn` 每轮必须归零 —— 不归零的话预算会跨轮累积，几轮之后就不让查了。"""
    java = FakeJava(ok_chain())
    llm = FakeLLM([_intent(Intent.EXPLORE_FLOW), _explain("一。")] * 2)
    ctx = _ctx()

    await run_turn(ctx, "第一轮", java, llm)
    assert ctx.budget.tool_calls_this_turn == 1

    await run_turn(ctx, "第二轮", java, llm)
    assert ctx.budget.tool_calls_this_turn == 1, "第二轮开始时应已清零，而不是变成 2"
    assert java.call_count == 2
