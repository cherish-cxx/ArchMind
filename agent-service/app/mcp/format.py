"""MCP 工具的响应格式。

所有工具共用的三块：项目级 meta、索引状态摘要、失败信封。
放在这里是为了让每个工具返回的 `meta` 形状只有一个来源 ——
外部 agent 靠 `graphRev` 判断快照新旧，字段飘了就失去意义。
"""

import re

from app.mcp.principal import ProjectContext
from app.models.tool import CallChain, ChainNode, GapReason


def index_meta(state: dict | None) -> dict:
    """把 Redis 里的索引状态压成对外摘要。

    没有状态（从未索引）也返回合法结构：`status="absent"`、`rev=0`，
    让调用方不必区分「没有键」和「状态为 null」。
    `coverage` 只在 actual/expected 都存在时给出 —— 缺失时省略，
    比编一个 `0/0` 更诚实。
    """
    if not state:
        return {"status": "absent", "rev": 0}
    out: dict = {
        "status": state.get("status") or "absent",
        "rev": state.get("activeRev") or 0,
    }
    actual = state.get("actual")
    expected = state.get("expected")
    if actual is not None and expected is not None:
        out["coverage"] = f"{actual}/{expected}"
    return out


def meta(
    ctx: ProjectContext, index: dict | None = None, truncated: bool = False
) -> dict:
    """项目级元信息；数据工具必带 `graphRev`。"""
    out: dict = {"projectId": ctx.project_id, "graphRev": ctx.graph_rev}
    if index is not None:
        out["index"] = index
    out["truncated"] = truncated
    return out


def tool_error(code: str, message: str, hint: str = "") -> dict:
    """预期失败的信封。**不抛协议错误** —— 让 agent 读到原因并自行纠偏。"""
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


# ==================== trace_flow 的层推断与整形 ====================
# 图里没有「层」这个事实，只有包名和类名。这里的一切都是**猜测**，
# 所以每个节点都带 `inferred: true` / `layerBasis: "package-name"`，
# 让 agent 知道这不是可以当证据用的字段。

#: service 类的简单名：IOrderService / OrderService / OrderServiceImpl。
_SERVICE_NAME = re.compile(r"I?[A-Z]\w*Service(Impl)?")

#: 断点原因 → 给人看的中文提示。
_GAP_HINTS: dict[str, str] = {
    GapReason.REMOTE_SERVICE.value: "跨服务：对端实现不在本图",
    GapReason.INTERFACE_NO_IMPLEMENTATION.value: "接口在本项目中找不到实现",
}
_GAP_HINT_DEFAULT = "链路在此终止：没有可继续追踪的下游"


def infer_layer(uid: str) -> str:
    """按包名/简单名推断一个类在分层架构中的角色。

    顺序即优先级：包名比类名更可信（`service.impl` 先命中 service），
    因此先按「包名含关键词」判断，再看简单名后缀/正则。
    推断不出来就是 `other` —— 不猜。
    """
    fqn = uid.split("#", 1)[0]
    package, _, simple = fqn.rpartition(".")
    pkg = package.lower()

    if "controller" in pkg or simple.endswith("Controller"):
        return "controller"
    if "service" in pkg or _SERVICE_NAME.fullmatch(simple):
        return "service"
    if (
        "mapper" in pkg
        or "repository" in pkg
        or "dao" in pkg
        or simple.endswith(("Mapper", "Repository", "Dao"))
    ):
        return "repository"
    if (
        "client" in pkg
        or "feign" in pkg
        or "api" in pkg
        or simple.endswith("Client")
    ):
        return "client"
    if "config" in pkg:
        return "config"
    return "other"


def _shape_node(node: ChainNode) -> dict:
    """一个链路节点的对外形状。层标签永远是推断，必须带来源说明。"""
    return {
        "uid": node.uid,
        "name": node.name,
        "kind": node.kind.value if node.kind is not None else None,
        "module": node.module,
        "layer": infer_layer(node.uid),
        "inferred": True,
        "layerBasis": "package-name",
        "depth": node.depth,
        "edgeCount": node.edge_count,
    }


def _shape_start(data: CallChain) -> dict:
    """起点。ClassNode 没有 depth/module/edgeCount，这里按同形补齐。

    `edgeCount` 取图中从起点出发的边数 —— 和 ChainNode 的语义对齐，
    避免 agent 在 start 与 stages 之间遇到两套字段。
    """
    start = data.start
    uid = start.qualified_name
    return {
        "uid": uid,
        "name": start.simple_name,
        "kind": start.kind.value,
        "module": None,
        "layer": infer_layer(uid),
        "inferred": True,
        "layerBasis": "package-name",
        "depth": 0,
        "edgeCount": sum(1 for e in data.edges if e.from_ == uid),
    }


def shape_flow(data: CallChain) -> dict:
    """把 `CallChain` 按深度整理成逐层展开的调用栈。

    同一 depth 的节点算一个 stage，按 `module`、`uid` 排序让输出稳定
    （agent 反复调用同一 uid 应该看到同样的顺序）。stage 的 `layer` 取
    排序后第一个节点的层，仅作概览；判断单个类请看节点自己的 `layer`。
    不生成自然语言总结 —— 那是 agent 的活。
    """
    by_depth: dict[int, list[ChainNode]] = {}
    for node in data.nodes:
        by_depth.setdefault(node.depth, []).append(node)

    stages: list[dict] = []
    for depth in sorted(by_depth):
        ordered = sorted(by_depth[depth], key=lambda n: (n.module or "", n.uid))
        shaped_nodes = [_shape_node(n) for n in ordered]
        stages.append(
            {
                "depth": depth,
                "layer": shaped_nodes[0]["layer"],
                "nodes": shaped_nodes,
            }
        )

    breaks = [
        {
            "at": gap.at,
            "reason": gap.reason.value,
            "hint": _GAP_HINTS.get(gap.reason.value, _GAP_HINT_DEFAULT),
        }
        for gap in data.gaps
    ]
    edges = [
        {
            "from": edge.from_,
            "to": edge.to,
            "rel": edge.rel.value,
            "callCount": edge.call_count,
            "viaFanout": edge.via_fanout,
        }
        for edge in data.edges
    ]

    return {
        "start": _shape_start(data),
        "stages": stages,
        "edges": edges,
        "breaks": breaks,
        "counts": {
            "stages": len(stages),
            "nodes": len(data.nodes),
            "edges": len(data.edges),
            "breaks": len(breaks),
        },
        "truncated": data.truncated,
    }
