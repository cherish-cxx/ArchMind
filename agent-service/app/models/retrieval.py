"""检索的产物 —— 候选类，以及"这一轮检索到底跑没跑"的说明。

**候选不是证据。** 证据只来自工具返回（`guard.evidence_uids` 的输入）。
候选是"向量觉得像"的类，从没被 Neo4j 查过，所以它不能进 `citedUids`、
不能让 UICommand 指过去 —— 这两件事的校验在 `core/guard.py`，这里只是数据形状。
"""

from dataclasses import dataclass, field

from app.models.tool import NodeKind

#: payload 里的 kind → 给 LLM 看的中文标签。模型不知道 CLASS 是什么，但知道"类"。
KIND_LABELS: dict[str, str] = {
    NodeKind.CLASS.value: "类",
    NodeKind.INTERFACE.value: "接口",
    NodeKind.ENUM.value: "枚举",
}


@dataclass(frozen=True)
class Candidate:
    """一个可能是用户在问的类。

    `uid` 是**全限定名**，也就是 `slots.entityRef` 和工具参数要的那个值 ——
    检索的全部意义，就是把「用户说的那句话」变成「图认得的这个字符串」。
    """

    uid: str
    simple_name: str
    package_name: str
    kind: str
    description: str | None
    rev: int
    #: RRF 融合分。**只能排序，不能设阈值，也不能给用户看** ——
    #: 它不是相似度，跨轮次之间没有可比性。
    score: float
    #: 索引时写入的方法名（payload 里带），给 LLM1 消歧用；不是证据。
    methods: tuple[str, ...] = ()

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, "类")


@dataclass(frozen=True)
class CapabilityCandidate:
    """一个可能是用户在问的业务能力（如 `POST /orders`）。

    **候选不是证据** —— 和 `Candidate` 同一条边界：它只用来把
    「用户说的业务词」引导到入口类，不能进 `citedUids`，也不能直接让画布指过去。
    `entry_uid` 才是能照抄去调工具的那个 uid。
    """

    uid: str
    name: str
    description: str | None
    area_uid: str
    area_name: str
    entry_uid: str
    entry_kind: str | None
    member_count: int
    rev: int
    score: float


# frozen=True = 这个对象一旦创建，里面所有字段不能改，只读
@dataclass(frozen=True)
class Retrieval:
    """一次检索的完整结果。

    `attempted` 与 `reason` 必须分开：**"没查"和"查了没中"是两件事**。
    前者说明索引/服务不可用，后者说明检索质量不够，排查方向完全不同。

    ⚠️ `attempted` / `reason` / `partial` 仍然**只描述类检索**（R4 的回归基准）。
    能力检索的成败单独放在 `capability_reason`，互不覆盖。
    """

    candidates: list[Candidate] = field(default_factory=list)
    #: 是否真的走到了 Qdrant（embed 失败时为 False —— 根本没发出查询）
    attempted: bool = False
    #: attempted 为 False 时是跳过原因（absent / not_ready / failed / embed_failed）；
    #: 为 True 时是查询失败原因（qdrant_failed）；成功时为 None
    reason: str | None = None
    #: "103/104" —— 索引只覆盖了一部分。允许检索，但要让调用方知道。
    partial: str | None = None
    #: 能力候选（与类候选并行检索，互不影响）。
    capabilities: list[CapabilityCandidate] = field(default_factory=list)
    #: 能力检索的"查没查/为什么没查"；仅当配置了能力检索时才有值。
    capability_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.attempted and self.reason is None

    @property
    def uids(self) -> set[str]:
        return {c.uid for c in self.candidates}
