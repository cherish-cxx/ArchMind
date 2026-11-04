"""工具执行入口（R6 第一阶段）—— 参数校验、起点检查、执行体调用。

六选一删除后，`execute_tool` 是唯一的调用协议；这些用例钉住
「错在打 Java 之前被拦住」和「归一化后的参数真的传下去了」。
"""

import pytest

from app.models.tool import ChainDirection
from app.tools import registry
from app.tools.errors import ErrorReason, ToolError
from app.tools.executor import execute_tool
from app.tools.java_client import ToolTransportError
from fakes import (
    ORDER_CONTROLLER,
    FakeJava,
    ok_chain,
    ok_overview,
)

OWNER = ORDER_CONTROLLER


def ctx(*known: str) -> registry.ToolContext:
    return registry.ToolContext(
        project_id=1, known_uids=frozenset(known) if known else None
    )


async def test_unknown_tool_fails_before_java():
    java = FakeJava(ok_overview())
    with pytest.raises(ToolError) as ei:
        await execute_tool("nope", java, ctx())
    assert ei.value.reason == ErrorReason.UNKNOWN_TOOL
    assert java.call_count == 0


async def test_unknown_start_fails_before_java():
    """起点不在本轮已见实体里 —— 告诉模型有哪些可选，而不是打一次 Java 再失败。"""
    java = FakeJava(ok_chain())
    with pytest.raises(ToolError) as ei:
        await execute_tool("call-chain", java, ctx(), {"uid": OWNER})
    assert ei.value.reason == ErrorReason.INVALID_ARGUMENTS
    assert java.call_count == 0


async def test_invalid_arguments_fail_before_java():
    java = FakeJava(ok_chain())
    with pytest.raises(ToolError) as ei:
        await execute_tool(
            "call-chain", java, ctx(OWNER), {"uid": OWNER, "direction": "BOTH"}
        )
    assert ei.value.reason == ErrorReason.INVALID_ARGUMENTS
    assert ei.value.field == "direction"
    assert java.call_count == 0


async def test_executes_with_normalized_defaults():
    java = FakeJava(ok_overview())
    envelope = await execute_tool(
        "overview", java, registry.ToolContext(project_id=1)
    )
    assert envelope.ok
    assert java.calls == [("overview", 1, None)]


async def test_executes_with_enum_conversion():
    java = FakeJava(ok_chain())
    await execute_tool(
        "call-chain", java, ctx(OWNER), {"uid": OWNER, "direction": "up"}
    )
    assert java.kwargs[0]["direction"] is ChainDirection.UP
    assert java.kwargs[0]["max_depth"] == 2


async def test_transport_error_propagates():
    """传输错误交给循环变成 tool 消息 —— 执行入口不吞。"""
    java = FakeJava(ok_chain(), raises=ToolTransportError("down"))
    with pytest.raises(ToolTransportError):
        await execute_tool("call-chain", java, ctx(OWNER), {"uid": OWNER})
