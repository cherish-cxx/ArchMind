"""R7 Task 8：`archmind_trace_flow`。

两部分：
1. 纯整形测试 —— `shape_flow` / `infer_layer` 是确定性的，直接断言形状，
   不碰任何外部依赖。
2. 工具测试 —— 用 FakeJava 返回 `CallChain` 信封，验证参数校验、错误翻译
   与 meta 的 `graphRev`。
"""

import pytest

from app.mcp import deps
from app.mcp.deps import McpServices
from app.mcp.format import infer_layer, shape_flow
from app.mcp.principal import Principal, ProjectContext, principal_scope
from app.mcp.tools import archmind_trace_flow
from app.models.tool import (
    CallChain,
    ChainDirection,
    ChainEdge,
    ChainNode,
    ClassNode,
    Gap,
    GapReason,
    NodeKind,
    RelationType,
)
from tests.fakes import FakeJava, failed, ok_chain

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeContext:
    """只实现工具用到的 `context()`：归属校验 + 项目上下文。"""

    def __init__(self, *, graph_rev: int = 7):
        self.graph_rev = graph_rev
        self.project_calls: list[int] = []

    async def context(self, principal: Principal, project_id: int) -> ProjectContext:
        self.project_calls.append(project_id)
        return ProjectContext(
            user_id=principal.user_id,
            pat_id=principal.pat_id,
            project_id=project_id,
            project_name="hmall",
            analysis_status="done",
            graph_rev=self.graph_rev,
        )


def configure_with_fakes(*, java, context=None) -> None:
    deps.configure(
        McpServices(
            java=java,
            retriever=None,
            index_store=None,
            context=context or FakeContext(),
            limiter=None,
        )
    )


@pytest.fixture(autouse=True)
def _clean_deps():
    deps.reset()
    yield
    deps.reset()


# ==================== 构造辅助 ====================


def node(
    uid: str, depth: int = 0, *, module: str | None = None, edge_count: int = 1
) -> ChainNode:
    return ChainNode(
        uid=uid,
        depth=depth,
        via_fanout=False,
        edge_count=edge_count,
        name=uid.rsplit(".", 1)[-1],
        kind=NodeKind.CLASS,
        module=module,
    )


def sample_chain(
    nodes: list[ChainNode],
    *,
    gaps: list[Gap] | None = None,
    edges: list[ChainEdge] | None = None,
    truncated: bool = False,
) -> CallChain:
    first = nodes[0].uid if nodes else "com.hmall.App"
    package, _, simple = first.rpartition(".")
    return CallChain(
        start=ClassNode(
            qualified_name=first,
            simple_name=simple,
            package_name=package,
            kind=NodeKind.CLASS,
        ),
        nodes=list(nodes),
        edges=list(edges or []),
        gaps=list(gaps or []),
        truncated=truncated,
    )


# ==================== shape_flow ====================


def test_groups_by_depth_and_marks_inferred_layers():
    data = sample_chain(
        [
            node("com.hmall.controller.OrderController", depth=0),
            node("com.hmall.service.IOrderService", depth=1),
            node("com.hmall.service.impl.OrderServiceImpl", depth=2),
            node("com.hmall.mapper.OrderMapper", depth=3),
        ]
    )

    out = shape_flow(data)

    assert [s["depth"] for s in out["stages"]] == [0, 1, 2, 3]
    assert out["stages"][0]["layer"] == "controller"
    assert out["stages"][1]["layer"] == "service"
    assert out["stages"][2]["layer"] == "service"
    assert out["stages"][3]["layer"] == "repository"
    first = out["stages"][0]["nodes"][0]
    assert first["uid"] == "com.hmall.controller.OrderController"
    assert first["inferred"] is True
    assert first["layerBasis"] == "package-name"


def test_within_stage_sorts_by_module_then_uid():
    data = sample_chain(
        [
            node("com.hmall.trade.service.BService", depth=1, module="trade"),
            node("com.hmall.cart.service.CService", depth=1, module="cart"),
            node("com.hmall.common.service.AService", depth=1, module=None),
        ]
    )

    out = shape_flow(data)

    uids = [n["uid"] for n in out["stages"][0]["nodes"]]
    assert uids == [
        "com.hmall.common.service.AService",  # module None 排最前
        "com.hmall.cart.service.CService",
        "com.hmall.trade.service.BService",
    ]


@pytest.mark.parametrize(
    "uid, expected",
    [
        ("com.hmall.controller.OrderController", "controller"),
        ("com.hmall.web.PayController", "controller"),
        ("com.hmall.service.IOrderService", "service"),
        ("com.hmall.service.OrderService", "service"),
        ("com.hmall.service.impl.OrderServiceImpl", "service"),
        ("com.hmall.mapper.OrderMapper", "repository"),
        ("com.hmall.dao.OrderDao", "repository"),
        ("com.hmall.repository.OrderRepository", "repository"),
        ("com.hmall.api.client.CartClient", "client"),
        ("com.hmall.feign.StockFeign", "client"),
        ("com.hmall.common.config.RedisConfig", "config"),
        ("com.hmall.common.util.StringUtil", "other"),
    ],
)
def test_infer_layer(uid, expected):
    assert infer_layer(uid) == expected


def test_maps_gaps_to_breaks_with_hint():
    data = sample_chain(
        [],
        gaps=[
            Gap(at="com.hmall.api.client.CartClient", reason=GapReason.REMOTE_SERVICE),
            Gap(
                at="com.hmall.service.IOrderService",
                reason=GapReason.INTERFACE_NO_IMPLEMENTATION,
            ),
            Gap(at="com.hmall.X", reason=GapReason.NO_DOWNSTREAM_EDGE),
        ],
    )

    out = shape_flow(data)

    assert out["breaks"][0]["reason"] == "REMOTE_SERVICE"
    assert "跨服务" in out["breaks"][0]["hint"]
    assert out["breaks"][1]["reason"] == "INTERFACE_NO_IMPLEMENTATION"
    assert "找不到实现" in out["breaks"][1]["hint"]
    # 其余原因走通用提示，不编造具体语义。
    assert out["breaks"][2]["reason"] == "NO_DOWNSTREAM_EDGE"
    assert out["breaks"][2]["hint"]


def test_truncated_flag_propagates():
    data = sample_chain(
        [node("com.hmall.service.IOrderService", depth=1)], truncated=True
    )

    assert shape_flow(data)["truncated"] is True


def test_counts_and_edges_shape():
    data = sample_chain(
        [
            node("com.hmall.controller.OrderController", depth=0),
            node("com.hmall.service.OrderService", depth=1),
            node("com.hmall.mapper.OrderMapper", depth=1),
        ],
        edges=[
            ChainEdge(
                from_="com.hmall.controller.OrderController",
                to="com.hmall.service.OrderService",
                rel=RelationType.CALLS,
                call_count=2,
                via_fanout=True,
            ),
        ],
        gaps=[Gap(at="com.hmall.service.OrderService", reason=GapReason.REMOTE_SERVICE)],
    )

    out = shape_flow(data)

    assert out["counts"] == {"stages": 2, "nodes": 3, "edges": 1, "breaks": 1}
    assert out["edges"] == [
        {
            "from": "com.hmall.controller.OrderController",
            "to": "com.hmall.service.OrderService",
            "rel": "CALLS",
            "callCount": 2,
            "viaFanout": True,
        }
    ]
    assert out["start"]["uid"] == "com.hmall.controller.OrderController"
    assert out["start"]["layer"] == "controller"
    assert out["start"]["edgeCount"] == 1


# ==================== archmind_trace_flow ====================


@pytest.mark.asyncio
async def test_success_returns_stages_evidence_and_graph_rev():
    java = FakeJava(envelope=ok_chain())
    context = FakeContext(graph_rev=7)
    configure_with_fakes(java=java, context=context)

    with principal_scope(GOOD):
        out = await archmind_trace_flow(1, "com.hmall.trade.controller.OrderController")

    assert out["ok"] is True
    assert [s["depth"] for s in out["stages"]] == [1]
    assert out["stages"][0]["nodes"][0]["uid"] == (
        "com.hmall.trade.service.impl.OrderServiceImpl"
    )
    assert out["counts"] == {"stages": 1, "nodes": 1, "edges": 1, "breaks": 1}
    assert out["evidence"][0]["from"] == "com.hmall.trade.controller.OrderController"
    assert out["meta"]["graphRev"] == 7
    assert out["meta"]["truncated"] is False

    # 参数要真的传到 Java —— direction 固定 DOWN，预算显式传。
    assert java.kwargs[0]["direction"] == ChainDirection.DOWN
    assert java.kwargs[0]["max_depth"] == 3
    assert java.kwargs[0]["max_nodes"] == 60


@pytest.mark.asyncio
async def test_java_failure_is_tool_failed():
    java = FakeJava(envelope=failed("类不存在"))
    configure_with_fakes(java=java)

    with principal_scope(GOOD):
        out = await archmind_trace_flow(1, "com.hmall.NoSuchClass")

    assert out["ok"] is False
    assert out["error"]["code"] == "tool_failed"
    assert out["error"]["message"] == "类不存在"


@pytest.mark.asyncio
async def test_blank_uid_is_invalid_and_skips_java():
    java = FakeJava()
    context = FakeContext()
    configure_with_fakes(java=java, context=context)

    with principal_scope(GOOD):
        out = await archmind_trace_flow(1, "   ")

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert "archmind_search_entities" in out["error"]["hint"]
    # 参数校验先于归属校验：非法输入不该打 Java。
    assert context.project_calls == []
    assert java.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_depth", [0, 6])
async def test_max_depth_out_of_range_is_invalid(max_depth):
    java = FakeJava()
    context = FakeContext()
    configure_with_fakes(java=java, context=context)

    with principal_scope(GOOD):
        out = await archmind_trace_flow(
            1, "com.hmall.service.OrderService", maxDepth=max_depth
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert context.project_calls == []
    assert java.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_nodes", [0, 201])
async def test_max_nodes_out_of_range_is_invalid(max_nodes):
    java = FakeJava()
    configure_with_fakes(java=java)

    with principal_scope(GOOD):
        out = await archmind_trace_flow(
            1, "com.hmall.service.OrderService", maxNodes=max_nodes
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert java.call_count == 0
