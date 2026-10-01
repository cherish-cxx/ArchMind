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

from app.models.base import CamelModel
from app.models.intent import Intent

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
    citations: list[Citation] = []
    ui_commands: list[UICmd] = []
    suggestions: list[Suggestion] = []
    steps: list[Step] = []
    meta: Meta
