"""Python → Java 的响应契约：`AgentResponse` 及其零件。

形状来自 [9月19 Agent v1设计] §4-② / §4-③。
**P2 的桩也必须用这个最终形状** —— P4 的 Java 网关要照它写透传，
P3 只换 `message` 的来源（模板 → LLM），不产生接口变更。

⚠️ 两处设计文档只出现过单个取值的枚举，是按图模型推的，**待确认**：
  - `NodeRefType`：示例里只出现过 `CLASS`；`METHOD` 是照「图里有 Method 节点」推的
  - `StepType`：示例里出现过 `INTENT` / `TOOL` / `LLM`，是否还有别的（如四道拦截）待定
"""

from enum import StrEnum
from typing import Any

from pydantic import Field

from app.models.base import CamelModel
from app.models.intent import Intent
from app.models.tool import GapReason, NodeKind, RelationType

# ==================== 实体引用 ====================


class NodeRefType(StrEnum):
    """`target` / `focus` / `citations[].kind` 指向的实体类型。

    注意与 `tool.NodeKind` 区分：那个说的是「这个类节点是 CLASS 还是 INTERFACE」，
    这个说的是「我指向的是一个类还是一个方法」。
    """

    CLASS = "CLASS"
    METHOD = "METHOD"


class NodeRef(CamelModel):
    """指向图里一个实体的引用。`focus` 用它，`uiCommands[].target` 也用它。"""

    type: NodeRefType
    uid: str


# ==================== 零件 ====================


class Citation(CamelModel):
    """讲解里引用的出处。**必须来自本轮 tool.evidence**，否则就是编造。

    这是 grounding 的抓手：Java 侧只需校验 `citations[].uid` 是否出现在本轮
    evidence 里（9月19 §8-A2）。
    """

    uid: str
    kind: NodeRefType


class Section(CamelModel):
    """讲解的一段。**段落是引用的最小单位** —— 一段话共享一组出处。

    `body` 是 Markdown。`cited_uids` **允许为空**：空数组不是「忘了填」，而是
    「这段在讲图的边界 / 做负向断言，图上没有对应的边可引」。
    别把它当缺省值「顺手补上」—— 补了就等于伪造出处。
    """

    title: str = ""
    body: str
    cited_uids: list[str] = []


class UiOp(StrEnum):
    """V1 只做 4 个（9月19 §4-③ 从 9 个砍下来的）。

    `Literal` 钉死取值 —— LLM 想返回不存在的 op，在模型校验层就被拒，不用等前端。
    """

    FOCUS_NODE = "FOCUS_NODE"      # 选中 + 居中
    EXPAND_NODE = "EXPAND_NODE"    # 拉出邻居
    SHOW_PATH = "SHOW_PATH"        # call-chain 的天然输出形态
    CLEAR_VIEW = "CLEAR_VIEW"      # 重置


class UICmd(CamelModel):
    """一条给前端的可视化命令。

    **LLM 只声明「要看什么」，怎么画是前端的事** —— 图结构绝不交给 LLM 拼。
    """

    op: UiOp
    target: NodeRef | None = None
    params: dict[str, Any] = {}
    rev: int


# ==================== 图数据（P5） ====================
# 命令（UICmd）只说「看哪里」；这里才是「那里有什么、怎么连」。
# 两者是**两个输入**：命令改变视图状态，数据填充图 store。


class GraphNode(CamelModel):
    """前端渲染用的节点。uid 是身份，name/kind 是显示信息。"""

    uid: str
    name: str | None = None
    kind: NodeKind | None = None
    package_name: str | None = None
    module: str | None = None
    depth: int | None = None          # 仅 call-chain 有
    via_fanout: bool = False
    edge_count: int | None = None
    description: str | None = None    # 没有描述时前端按需补齐
    is_abstract: bool | None = None
    core: bool | None = None


class GraphEdge(CamelModel):
    """一条要画出来的边。`from_` 在 Python 是关键字，JSON 里仍是 `from`。"""

    from_: str = Field(alias="from")
    to: str
    rel: RelationType
    call_count: int = 1
    via_fanout: bool = False


class GraphGap(CamelModel):
    """链路断点：前端用「虚线/未解析」标出来，不能画成没这回事。"""

    at: str
    reason: GapReason


class GraphPatch(CamelModel):
    """一轮回答对应的证据子图。

    前端把它 **upsert** 进现有画布（不是替换整张图）。
    `truncated=True` 表示达到上限被截断 —— 必须让用户看见「这里不是全部」。
    """

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    gaps: list[GraphGap] = []
    truncated: bool = False


class Suggestion(CamelModel):
    """「下一步可以问什么」。闭环的发动机 —— 没有它，对话转不动。

    `intent` 在 P2 是裸 `str`（那时枚举还没定义），P3 收紧成 `Intent`：
    前端点一个建议就是发一轮 `ask`，这个字段最终会变成下一轮的 Intent ——
    让它现在就是个枚举，错值在模型校验层就被拒，不用等前端点下去才发现。
    """

    label: str
    intent: Intent
    slots: dict[str, Any] = {}


class StepType(StrEnum):
    """`steps[].type`。给用户回放、给自己审计。"""

    INTENT = "INTENT"
    TOOL = "TOOL"
    LLM = "LLM"


class Step(CamelModel):
    """一轮问答里的一步。`ms` 让「慢在哪」一眼可见。"""

    n: int
    type: StepType
    detail: str
    ms: int


class Meta(CamelModel):
    """这一轮的元信息。`graphRev` 让上层能发现「我讲的是旧图」。"""

    intent: str
    confidence: float
    graph_rev: int
    latency_ms: int


# ==================== 顶层响应 ====================


class AgentResponse(CamelModel):
    """`POST /internal/agent/ask` 的响应 —— 就是 P4 网关要透传的那个形状。"""

    conversation_id: str | None = None   # P4 才有；P2 一律 None
    message: str
    #: 一句话结论。和 `sections` 一起给前端做逐段渲染；降级时为 None。
    headline: str | None = None
    #: 结构化讲解：**唯一真相**。`message` 由它拼出来（降级时反过来，sections 为空）。
    #: 顶层 `citations` 是各段 citedUids 的并集，旧前端读它不受影响。
    sections: list[Section] = []
    #: ★ P4.2：请求带了 cid、但 Redis 里的 Context 已过期（TTL 2h）。
    #: **给前端一个能直接分支的布尔**，而不是让它去匹配文案。
    #: 前端据此把那个会话窗口置灰，并提示「对话已过期，请开始新的对话」。
    session_expired: bool = False
    citations: list[Citation] = []
    ui_commands: list[UICmd] = []
    #: P5：本轮用于渲染的证据子图。None = 这轮没有图数据（不是「图是空的」）。
    graph: GraphPatch | None = None
    suggestions: list[Suggestion] = []
    steps: list[Step] = []
    meta: Meta
