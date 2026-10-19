"""把工具结果归一化成前端可渲染的 `GraphPatch`（P5）。

**为什么需要这一层**：5 个工具的数据形状各不相同（call-chain 是链、class-relations
是星形、method-body 是源码切片），前端不应该为每种工具写一套解析。这里统一成
`nodes + edges + gaps`，前端只认这一种形状。

**边界**：只做归一化，不做取舍 —— 讲解归 LLM，画图归前端，这里只负责把
「图里实际有什么」如实搬过去。没有图数据的工具返回 `None`，不是空 patch：
前端要能区分「这轮没有图」和「这轮图就是空的」。
"""

from app.models.response import GraphEdge, GraphGap, GraphNode, GraphPatch
from app.models.tool import CallChain, ClassNode, ClassRelations, MethodBody, ToolEnvelope

#: 单轮下发的上限。超了就截断并置 `truncated` —— 宁可少画，也不能让响应膨胀。
MAX_GRAPH_NODES = 80
MAX_GRAPH_EDGES = 160


def build_graph_patch(env: ToolEnvelope) -> GraphPatch | None:
    """按 **data 的形状**分派，而不是按 intent。

    理由：同一个 intent 可能因「有没有锚点」走到不同工具（有锚点走 call-chain，
    没锚点回落 classes），按形状判断才不会被这种分支绕过。
    """
    if env is None or not env.ok or env.data is None:
        return None

    data = env.data
    if getattr(data, "start", None) is not None and hasattr(data, "edges"):
        return _from_call_chain(env)
    if getattr(data, "center", None) is not None:
        return _from_class_relations(env)
    if hasattr(data, "methods"):
        return _from_method_body(env)
    # overview / classes 清单 / 其他：没有可画的图
    return None


# ==================== 三种工具的映射 ====================


def _from_call_chain(env: ToolEnvelope) -> GraphPatch:
    data: CallChain = env.data
    nodes: list[GraphNode] = []
    seen: set[str] = set()

    # start 是 ClassNode，不一定出现在 nodes 里（Java 侧 nodes 只放「下游节点」），
    # 但 SHOW_PATH 的 target 就是它 —— 必须保证它在图上。
    start = data.start
    nodes.append(_node_from_class(start))
    seen.add(start.qualified_name)

    for n in data.nodes:
        if n.uid in seen:
            continue
        seen.add(n.uid)
        nodes.append(GraphNode(
            uid=n.uid,
            name=n.name or _simple_name(n.uid),
            kind=n.kind,
            package_name=_package_of(n.uid),
            module=n.module,
            depth=n.depth,
            via_fanout=n.via_fanout,
            edge_count=n.edge_count,
        ))

    edges = [
        GraphEdge(**{
            "from": e.from_, "to": e.to, "rel": e.rel,
            "call_count": e.call_count, "via_fanout": e.via_fanout,
        })
        for e in data.edges
    ]
    gaps = [GraphGap(at=g.at, reason=g.reason) for g in data.gaps]

    return _cap(GraphPatch(
        nodes=nodes,
        edges=edges,
        gaps=gaps,
        truncated=bool(env.truncated or data.truncated),
    ))


def _from_class_relations(env: ToolEnvelope) -> GraphPatch:
    data: ClassRelations = env.data
    center = data.center
    nodes: dict[str, GraphNode] = {center.qualified_name: _node_from_class(center)}
    edges: list[GraphEdge] = []

    def add(related, *, upstream: bool) -> None:
        clazz = related.clazz
        nodes.setdefault(clazz.qualified_name, _node_from_class(clazz))
        # 上游：相关类 → 中心；下游：中心 → 相关类。方向不能反 ——
        # 前端画箭头时依赖它。
        source, target = (
            (clazz.qualified_name, center.qualified_name) if upstream
            else (center.qualified_name, clazz.qualified_name)
        )
        edges.append(GraphEdge(**{
            "from": source, "to": target, "rel": related.relation_type,
        }))

    if data.upstream is not None:
        for group in (data.upstream.callers, data.upstream.dependents,
                      data.upstream.subclasses):
            for related in group or []:
                add(related, upstream=True)

    if data.downstream is not None:
        for group in (data.downstream.callees, data.downstream.dependencies,
                      data.downstream.parents):
            for related in group or []:
                add(related, upstream=False)

    return _cap(GraphPatch(
        nodes=list(nodes.values()),
        edges=edges,
        truncated=bool(env.truncated),
    ))


def _from_method_body(env: ToolEnvelope) -> GraphPatch | None:
    data: MethodBody = env.data
    nodes: dict[str, GraphNode] = {}
    for method in data.methods:
        owner = method.owner or _owner_of(method.uid)
        if owner is None:
            continue
        nodes.setdefault(owner, GraphNode(
            uid=owner,
            name=_simple_name(owner),
            package_name=_package_of(owner),
        ))
    if not nodes:
        return None
    # 方法体没有边：它只是告诉前端「FOCUS_NODE 的 target 确实在图上」。
    return _cap(GraphPatch(nodes=list(nodes.values()), truncated=bool(env.truncated)))


# ==================== 去重 + 截断 ====================


def _cap(patch: GraphPatch) -> GraphPatch:
    """按 uid 去重、按上限截断；边只保留两端都还在的。

    顺序是先节点后边：节点被截掉时，指向它的边必须一起丢，否则前端会画出悬空边。
    """
    truncated = patch.truncated

    nodes: list[GraphNode] = []
    seen: set[str] = set()
    for node in patch.nodes:
        if node.uid in seen:
            continue
        seen.add(node.uid)
        nodes.append(node)
    if len(nodes) > MAX_GRAPH_NODES:
        nodes = nodes[:MAX_GRAPH_NODES]
        truncated = True

    kept = {n.uid for n in nodes}
    edges: list[GraphEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for edge in patch.edges:
        if edge.from_ not in kept or edge.to not in kept:
            continue
        key = (edge.from_, edge.to, edge.rel.value)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        edges.append(edge)
    if len(edges) > MAX_GRAPH_EDGES:
        edges = edges[:MAX_GRAPH_EDGES]
        truncated = True

    patch.nodes = nodes
    patch.edges = edges
    patch.truncated = truncated
    return patch


# ==================== 小工具 ====================


def _simple_name(uid: str) -> str:
    """`com.x.OrderService` → `OrderService`；方法 uid 先去掉 `#signature`。"""
    return _owner_of(uid).rsplit(".", 1)[-1]


def _package_of(uid: str) -> str | None:
    owner = _owner_of(uid)
    return owner.rsplit(".", 1)[0] if "." in owner else None


def _owner_of(uid: str) -> str:
    """方法 uid（`owner#signature`）→ owner；类 uid 原样返回。"""
    return uid.rsplit("#", 1)[0]


def _node_from_class(clazz: ClassNode) -> GraphNode:
    return GraphNode(
        uid=clazz.qualified_name,
        name=clazz.simple_name,
        kind=clazz.kind,
        package_name=clazz.package_name,
    )
