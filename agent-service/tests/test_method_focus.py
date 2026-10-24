"""方法锁（methodUid）—— 从请求字段到工具入参的那一段。

这一段最容易出的错是**静默错位**：锁了 A 方法却拿 B 方法的代码答题，
用户看不出来。所以几条规则都单独钉死。
"""

from app.models.context import AgentContext
from app.models.intent import Intent, Slots
from app.models.response import NodeRef, NodeRefType
from app.models.tool import ChainDirection
from app.tools import registry
from fakes import FakeJava, ok_chain

OWNER = "com.demo.OrderServiceImpl"
METHOD = "com.demo.OrderServiceImpl#create(Long)"


def ctx(**kwargs) -> AgentContext:
    return AgentContext(conversation_id="c_test", project_id=1, **kwargs)


def focus_owner() -> NodeRef:
    return NodeRef(type=NodeRefType.CLASS, uid=OWNER)


def test_invocation_carries_method_uid():
    inv = registry.build_invocation(ctx(focus=focus_owner(), method_uid=METHOD), Slots())

    assert inv.uid == OWNER, "类锚点保持不变 —— 画布仍停在类上"
    assert inv.method_uid == METHOD


def test_entity_ref_to_another_class_drops_the_lock():
    """用户显式问了另一个类 —— 旧锁必须失效，否则会「问 A、拿 B 的代码答」。"""
    inv = registry.build_invocation(
        ctx(focus=focus_owner(), method_uid=METHOD),
        Slots(entity_ref="com.demo.Other"),
    )

    assert inv.method_uid is None


def test_entity_ref_to_the_same_owner_keeps_the_lock():
    inv = registry.build_invocation(
        ctx(focus=focus_owner(), method_uid=METHOD),
        Slots(entity_ref=OWNER),
    )

    assert inv.method_uid == METHOD


def test_entity_ref_pointing_at_the_locked_method_keeps_the_lock():
    """「谁调用了它」里的「它」会被消解成方法 uid —— 那不是在换口径。"""
    inv = registry.build_invocation(
        ctx(focus=focus_owner(), method_uid=METHOD),
        Slots(entity_ref=METHOD),
    )

    assert inv.method_uid == METHOD
    assert inv.uid == OWNER, "方法 uid 不能当类 uid 传给类级工具，否则 Java 报「类不存在」"


async def test_call_chain_carries_method_uid():
    """锁了方法时，上下游只查这一个方法；否则「谁调用了它」会答成整个类。"""
    java = FakeJava(ok_chain())
    inv = registry.ToolInvocation(
        project_id=1, uid=OWNER, slots=Slots(direction=ChainDirection.UP), method_uid=METHOD
    )

    await registry._call_chain(java, inv)

    assert java.calls[0] == ("call-chain", 1, OWNER)
    assert java.kwargs[0]["method_uid"] == METHOD


def test_no_lock_means_no_method_uid():
    inv = registry.build_invocation(ctx(focus=focus_owner()), Slots())

    assert inv.method_uid is None


def test_plan_accepts_method_only_anchor_for_explore_code():
    """锁了方法、画布又没焦点时，EXPLORE_CODE 仍然可以调工具。"""
    spec = registry.plan(Intent.EXPLORE_CODE, ctx(method_uid=METHOD))

    assert spec is not None and spec.name == "method-body"


def test_plan_does_not_let_method_lock_satisfy_class_tools():
    """方法锁不是类锚点 —— 关系类工具没有类就没得查。"""
    assert registry.plan(Intent.EXPLORE_RELATION, ctx(method_uid=METHOD)) is None


async def test_method_body_passes_method_uid_and_wide_line_budget():
    java = FakeJava(ok_chain())
    inv = registry.ToolInvocation(project_id=1, uid=OWNER, slots=Slots(), method_uid=METHOD)

    await registry._method_body(java, inv)

    assert java.kwargs[0].get("method_uid") == METHOD
    assert java.kwargs[0].get("max_lines") == 300, "方法级只切一个方法，行数预算可以放宽"
    assert "uid" not in java.kwargs[0], "给了 method_uid 就不该再传类 uid"


async def test_method_body_falls_back_to_class_when_unlocked():
    java = FakeJava(ok_chain())
    inv = registry.ToolInvocation(project_id=1, uid=OWNER, slots=Slots())

    await registry._method_body(java, inv)

    assert java.calls[0][2] == OWNER, "没锁方法时仍按类切"
    assert "method_uid" not in java.kwargs[0]
