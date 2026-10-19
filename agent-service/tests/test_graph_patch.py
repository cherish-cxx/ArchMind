"""`core.graph.build_graph_patch` 的单元测试（P5）。

这一层只做归一化，不打网络、不调 LLM。要钉死的是三件事：

1. **三种工具的形状都能变成统一的 nodes/edges**；
2. **方向不能反**（上游 related→center，下游 center→related）；
3. **截断必须置位**，且不能留下悬空边。
"""

from app.core.graph import MAX_GRAPH_NODES, build_graph_patch
from app.models.response import AgentResponse, GraphEdge, GraphGap, GraphNode, GraphPatch, Meta
from app.models.tool import (
    ChainEdge,
    ChainNode,
    ClassNode,
    ClassRelations,
    Downstream,
    GapReason,
    MethodBody,
    MethodCode,
    NodeKind,
    RelatedClass,
    RelationType,
    ToolEnvelope,
    Upstream,
)
from fakes import ORDER_CONTROLLER, ORDER_SERVICE, failed, ok_chain, ok_overview


def _env(data, *, truncated: bool = False) -> ToolEnvelope:
    return ToolEnvelope(ok=True, data=data, evidence=[], truncated=truncated, graph_rev=1)


def _clazz(uid: str, name: str, kind: NodeKind = NodeKind.CLASS) -> ClassNode:
    package = uid.rsplit(".", 1)[0]
    return ClassNode(qualified_name=uid, simple_name=name, package_name=package, kind=kind)


# ==================== call-chain ====================


def test_call_chain_maps_nodes_edges_and_gaps():
    patch = build_graph_patch(ok_chain())

    assert patch is not None
    assert {n.uid for n in patch.nodes} == {ORDER_CONTROLLER, ORDER_SERVICE}
    # start 一定在图里，而且带显示信息
    start = next(n for n in patch.nodes if n.uid == ORDER_CONTROLLER)
    assert start.name == "OrderController"
    assert start.kind is NodeKind.CLASS
    assert start.package_name == "com.hmall.trade.controller"

    assert len(patch.edges) == 1
    edge = patch.edges[0]
    assert (edge.from_, edge.to, edge.rel) == (
        ORDER_CONTROLLER, ORDER_SERVICE, RelationType.CALLS)

    assert len(patch.gaps) == 1
    assert patch.gaps[0].at == ORDER_SERVICE
    assert patch.gaps[0].reason is GapReason.REMOTE_SERVICE
    assert patch.truncated is False


# ==================== class-relations ====================


def test_class_relations_keeps_edge_direction():
    center_uid = "com.x.OrderService"
    caller_uid = "com.x.OrderController"       # 上游：caller → center
    callee_uid = "com.x.OrderMapper"           # 下游：center → callee

    data = ClassRelations(
        center=_clazz(center_uid, "OrderService", NodeKind.INTERFACE),
        upstream=Upstream(callers=[
            RelatedClass(clazz=_clazz(caller_uid, "OrderController"),
                         relation_type=RelationType.CALLS),
        ]),
        downstream=Downstream(callees=[
            RelatedClass(clazz=_clazz(callee_uid, "OrderMapper", NodeKind.INTERFACE),
                         relation_type=RelationType.CALLS),
        ]),
    )

    patch = build_graph_patch(_env(data))

    assert patch is not None
    assert {n.uid for n in patch.nodes} == {center_uid, caller_uid, callee_uid}
    directions = {(e.from_, e.to, e.rel) for e in patch.edges}
    assert (caller_uid, center_uid, RelationType.CALLS) in directions
    assert (center_uid, callee_uid, RelationType.CALLS) in directions


def test_class_relations_dedupes_nodes_but_keeps_distinct_relations():
    center_uid = "com.x.A"
    other_uid = "com.x.B"
    data = ClassRelations(
        center=_clazz(center_uid, "A"),
        upstream=Upstream(
            callers=[RelatedClass(clazz=_clazz(other_uid, "B"),
                                  relation_type=RelationType.CALLS)],
            dependents=[RelatedClass(clazz=_clazz(other_uid, "B"),
                                     relation_type=RelationType.DEPENDS)],
        ),
    )

    patch = build_graph_patch(_env(data))

    assert patch is not None
    assert [n.uid for n in patch.nodes].count(other_uid) == 1
    assert {(e.from_, e.to, e.rel) for e in patch.edges} == {
        (other_uid, center_uid, RelationType.CALLS),
        (other_uid, center_uid, RelationType.DEPENDS),
    }


# ==================== method-body ====================


def test_method_body_contributes_the_owner_node():
    data = MethodBody(
        methods=[MethodCode(uid="com.x.S#run()", start_line=1, end_line=3, owner="com.x.S")],
        truncated=False,
    )

    patch = build_graph_patch(_env(data))

    assert patch is not None
    assert [(n.uid, n.name) for n in patch.nodes] == [("com.x.S", "S")]
    assert patch.edges == []


# ==================== 没有图数据的轮次 ====================


def test_overview_has_no_graph():
    assert build_graph_patch(ok_overview()) is None


def test_failed_tool_has_no_graph():
    assert build_graph_patch(failed()) is None


# ==================== 截断 ====================


def test_truncation_drops_dangling_edges():
    start = _clazz("com.x.N0", "N0")
    nodes = [
        ChainNode(uid=f"com.x.N{i}", depth=i, via_fanout=False, edge_count=1)
        for i in range(1, 100)
    ]
    edges = [
        ChainEdge(from_=f"com.x.N{i}", to=f"com.x.N{i + 1}",
                  rel=RelationType.CALLS, call_count=1, via_fanout=False)
        for i in range(99)
    ]
    from app.models.tool import CallChain
    data = CallChain(start=start, nodes=nodes, edges=edges, gaps=[], truncated=False)

    patch = build_graph_patch(_env(data))

    assert patch is not None
    assert len(patch.nodes) == MAX_GRAPH_NODES
    assert patch.truncated is True
    kept = {n.uid for n in patch.nodes}
    assert all(e.from_ in kept and e.to in kept for e in patch.edges)


# ==================== JSON 别名 ====================


def test_graph_serializes_with_camel_case_and_from_alias():
    patch = GraphPatch(
        nodes=[GraphNode(uid="com.x.A", name="A", kind=NodeKind.CLASS,
                         package_name="com.x")],
        edges=[GraphEdge(**{"from": "com.x.A", "to": "com.x.B",
                            "rel": RelationType.CALLS})],
        gaps=[GraphGap(at="com.x.B", reason=GapReason.NO_DOWNSTREAM_EDGE)],
    )
    resp = AgentResponse(
        message="x", graph=patch,
        meta=Meta(intent="EXPLORE_FLOW", confidence=1.0, graph_rev=1, latency_ms=1),
    )

    dumped = resp.model_dump(by_alias=True)

    assert dumped["graph"]["nodes"][0]["packageName"] == "com.x"
    assert dumped["graph"]["edges"][0]["from"] == "com.x.A"
    assert dumped["graph"]["edges"][0]["callCount"] == 1
    assert dumped["graph"]["gaps"][0]["reason"] == "NO_DOWNSTREAM_EDGE"
