"""多步 Agent 循环（R6 第二步）。

测的是**控制流**，不测模型答得好不好：给定剧本，循环该调几次工具、
失败之后停不停、预算到点收不收尾 —— 这些必须完全确定。
"""

import json

import pytest

from app.core.agent_loop import (
    MAX_TOOL_MESSAGE_CHARS,
    run_agent_turn,
    tool_definitions,
)
from app.core.llm import LLMResult
from app.models.context import AgentContext, MessageRole, ToolCall
from app.models.memory import MemoryHit, MemoryType
from app.models.response import NodeRef, NodeRefType, StepType
from app.tools.java_client import InternalAuthError, ToolTransportError
from fakes import (
    LOCKED_METHOD,
    ORDER_CONTROLLER,
    ORDER_SERVICE,
    FakeJava,
    FakeLLM,
    FakeMemory,
    classes_with,
    failed,
    ok_chain,
    ok_classes,
    ok_method_body,
    ok_overview,
)

ANSWER_ARGS = {
    "headline": "下单先到 Controller 再到 Service",
    "sections": [{
        "title": "路径",
        "body": "`OrderController` 调用了 `OrderServiceImpl`。",
        "citedUids": [ORDER_CONTROLLER, ORDER_SERVICE],
    }],
}


def ctx(**kwargs) -> AgentContext:
    return AgentContext(conversation_id="c1", project_id=7, **kwargs)


def focused(uid: str = ORDER_CONTROLLER) -> AgentContext:
    """带锚点的上下文 —— 类级工具要靠它过已知实体检查。"""
    return ctx(focus=NodeRef(type=NodeRefType.CLASS, uid=uid))


def tool_call(call_id: str, name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name,
                    arguments=json.dumps(args or {}, ensure_ascii=False))


def reply(*calls: ToolCall, content: str | None = None) -> LLMResult:
    return LLMResult(content=content, tool_calls=tuple(calls), tokens_in=5, tokens_out=5)


def tool_messages(context: AgentContext) -> list[str]:
    return [m.content or "" for m in context.messages if m.role is MessageRole.TOOL]


# ==================== 工具清单 ====================


def test_definitions_cover_data_tools_and_answer():
    names = [d["function"]["name"] for d in tool_definitions()]
    assert names == ["overview", "classes", "class-relations", "call-chain", "method-body",
                     "navigate", "clarify", "answer"]
    # 每个都带 description 与 parameters —— 少一个模型就不会用
    assert all(d["function"]["description"] for d in tool_definitions())
    assert all(d["function"]["parameters"]["type"] == "object" for d in tool_definitions())


# ==================== 正常路径 ====================


async def test_answer_without_any_tool():
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    resp = await run_agent_turn(ctx(), "下单流程是什么", FakeJava(ok_chain()), llm)

    assert llm.call_count == 1
    assert resp.headline == ANSWER_ARGS["headline"]
    assert [s.title for s in resp.sections] == ["路径"]
    assert resp.meta.intent is None, "分发归模型后不再有意图标签"
    # 第一轮就把工具清单交出去了
    assert llm.calls[0]["tools"] is not None


async def test_tool_then_answer_appends_exchange():
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    context = focused()
    java = FakeJava(ok_chain())
    resp = await run_agent_turn(context, "下单流程", java, llm)

    assert java.call_count == 1
    assert llm.call_count == 2
    # 第二次请求里必须已经有 tool 消息，否则模型看不到第一次查了什么
    second = llm.calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second)
    assert resp.headline
    assert any(s.type is StepType.TOOL for s in resp.steps)


async def test_classes_then_chain_uses_new_start():
    """`classes` 没有 evidence，但它列出的类必须能成为下一次调用的起点。"""
    llm = FakeLLM([
        reply(tool_call("c1", "classes")),
        reply(tool_call("c2", "call-chain", {"uid": ORDER_SERVICE})),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(ok_classes())
    await run_agent_turn(ctx(), "看看有哪些类", java, llm)

    assert java.call_count == 2, "第二步应该真的执行了，而不是被已知实体检查拦下"


# ==================== 失败不终止 ====================


async def test_unknown_start_is_fed_back_not_raised():
    context = ctx()
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain", {"uid": "com.nowhere.Nope"})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(ok_chain())
    await run_agent_turn(context, "查一下", java, llm)

    assert java.call_count == 0, "不在已知集合里的起点不该打到 Java"
    # 已知集合是空的，提示走的是「先调 classes」那条；回填的必须是结构化 JSON
    assert any('"reason": "invalid_arguments"' in text for text in tool_messages(context))


async def test_unknown_tool_is_fed_back_not_raised():
    llm = FakeLLM([
        reply(tool_call("c1", "nope")),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    context = ctx()
    await run_agent_turn(context, "查一下", FakeJava(ok_chain()), llm)

    assert any("没有名为 nope 的工具" in text for text in tool_messages(context))
    assert llm.call_count == 2


async def test_failed_tool_result_tells_the_model_why():
    """`ok:false` 不能只回一个 `null` —— 模型不知道失败就不会换查法。"""
    context = focused()
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(failed("文件不存在或不可读"))
    await run_agent_turn(context, "看下流程", java, llm)

    assert any(
        "文件不存在或不可读" in text and '"ok": false' in text
        for text in tool_messages(context)
    ), "失败原因必须进 tool 消息"


async def test_auth_error_propagates():
    llm = FakeLLM([reply(tool_call("c1", "overview"))])
    java = FakeJava(ok_overview(), raises=InternalAuthError("token 不一致"))
    with pytest.raises(InternalAuthError):
        await run_agent_turn(ctx(), "看看项目", java, llm)


# ==================== 去重 ====================


async def test_duplicate_call_reuses_result_without_budget():
    llm = FakeLLM([
        reply(tool_call("c1", "overview"), tool_call("c2", "overview")),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    context = ctx()
    java = FakeJava(ok_overview())
    await run_agent_turn(context, "看看项目", java, llm)

    assert java.call_count == 1, "同工具同参数只打一次 Java"
    assert context.budget.tool_calls_this_turn == 1, "重复调用不扣预算"
    assert any("cached" in s.detail for s in context.turn.steps)


# ==================== 预算与收尾 ====================


async def test_budget_exhausted_forces_answer():
    context = ctx()
    context.budget.steps_limit = 1
    llm = FakeLLM([
        reply(tool_call("c1", "overview")),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    await run_agent_turn(context, "看看项目", FakeJava(ok_overview()), llm)

    assert llm.call_count == 2
    forced = llm.calls[1]
    # DeepSeek 会把「指定函数名」的 tool_choice 当成 auto，所以只能靠**只递一个工具**来收口
    assert forced["tool_choice"] == "required"
    assert [d["function"]["name"] for d in forced["tools"]] == ["answer"]


async def test_model_silent_forces_answer():
    """模型只说话不调工具 —— 不猜它的自由文本，逼一次结构化作答。"""
    llm = FakeLLM([
        reply(content="我觉得这个项目是电商系统"),
        reply(tool_call("c1", "answer", ANSWER_ARGS)),
    ])
    resp = await run_agent_turn(ctx(), "看看项目", FakeJava(ok_overview()), llm)

    assert llm.call_count == 2
    assert llm.calls[1]["tool_choice"] is not None
    assert resp.headline == ANSWER_ARGS["headline"]


async def test_bad_answer_arguments_are_retried():
    bad = LLMResult(content=None, tool_calls=(
        ToolCall(id="a1", name="answer", arguments='{"sections": []}'),
    ))
    llm = FakeLLM([bad, reply(tool_call("a2", "answer", ANSWER_ARGS))])
    context = ctx()
    resp = await run_agent_turn(context, "看看项目", FakeJava(ok_overview()), llm)

    assert any("answer 的参数不合规" in text for text in tool_messages(context))
    assert resp.headline == ANSWER_ARGS["headline"]


# ==================== 引用校验 ====================


async def test_cited_uids_outside_evidence_are_dropped():
    answer = {
        "headline": "结论",
        "sections": [{
            "title": "路径",
            "body": "正文",
            "citedUids": [ORDER_SERVICE, "com.bogus.Hallucinated"],
        }],
    }
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "answer", answer)),
    ])
    resp = await run_agent_turn(focused(), "下单流程", FakeJava(ok_chain()), llm)

    cited = resp.sections[0].cited_uids
    assert ORDER_SERVICE in cited
    assert "com.bogus.Hallucinated" not in cited


async def test_source_class_is_grounded_without_an_edge():
    """局部变量类型不产生图的边，但模型读过源码 —— 引用它应当算有出处。

    `Order order = new Order()` 只是方法体里的一个局部变量，关系解析只认
    字段 / 参数 / 返回类型 / 方法调用，所以图上没有 `OrderServiceImpl → Order`
    这条边。卡在「必须有边」等于让模型亲眼看到的事实不算数。
    """
    context = focused()
    llm = FakeLLM([
        reply(tool_call("c1", "classes")),
        reply(tool_call("c2", "method-body", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(
        ok_classes(),
        method_body_envelope=ok_method_body(
            "OrderServiceImpl svc = new OrderServiceImpl();"
        ),
    )
    resp = await run_agent_turn(context, "下单怎么实现的", java, llm)

    cited = resp.sections[0].cited_uids
    assert ORDER_SERVICE in cited, "源码里出现过的类应当被认可"
    assert ORDER_CONTROLLER not in cited, "既不在证据里、也没在源码里出现，仍要剔除"


async def test_source_class_absent_from_evidence_uses_project_roster():
    """源码提到、但本轮从没查过的类 —— 靠项目花名册落实它真实存在。"""
    cart = "com.hmall.cart.service.impl.CartServiceImpl"
    context = focused()
    llm = FakeLLM([
        reply(tool_call("c1", "method-body", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "answer", {
            "headline": "h",
            "sections": [{"title": "t", "body": "b", "citedUids": [cart]}],
        })),
    ])
    java = FakeJava(
        classes_with(cart),
        method_body_envelope=ok_method_body(
            "CartServiceImpl cart = new CartServiceImpl();"
        ),
    )
    resp = await run_agent_turn(context, "购物车怎么加", java, llm)

    assert resp.sections[0].cited_uids == [cart], "源码提到 + 图上存在 = 有出处"
    assert java.call_count == 2, "花名册只在需要时补一次"


# ==================== 澄清 ====================


async def test_clarify_ends_turn_with_options():
    llm = FakeLLM([reply(tool_call("c1", "clarify", {
        "question": "你问的是哪个服务？",
        "options": ["OrderService", "PayService"],
    }))])
    context = ctx()
    resp = await run_agent_turn(context, "那个流程", FakeJava(ok_overview()), llm)

    assert resp.message == "你问的是哪个服务？"
    assert [s.label for s in resp.suggestions] == ["OrderService", "PayService"]
    assert resp.sections == []
    assert context.last_outcome == "CLARIFY"


async def test_clarify_without_question_is_retried():
    bad = LLMResult(content=None, tool_calls=(
        ToolCall(id="c1", name="clarify", arguments="{}"),
    ))
    llm = FakeLLM([bad, reply(tool_call("c2", "answer", ANSWER_ARGS))])
    context = ctx()
    resp = await run_agent_turn(context, "问一下", FakeJava(ok_overview()), llm)

    assert any("clarify 需要非空的 question" in text for text in tool_messages(context))
    assert resp.headline == ANSWER_ARGS["headline"]


# ==================== 导航 ====================


async def test_navigate_focus_moves_anchor_without_budget():
    context = ctx()
    llm = FakeLLM([
        reply(tool_call("c1", "classes")),
        reply(tool_call("c2", "navigate", {"action": "focus", "uid": ORDER_SERVICE})),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    await run_agent_turn(context, "看看类", FakeJava(ok_classes()), llm)

    assert context.focus is not None and context.focus.uid == ORDER_SERVICE
    # 只有 classes 打了一次 Java —— navigate 是本地的，不扣工具预算
    assert context.budget.tool_calls_this_turn == 1


async def test_navigate_focus_pushes_previous_anchor():
    context = focused(ORDER_CONTROLLER)
    llm = FakeLLM([
        reply(tool_call("c1", "classes")),      # 先让 ORDER_SERVICE 进已知集合
        reply(tool_call("c2", "navigate", {"action": "focus", "uid": ORDER_SERVICE})),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    await run_agent_turn(context, "跳到实现类", FakeJava(ok_classes()), llm)

    assert context.focus.uid == ORDER_SERVICE
    assert context.focus_stack == [ORDER_CONTROLLER]


async def test_navigate_back_pops_stack():
    context = focused(ORDER_CONTROLLER)
    context.focus_stack = [ORDER_SERVICE]
    llm = FakeLLM([
        reply(tool_call("c1", "navigate", {"action": "back"})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    await run_agent_turn(context, "退回去", FakeJava(ok_overview()), llm)

    assert context.focus.uid == ORDER_SERVICE
    assert context.focus_stack == []


async def test_navigate_clear_reaches_the_canvas():
    """没有任何工具能产图时，navigate 的命令才被采用 —— 否则会被 SHOW_PATH 盖掉。"""
    context = focused(ORDER_CONTROLLER)
    llm = FakeLLM([
        reply(tool_call("c1", "navigate", {"action": "clear"})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    resp = await run_agent_turn(context, "先别看了", FakeJava(ok_overview()), llm)

    assert context.focus is None
    assert [c.op.value for c in resp.ui_commands] == ["CLEAR_VIEW"]


async def test_navigate_rejects_unknown_uid():
    context = focused(ORDER_CONTROLLER)
    llm = FakeLLM([
        reply(tool_call("c1", "navigate", {"action": "focus", "uid": "com.nope.X"})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    await run_agent_turn(context, "跳到那个类", FakeJava(ok_overview()), llm)

    assert context.focus.uid == ORDER_CONTROLLER, "被拒绝的导航不能改锚点"
    assert any("不在本轮已见过的实体里" in text for text in tool_messages(context))


async def test_derived_commands_beat_navigate():
    """有 call-chain 的时候画 SHOW_PATH，不能被 navigate 的 FOCUS_NODE 顶掉。"""
    context = focused(ORDER_CONTROLLER)
    llm = FakeLLM([
        reply(tool_call("c1", "navigate", {"action": "focus", "uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "call-chain", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c3", "answer", ANSWER_ARGS)),
    ])
    resp = await run_agent_turn(context, "看下流程", FakeJava(ok_chain()), llm)

    assert [c.op.value for c in resp.ui_commands] == ["SHOW_PATH"]


# ==================== 记忆 ====================


def hit(text: str = "上次说过：下单先到 Controller") -> MemoryHit:
    return MemoryHit(id=1, text=text, type=MemoryType.FACT,
                     subject_uids=(ORDER_CONTROLLER,), score=0.9)


async def test_memory_hits_go_into_system_prompt():
    """无锚点 + 第 0 轮 = cold_start，召回结果必须进系统提示。"""
    memory = FakeMemory([hit()])
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    context = ctx()
    await run_agent_turn(context, "这个项目的下单流程呢", FakeJava(ok_overview()),
                         llm, memory=memory)

    system = llm.calls[0]["messages"][0]["content"]
    assert "上次说过：下单先到 Controller" in system
    assert memory.recall_calls, "冷启动应该触发召回"
    assert len(context.turn.memory_hits) == 1


async def test_memory_absent_keeps_prompt_unchanged():
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    context = ctx()
    await run_agent_turn(context, "看看项目", FakeJava(ok_overview()), llm)

    assert context.turn.memory_hits == []
    assert "记忆参考" not in llm.calls[0]["messages"][0]["content"]


async def test_explicit_remember_is_written_before_answer():
    memory = FakeMemory()
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain", {"uid": ORDER_CONTROLLER})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    context = focused()
    resp = await run_agent_turn(
        context, "记住 OrderServiceImpl 是下单主流程",
        FakeJava(ok_chain()), llm, memory=memory,
    )

    assert memory.remembered, "命中「记住」且有 evidence，就该写一条"
    assert "已记住" in resp.message
    # 确认行必须一起进对话记录 —— 否则下一轮模型不知道自己记过
    assert "已记住" in context.turn.answer_message


async def test_explicit_remember_needs_evidence():
    """光说「记住」但本轮没查任何东西 —— 挑不出 subject，不写。"""
    memory = FakeMemory()
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    context = ctx()
    await run_agent_turn(context, "记住 OrderServiceImpl 是下单主流程",
                         FakeJava(ok_overview()), llm, memory=memory)

    assert memory.remembered == []


# ==================== 锁定方法 ====================


async def test_locked_method_is_prefetched():
    context = ctx(method_uid=LOCKED_METHOD)
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    java = FakeJava(ok_overview(), method_body_envelope=ok_method_body())
    await run_agent_turn(context, "这个方法怎么实现的", java, llm)

    assert java.call_count == 1
    first = llm.calls[0]["messages"]
    assert any(
        m.get("role") == "tool" and "cleanToken" in (m.get("content") or "")
        for m in first
    ), "锁定的源码必须出现在第一次请求里"
    assert [r.name for r in context.turn.tool_results] == ["method-body"]


async def test_locked_method_prefetch_failure_is_silent():
    context = ctx(method_uid=LOCKED_METHOD)
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    java = FakeJava(ok_overview(), raises=ToolTransportError("down"))
    resp = await run_agent_turn(context, "这个方法怎么实现的", java, llm)

    assert resp.headline == ANSWER_ARGS["headline"], "预取失败不能拖垮整轮"
    assert context.turn.tool_results == []


async def test_model_method_body_hits_prefetch_cache():
    context = ctx(method_uid=LOCKED_METHOD)
    llm = FakeLLM([
        reply(tool_call("c1", "method-body", {"methodUid": LOCKED_METHOD})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(ok_overview(), method_body_envelope=ok_method_body())
    await run_agent_turn(context, "这个方法怎么实现的", java, llm)

    assert java.call_count == 1, "预取过的不该再打一次 Java"


# ==================== 上下文长度 ====================


async def test_long_tool_result_is_clipped():
    context = ctx(method_uid=LOCKED_METHOD)
    llm = FakeLLM([
        reply(tool_call("c1", "method-body", {"methodUid": LOCKED_METHOD})),
        reply(tool_call("c2", "answer", ANSWER_ARGS)),
    ])
    java = FakeJava(ok_overview(), method_body_envelope=ok_method_body("x" * 40000))
    await run_agent_turn(context, "这个方法怎么实现的", java, llm)

    body = max(tool_messages(context), key=len)
    assert len(body) <= MAX_TOOL_MESSAGE_CHARS + 100
    assert "已截断" in body


# ==================== 步骤类型（R6 收尾）====================


async def test_steps_use_plan_not_llm_or_intent():
    llm = FakeLLM([
        reply(tool_call("t1", "overview")),
        reply(tool_call("a1", "answer", ANSWER_ARGS)),
    ])
    resp = await run_agent_turn(ctx(), "订单流程", FakeJava(), llm)

    types = {s.type for s in resp.steps}
    assert StepType.PLAN in types
    assert types <= {StepType.PLAN, StepType.TOOL}
    assert not hasattr(StepType, "LLM")
    assert not hasattr(StepType, "INTENT")
