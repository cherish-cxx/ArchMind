"""上下文 v2：messages 模型与窗口策略（R5）。

这些是纯模型测试，不碰网络、不碰数据库。
"""

import pytest

from app.models.context import (
    CURRENT_CONTEXT_VERSION,
    AgentContext,
    ChatMessage,
    MessageRole,
    ToolCall,
)


def _ctx() -> AgentContext:
    return AgentContext(conversation_id="c1", project_id=7)


def test_begin_turn_appends_user_and_resets_counters():
    ctx = _ctx()
    ctx.budget.steps_used = 3
    ctx.budget.tool_calls_this_turn = 2

    ctx.begin_turn("订单流程是什么")

    assert len(ctx.messages) == 1
    assert ctx.messages[0].role is MessageRole.USER
    assert ctx.messages[0].content == "订单流程是什么"
    assert ctx.turn.question == "订单流程是什么"
    assert ctx.turn_start == 0
    assert ctx.budget.steps_used == 0
    assert ctx.budget.tool_calls_this_turn == 0


def test_empty_question_does_not_append_user_message():
    """按钮导航没有用户话术，不该伪造一条 USER 消息。"""
    ctx = _ctx()
    ctx.begin_turn("")
    assert ctx.messages == []


def test_tool_exchange_appends_call_and_result():
    ctx = _ctx()
    ctx.begin_turn("问")
    ctx.append_tool_exchange(
        call=ToolCall(id="call_1", name="call-chain", arguments='{"uid":"A"}'),
        tool_name="call-chain",
        result_text="A -> B",
    )

    assert [m.role for m in ctx.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL,
    ]
    assert ctx.messages[1].tool_calls[0].name == "call-chain"
    assert ctx.messages[2].tool_call_id == "call_1"
    assert ctx.messages[2].name == "call-chain"


def test_render_messages_includes_retrieval_and_tools():
    ctx = _ctx()
    ctx.begin_turn("问")
    ctx.append_retrieval("- a.B | 类 | 下单入口")
    ctx.append_tool_exchange(
        call=ToolCall(id="c1", name="call-chain", arguments="{}"),
        tool_name="call-chain",
        result_text="A -> B",
    )

    text = ctx.render_messages()
    assert "[用户] 问" in text
    assert "[检索结果]" in text
    assert "[助手调用工具]" in text
    assert "[工具 call-chain 返回]" in text


def test_to_llm_messages_shapes_tool_calls():
    ctx = _ctx()
    ctx.begin_turn("问")
    ctx.append_tool_exchange(
        call=ToolCall(id="c1", name="call-chain", arguments='{"uid":"A"}'),
        tool_name="call-chain",
        result_text="A -> B",
    )

    out = ctx.to_llm_messages()
    assert out[0] == {"role": "user", "content": "问"}
    assert out[1]["tool_calls"][0]["function"]["name"] == "call-chain"
    assert out[2]["role"] == "tool"
    assert out[2]["tool_call_id"] == "c1"


def test_retrieval_is_rendered_as_system_for_llm():
    """检索不是模型发起的，不能塞成 tool 消息（会违反 tool 协议）。"""
    ctx = _ctx()
    ctx.append_retrieval("- a.B")
    out = ctx.to_llm_messages()
    assert out[0]["role"] == "system"
    assert "检索结果" in out[0]["content"]


def test_prune_window_drops_retrieval_and_tool_but_keeps_origin_user():
    ctx = _ctx()
    ctx.begin_turn("第一问")
    ctx.append_retrieval("- a.B")
    ctx.append_tool_exchange(
        call=ToolCall(id="c1", name="call-chain", arguments="{}"),
        tool_name="call-chain",
        result_text="A -> B",
    )
    ctx.append_answer("第一答")

    ctx.prune_window(max_messages=40, max_chars=48000)

    roles = [m.role for m in ctx.messages]
    assert MessageRole.RETRIEVAL not in roles
    assert MessageRole.TOOL not in roles
    assert roles[0] is MessageRole.USER
    assert ctx.messages[0].content == "第一问"


def test_prune_window_keeps_origin_under_size_pressure():
    ctx = _ctx()
    ctx.begin_turn("最早的提问")
    for i in range(50):
        ctx.append_answer(f"很长的回答 {i} " + "x" * 200)

    ctx.prune_window(max_messages=5, max_chars=1000)

    assert ctx.messages[0].role is MessageRole.USER
    assert ctx.messages[0].content == "最早的提问"
    assert len(ctx.messages) <= 5


def test_prune_window_does_not_leave_dangling_tool_calls():
    """工具消息被移出后，assistant 上的 tool_calls 必须一起清掉。

    多步循环把 `messages` **原样**发给模型；留下没有配对结果的 `tool_call`
    会被接口直接 400 —— 而这个损坏发生在上一轮结束之后，很难追。
    """
    ctx = _ctx()
    ctx.begin_turn("查一下")
    ctx.append_tool_exchange(
        call=ToolCall(id="c1", name="call-chain", arguments='{"uid":"a.B"}'),
        tool_name="call-chain",
        result_text="链路",
    )
    ctx.append_answer("答案")

    ctx.prune_window(max_messages=40, max_chars=48000)

    assert all(m.role is not MessageRole.TOOL for m in ctx.messages)
    assert all(m.tool_calls is None for m in ctx.messages
               if m.role is MessageRole.ASSISTANT)
    # 发给模型的那份形状同样不能带孤儿 tool_calls
    assert all("tool_calls" not in m for m in ctx.to_llm_messages())


def test_old_context_version_is_rejected():
    with pytest.raises(Exception):
        AgentContext(conversation_id="c", project_id=1, version=1)

    assert CURRENT_CONTEXT_VERSION == 2


def test_turn_artifacts_are_not_serialized():
    """本轮产物不进快照 —— 否则 Redis 会被工具结果撑大。"""
    ctx = _ctx()
    ctx.begin_turn("问")
    ctx.turn.answer_message = "答"

    dumped = ctx.model_dump(mode="json", by_alias=True)
    assert "turn" not in dumped
    assert "messages" in dumped
