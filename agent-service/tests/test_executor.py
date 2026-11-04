"""统一执行入口的测试（R6 第一阶段）。

覆盖两件事：legacy 的 `ToolInvocation → args` 翻译是否保真，
以及执行入口是否在调用 Java 之前完成校验和锚点检查。
"""

import pytest

from app.models.intent import Slots
from app.models.tool import ChainDirection
from app.tools import registry
from app.tools.errors import ErrorReason, ToolError
from app.tools.executor import execute_tool
from app.tools.java_client import ToolTransportError
from fakes import FakeJava, ok_chain, ok_overview

OWNER = "com.demo.OrderServiceImpl"
METHOD = "com.demo.OrderServiceImpl#create(Long)"


def invocation(**kw) -> registry.ToolInvocation:
    base = dict(project_id=1, uid=OWNER, slots=Slots())
    base.update(kw)
    return registry.ToolInvocation(**base)


# ==================== legacy 参数适配 ====================


def test_overview_has_no_args():
    assert registry.invocation_args("overview", invocation()) == {}


def test_classes_keeps_default_limit():
    assert registry.invocation_args("classes", invocation()) == {"limit": 40}


def test_relations_maps_none_to_both():
    args = registry.invocation_args("class-relations", invocation())
    assert args == {"uid": OWNER, "direction": "BOTH", "limit": 40}

    up = registry.invocation_args(
        "class-relations", invocation(slots=Slots(direction=ChainDirection.UP))
    )
    assert up["direction"] == "UP"


def test_chain_maps_direction_depth_and_method():
    args = registry.invocation_args(
        "call-chain",
        invocation(
            slots=Slots(direction=ChainDirection.DOWN, depth=3),
            method_uid=METHOD,
        ),
    )
    assert args == {
        "uid": OWNER,
        "direction": "DOWN",
        "maxDepth": 3,
        "maxNodes": 40,
        "methodUid": METHOD,
    }


def test_method_body_line_budget_depends_on_lock():
    locked = registry.invocation_args(
        "method-body", invocation(method_uid=METHOD)
    )
    assert locked["maxLines"] == 300
    assert locked["methodUid"] == METHOD
    assert "uid" not in locked

    unlocked = registry.invocation_args("method-body", invocation())
    assert unlocked["maxLines"] == 120
    assert unlocked["uid"] == OWNER


# ==================== 执行入口 ====================


async def test_unknown_tool_fails_before_java():
    java = FakeJava(ok_overview())
    with pytest.raises(ToolError) as ei:
        await execute_tool("nope", java, registry.ToolContext(project_id=1))
    assert ei.value.reason == ErrorReason.UNKNOWN_TOOL
    assert java.call_count == 0


async def test_missing_anchor_fails_before_java():
    java = FakeJava(ok_chain())
    with pytest.raises(ToolError) as ei:
        await execute_tool(
            "call-chain",
            java,
            registry.ToolContext(project_id=1, has_anchor=False),
            {"uid": OWNER},
        )
    assert ei.value.reason == ErrorReason.MISSING_ANCHOR
    assert java.call_count == 0


async def test_invalid_arguments_fail_before_java():
    java = FakeJava(ok_chain())
    with pytest.raises(ToolError) as ei:
        await execute_tool(
            "call-chain",
            java,
            registry.ToolContext(project_id=1, has_anchor=True),
            {"uid": OWNER, "direction": "BOTH"},
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
        "call-chain",
        java,
        registry.ToolContext(project_id=1, has_anchor=True),
        {"uid": OWNER, "direction": "up"},
    )
    assert java.kwargs[0]["direction"] is ChainDirection.UP
    assert java.kwargs[0]["max_depth"] == 2


async def test_transport_error_still_propagates_for_legacy():
    java = FakeJava(ok_chain(), raises=ToolTransportError("down"))
    with pytest.raises(ToolTransportError):
        await execute_tool(
            "call-chain",
            java,
            registry.ToolContext(project_id=1, has_anchor=True),
            {"uid": OWNER},
        )
