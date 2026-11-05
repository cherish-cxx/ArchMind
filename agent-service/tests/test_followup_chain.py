"""接口断链之后能不能自己补查实现类 —— R6 的核心承诺之一。

这条测的是**机制**，不是模型水平：脚本化的模型按「先追接口、再读实现」两步走，
循环必须允许第二步以第一步返回的 Impl uid 作为起点。
"""

from app.core.agent_loop import run_agent_turn
from app.models.response import NodeRef, NodeRefType
from app.models.tool import (
    CallChain,
    ChainEdge,
    ChainNode,
    ClassNode,
    Evidence,
    EvidenceKind,
    Gap,
    GapReason,
    MethodBody,
    MethodCode,
    NodeKind,
    RelationType,
    ToolEnvelope,
)
from fakes import FakeJava, FakeLLM
from test_agent_loop import ctx, reply, tool_call

INTERFACE = "com.hmall.trade.service.OrderService"
IMPL = "com.hmall.trade.service.impl.OrderServiceImpl"
IMPL_METHOD = f"{IMPL}#placeOrder(Long)"

ANSWER_IMPL = {
    "headline": "支付最终落在实现类",
    "sections": [{
        "title": "补查",
        "body": "`OrderService` 没有出边，继续看 `OrderServiceImpl`。",
        "citedUids": [IMPL],
    }],
}


def chain_with_gap() -> ToolEnvelope:
    """接口追下去断了，但扇出给出实现类 —— 这就是要补查的那个分支点。"""
    return ToolEnvelope(
        ok=True,
        data=CallChain(
            start=ClassNode(
                qualified_name=INTERFACE, simple_name="OrderService",
                package_name="com.hmall.trade.service", kind=NodeKind.INTERFACE,
            ),
            nodes=[ChainNode(uid=IMPL, depth=1, via_fanout=True, edge_count=1)],
            edges=[ChainEdge(from_=IMPL, to=INTERFACE, rel=RelationType.IMPLEMENTS,
                             call_count=1, via_fanout=True)],
            gaps=[Gap(at=INTERFACE, reason=GapReason.NO_DOWNSTREAM_EDGE)],
            truncated=False,
        ),
        evidence=[Evidence(kind=EvidenceKind.EDGE, from_=IMPL, to=INTERFACE,
                           rel=RelationType.IMPLEMENTS, count=1)],
        truncated=False,
        graph_rev=1,
    )


def impl_body() -> ToolEnvelope:
    return ToolEnvelope(
        ok=True,
        data=MethodBody(methods=[MethodCode(
            uid=IMPL_METHOD, start_line=10, end_line=12,
            signature="placeOrder(Long)", owner=IMPL,
            code="public void placeOrder(Long id) { tradeClient.pay(id); }",
        )], truncated=False),
        evidence=[Evidence(kind=EvidenceKind.METHOD, from_=IMPL_METHOD)],
        truncated=False,
        graph_rev=1,
    )


async def test_interface_gap_leads_to_impl_lookup():
    context = ctx(focus=NodeRef(type=NodeRefType.CLASS, uid=INTERFACE))
    java = FakeJava(chain_with_gap(), method_body_envelope=impl_body())
    llm = FakeLLM([
        reply(tool_call("c1", "call-chain",
                        {"uid": INTERFACE, "direction": "DOWN", "maxDepth": 2})),
        reply(tool_call("c2", "method-body", {"methodUid": IMPL_METHOD})),
        reply(tool_call("a1", "answer", ANSWER_IMPL)),
    ])

    resp = await run_agent_turn(context, "支付流程怎么走的", java, llm)

    assert [r.name for r in context.turn.tool_results] == ["call-chain", "method-body"]
    assert IMPL in {c.uid for c in resp.citations}
    assert "OrderServiceImpl" in resp.message
    assert any(g.at == INTERFACE for g in resp.graph.gaps)
