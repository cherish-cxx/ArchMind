"""Java `/internal/tools/*` 的契约模型 —— Python 侧唯一的契约真相。

字段名与 Java DTO 逐个对齐。改这里之前，先去读 Java 那边对应的类。
注意：这不是「Python 风格改写」的地方 —— 名字对不上就会丢字段。

关于 null：Java 那边没配 Jackson 的 NON_NULL，**可为空的字段会发成 `"key": null`**。
Pydantic 的 `= 默认值` 只在「键不存在」时生效，对显式 null 无效，
所以凡是 Java 可能发 null 的字段，类型里**必须**带 `| None`。
"""

from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")


# ==================== 地基 ====================


class CamelModel(BaseModel):
    """所有契约模型的基类：Python 侧写蛇形，JSON 里自动是驼峰。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ==================== 枚举（对应 Java 的 enum） ====================


class NodeKind(StrEnum):
    """ClassNode.kind"""

    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    ENUM = "ENUM"


class EvidenceKind(StrEnum):
    """Evidence.kind"""

    EDGE = "EDGE"
    CLASS = "CLASS"
    METHOD = "METHOD"
    FILE = "FILE"


class RelationType(StrEnum):
    """RelatedClass.relationType"""

    CALLS = "CALLS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    DEPENDS = "DEPENDS"


class RelationDirection(StrEnum):
    """class-relations 的 direction"""

    UP = "UP"
    DOWN = "DOWN"
    BOTH = "BOTH"


class ChainDirection(StrEnum):
    """call-chain 的 direction —— 没有 BOTH（DOWN 是多层 BFS，UP 是单层反查）"""

    DOWN = "DOWN"
    UP = "UP"


class GapReason(StrEnum):
    """链路断点的原因"""

    REMOTE_SERVICE = "REMOTE_SERVICE"
    INTERFACE_NO_IMPLEMENTATION = "INTERFACE_NO_IMPLEMENTATION"
    NO_DOWNSTREAM_EDGE = "NO_DOWNSTREAM_EDGE"      # 保留值，V1 不产出
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"    # 保留值，V1 不产出


# ==================== 信封（对应 ToolEnvelope.java / Evidence.java） ====================


class Evidence(CamelModel):
    """工具返回的「真实来源凭据」。

    `from_` 带下划线是因为 from 是 Python 关键字；Java 那边叫 from 完全合法。
    """

    kind: EvidenceKind
    from_: str = Field(alias="from")
    to: str | None = None
    rel: RelationType | None = None
    count: int | None = None


class ToolEnvelope(CamelModel, Generic[T]):
    """5 个工具共用的信封。ok=False 时 data 为 None、note 有原因，HTTP 仍是 200。

    `truncated` 故意**不给默认值**：Java 是原始 boolean，一定会发；
    一旦缺失就默认成 False，会把「被截断」讲成「完整」—— 这是最难发现的错误。
    """

    ok: bool
    data: T | None = None
    evidence: list[Evidence] = []   # Java 侧必定是 List.of() / new ArrayList<>()，不会是 null
    truncated: bool
    note: str | None = None
    graph_rev: int = 0              # 0 是合法值（表示从未落图），不是错误


# ==================== 公共积木 ====================


class ClassNode(CamelModel):
    """图里的一个类节点（含接口/枚举），对应 ClassNodeDTO。"""

    qualified_name: str              # = 图里的 uid
    simple_name: str
    package_name: str
    kind: NodeKind
    description: str | None = None   # 只有核心类有
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None


# ==================== 工具 ① overview ====================
# 数据源是 project_overview 表的 JSON 列（LLM 生成），字段大面积缺失是常态。
# 而且 overview 是**降级路径**（图挂了它还得能答），所以这一组故意全宽松 ——
# 缺字段只会让讲解变少，不会让讲解变错。


class TechStackItem(CamelModel):
    name: str | None = None
    category: str | None = None
    version: str | None = None
    role: str | None = None


class Architecture(CamelModel):
    style: str | None = None
    layers: list[str] | None = None
    description: str | None = None


class ModuleItem(CamelModel):
    name: str | None = None
    responsibility: str | None = None
    key_files: list[str] | None = None


class ProjectOverview(CamelModel):
    """对应 Java ProjectOverviewResponse"""

    project_type: str | None = None
    summary: str | None = None
    description: str | None = None
    tech_stack: list[TechStackItem] | None = None
    architecture: Architecture | None = None
    modules: list[ModuleItem] | None = None


# ==================== 工具 ② classes ====================
# data 就是 list[ClassNode]，公共积木里已经有了，这里不需要新类。


# ==================== 工具 ③ class-relations ====================


class RelatedClass(CamelModel):
    """对应 Java RelatedClassDTO。

    注意它的 evidence 是 `list[str]`（给人看的短句，如 "a() → b()"），
    和信封里那个结构化 `Evidence` **同名但完全不同类型**。
    """

    clazz: ClassNode
    relation_type: RelationType
    evidence: list[str] | None = None   # Java 侧显式判过 null


class Upstream(CamelModel):
    """谁指向我"""

    callers: list[RelatedClass] | None = None       # 谁调我
    dependents: list[RelatedClass] | None = None    # 谁依赖我
    subclasses: list[RelatedClass] | None = None    # 谁继承/实现我


class Downstream(CamelModel):
    """我指向谁"""

    callees: list[RelatedClass] | None = None       # 我调谁
    dependencies: list[RelatedClass] | None = None  # 我依赖谁
    parents: list[RelatedClass] | None = None       # 我继承/实现谁


class ClassRelations(CamelModel):
    """对应 Java ClassRelationsResponse"""

    center: ClassNode                  # 控制器无条件取它 → 必定非空
    upstream: Upstream | None = None   # Java 侧显式判过 null
    downstream: Downstream | None = None


# ==================== 工具 ④ call-chain ====================


class ChainNode(CamelModel):
    """调用链上的一个类节点。

    Java 里 depth / viaFanout / edgeCount 是原始类型，必定有值 → 必填。
    """

    uid: str
    depth: int
    via_fanout: bool
    edge_count: int
    name: str | None = None
    kind: NodeKind | None = None
    module: str | None = None          # 从 filePath 推导，推不出来就是 None


class ChainEdge(CamelModel):
    """调用链上的一条边。rel 实际只会是 CALLS 或 IMPLEMENTS。"""

    from_: str = Field(alias="from")   # ← from 又是 Python 关键字
    to: str
    rel: RelationType
    call_count: int
    via_fanout: bool


class Gap(CamelModel):
    """链路断点：**意外**终止的位置与原因"""

    at: str
    reason: GapReason


class CallChain(CamelModel):
    """对应 Java CallChainResponse。Agent 的核心场景。"""

    start: ClassNode
    nodes: list[ChainNode] = []        # Java 侧 new ArrayList<>()，不会是 null
    edges: list[ChainEdge] = []
    gaps: list[Gap] = []
    truncated: bool                    # 同 ToolEnvelope：不给默认值


# ==================== 工具 ⑤ method-body ====================


class MethodCode(CamelModel):
    """一段方法源码切片。"""

    uid: str
    start_line: int                    # Java 原始类型 int
    end_line: int
    signature: str | None = None
    owner: str | None = None
    code: str | None = None
    error: str | None = None           # 源码读不到时的原因
    resolved_from: str | None = None   # 请求接口方法、实际切了实现类时，填原接口方法 uid


class MethodBody(CamelModel):
    """对应 Java MethodBodyResponse。「讲解深度」的唯一来源。"""

    methods: list[MethodCode] = []
    truncated: bool                    # 同 ToolEnvelope：不给默认值
    note: str | None = None            # 降级说明
