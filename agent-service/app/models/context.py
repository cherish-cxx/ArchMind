"""一轮对话的上下文 —— `AgentContext` 及其零件。

**第一原则**：Context 存**状态与对话记录**，不存知识。
实体引用一律用图的 uid 字符串，不存类对象、不存代码片段。

## v2：messages 取代 history（2026-11）

v1 的 `history` 每轮只存 120 字摘要，导致三件事做不到：
  - 工具返回、检索候选、完整模型输出全部丢失；
  - 模型看不到自己上一轮真正说了什么；
  - 未来换成 function calling 时没有消息流可接。

v2 改成 `messages`：**完整对话记录 + 工具调用 + 工具返回**。
`ChatMessage` 是我们自己的形状（`retrieval` / `event` 是内部角色），
真正发给 LLM 时由 `to_llm_messages()` 适配 —— 这样既完整记录，
又不会因为塞一个没有前置 tool_call 的 tool 消息而触发 400。

⚠️ **窗口 ≠ 完整**：`messages` 是工作窗口（有上限，会淘汰），
完整转录在 MySQL 的 `agent_turn.transcript_json`。轮结束后
`RETRIEVAL` / `TOOL` 从窗口移除（已落库，对后续轮次无价值）。
"""

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.models.base import CamelModel
from app.models.intent import Intent
from app.models.memory import MemoryHit
from app.models.response import Citation, GraphPatch, NodeRef, Section, Step, UICmd
from app.models.retrieval import Retrieval
from app.models.tool import RelationType, ToolEnvelope

#: 当前上下文结构版本。旧版本反序列化时按「新会话」重建（沿用 P4.1 的既有语义）。
CURRENT_CONTEXT_VERSION = 2


class Budget(CamelModel):
    """本轮的预算。循环靠它刹车（设计 §8-C3）。

    `steps` 与 `toolCalls` 是**两层**刹车，不是重复：一次工具调用算一个 step，
    但 LLM 可能在两次调用之间插一步「我信息够了」的判断。
    """

    turns: int = 0                    # 这个会话总共聊了几轮（由落库后的 turn_no 回填）
    steps_used: int = 0               # **本轮**已用步数 —— 每轮开头清零
    tool_calls_this_turn: int = 0     # **本轮**已打 Java 次数 —— 每轮开头清零
    llm_calls_this_turn: int = 0      # **本轮**已调模型次数 —— 每轮开头清零
    tool_calls_limit: int = 8
    llm_calls_limit: int = 6
    steps_limit: int = 12


# ==================== 消息 ====================


class MessageRole(StrEnum):
    """对话记录里的角色。

    `RETRIEVAL` / `EVENT` 是**内部角色**，不是 OpenAI 的角色：
      - `RETRIEVAL` 渲染成一段 system 文本块（检索候选不是工具调用）
      - `EVENT`     渲染成一句系统注记（澄清 / 导航 / 越界 / 降级提示）
    这样记录完整，而发给模型时不会违反 tool 消息协议。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    EVENT = "event"


class ToolCall(CamelModel):
    """一次工具调用（模型侧的意图，不含结果）。"""

    id: str
    name: str
    arguments: str = "{}"


class ChatMessage(CamelModel):
    """对话记录里的一条。

    `meta` 放结构化附注（intent / confidence / citations / uids），
    渲染成文本时不参与，但落库时随 transcript 一起保存。
    """

    role: MessageRole
    content: str | None = None
    name: str | None = None            # tool 消息对应的工具名
    tool_call_id: str | None = None    # tool 消息回指的调用 id
    tool_calls: list[ToolCall] | None = None
    meta: dict[str, Any] | None = None


# ==================== 本轮产物（不持久化进快照）====================


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的结果。`name` 必须和调用它的工具名一致 ——
    落库时 `tool_calls_json` 要用它区分是哪个工具。"""

    name: str
    envelope: ToolEnvelope


@dataclass
class TurnArtifacts:
    """本轮的结构化产物。

    **不进 Redis 快照**（`AgentContext.turn` 标了 `exclude=True`）：
    它们只在本轮内存里存在，由 `ConversationStore.commit_turn` 读出来写进
    `agent_turn` 的各个列。冷加载后为空 —— 这是有意的，避免快照被工具结果撑大。
    """

    intent: Intent | None = None
    confidence: float | None = None
    degraded: bool = False
    outcome: str = "OK"

    rewritten_query: str | None = None
    question: str = ""
    focus_before: str | None = None
    focus_after: str | None = None
    method_uid: str | None = None

    retrieval: Retrieval | None = None
    #: 本轮召回的记忆。只用于 prompt 注入和日志，**不进快照**：
    #: 下轮要用记忆会重新召回，存下来只会让快照随会话膨胀。
    memory_hits: list[MemoryHit] = dc_field(default_factory=list)
    tool_results: list[ToolResult] = dc_field(default_factory=list)
    evidence: list[str] = dc_field(default_factory=list)

    headline: str | None = None
    answer_message: str = ""
    sections: list[Section] = dc_field(default_factory=list)
    citations: list[Citation] = dc_field(default_factory=list)
    ui_commands: list[UICmd] = dc_field(default_factory=list)
    graph: GraphPatch | None = None
    steps: list[Step] = dc_field(default_factory=list)

    llm_calls: list[dict[str, Any]] = dc_field(default_factory=list)
    latency_ms: int = 0

    @property
    def envelopes(self) -> list[ToolEnvelope]:
        return [r.envelope for r in self.tool_results]


# ==================== 屏幕快照 ====================

#: `viewport.visibleNodes` / `visibleRelations` 的硬上限。
VIEWPORT_MAX_ITEMS = 60


class RelationRef(CamelModel):
    """屏幕上一条可见的关系。三要素：从谁、到谁、什么关系。"""

    from_: str = Field(alias="from")
    to: str
    rel: RelationType


class Viewport(CamelModel):
    """前端屏幕状态的**快照**。真相在前端，这里只是它的镜像。"""

    rev: int = 0
    visible_nodes: list[str] = []
    visible_relations: list[RelationRef] = []
    focused_node: NodeRef | None = None
    truncated: bool = False
    mode: str = "cumulative"
    center_uid: str | None = None
    hops: int = 0
    folded_count: int = 0

    @classmethod
    def from_report(
        cls,
        *,
        rev: int,
        visible_nodes: list[str],
        visible_relations: list[RelationRef],
        focused_node: NodeRef | None,
        mode: str = "cumulative",
        center_uid: str | None = None,
        hops: int = 0,
        folded_count: int = 0,
    ) -> "Viewport":
        nodes = visible_nodes[:VIEWPORT_MAX_ITEMS]
        rels = visible_relations[:VIEWPORT_MAX_ITEMS]
        return cls(
            rev=rev,
            visible_nodes=nodes,
            visible_relations=rels,
            focused_node=focused_node,
            mode=mode,
            center_uid=center_uid,
            hops=hops,
            folded_count=folded_count,
            truncated=(
                len(visible_nodes) > VIEWPORT_MAX_ITEMS
                or len(visible_relations) > VIEWPORT_MAX_ITEMS
            ),
        )


# ==================== 会话状态 ====================


class AgentContext(CamelModel):
    """一个会话的全部状态。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    version: int = CURRENT_CONTEXT_VERSION
    conversation_id: str
    project_id: int
    #: 会话归属用户。生产由 Java 注入并在路由里写死；默认 0 只用于测试构造。
    #: Redis 的 key 也带它 —— 否则同一项目下的两个用户会共用一份上下文。
    user_id: int = 0

    #: 完整对话记录（工作窗口，有上限）
    messages: list[ChatMessage] = []
    #: 本轮在 messages 中的起点；`commit_turn` 用它切出本轮 delta
    turn_start: int = 0
    #: 本轮产物；**不进快照**，由落库层读取
    turn: TurnArtifacts = Field(default_factory=TurnArtifacts, exclude=True)

    focus: NodeRef | None = None
    focus_stack: list[str] = []       # 「退回去」= pop()
    method_uid: str | None = None

    budget: Budget = Budget()
    viewport: Viewport = Viewport()
    #: 上一轮走到哪个分支（OK / CLARIFY / NAVIGATE / …）。
    #: 记忆触发用：上一轮澄清了，这轮先查一次记忆再问。
    last_outcome: str | None = None

    @model_validator(mode="after")
    def _reject_old_version(self) -> "AgentContext":
        if self.version < CURRENT_CONTEXT_VERSION:
            raise ValueError(
                f"context version {self.version} < {CURRENT_CONTEXT_VERSION}，按新会话重建"
            )
        return self

    # ── 一轮的生命周期 ──────────────────────────────────────────────

    def begin_turn(self, question: str) -> None:
        """每轮开头调一次：记下起点、清空本轮产物、追加用户消息。"""
        self.turn_start = len(self.messages)
        self.turn = TurnArtifacts(
            question=question,
            focus_before=self.focus.uid if self.focus else None,
            method_uid=self.method_uid,
            evidence=[],
        )
        self.budget.steps_used = 0
        self.budget.tool_calls_this_turn = 0
        self.budget.llm_calls_this_turn = 0
        if question:
            self.messages.append(ChatMessage(role=MessageRole.USER, content=question))

    def finish_turn(self, outcome: str, latency_ms: int) -> None:
        self.turn.outcome = outcome
        self.turn.latency_ms = latency_ms
        self.turn.focus_after = self.focus.uid if self.focus else None
        # 下一轮的事件触发要看它（`after_clarify`）
        self.last_outcome = outcome

    def append_message(self, message: ChatMessage) -> None:
        self.messages.append(message)

    def append_event(self, text: str, **meta: Any) -> None:
        self.messages.append(
            ChatMessage(role=MessageRole.EVENT, content=text, meta=meta or None)
        )

    def append_retrieval(self, text: str, **meta: Any) -> None:
        self.messages.append(
            ChatMessage(role=MessageRole.RETRIEVAL, content=text, meta=meta or None)
        )

    def append_tool_exchange(
        self, *, call: ToolCall, tool_name: str, result_text: str
    ) -> None:
        """追加一对 `assistant(tool_calls)` + `tool(result)`。"""
        self.messages.append(
            ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call])
        )
        self.messages.append(
            ChatMessage(
                role=MessageRole.TOOL,
                content=result_text,
                name=tool_name,
                tool_call_id=call.id,
            )
        )

    def append_answer(self, text: str, **meta: Any) -> None:
        self.messages.append(
            ChatMessage(role=MessageRole.ASSISTANT, content=text, meta=meta or None)
        )

    # ── 渲染 ────────────────────────────────────────────────────────

    def render_messages(self, messages: list[ChatMessage] | None = None) -> str:
        """把消息渲染成给 LLM 读的文本块（不是 OpenAI 消息数组）。"""
        items = self.messages if messages is None else messages
        lines: list[str] = []
        for m in items:
            if m.role is MessageRole.USER:
                lines.append(f"[用户] {m.content or ''}")
            elif m.role is MessageRole.ASSISTANT:
                if m.tool_calls:
                    calls = "、".join(f"{c.name}({c.arguments})" for c in m.tool_calls)
                    lines.append(f"[助手调用工具] {calls}")
                if m.content:
                    lines.append(f"[助手] {m.content}")
            elif m.role is MessageRole.TOOL:
                lines.append(f"[工具 {m.name or ''} 返回] {m.content or ''}")
            elif m.role is MessageRole.RETRIEVAL:
                lines.append(f"[检索结果] {m.content or ''}")
            elif m.role is MessageRole.EVENT:
                lines.append(f"[系统] {m.content or ''}")
            elif m.role is MessageRole.SYSTEM:
                lines.append(f"[规则] {m.content or ''}")
        return "\n".join(lines)

    def to_llm_messages(self) -> list[dict[str, Any]]:
        """渲染成 OpenAI 兼容的 messages（R6 的 function calling 直接用它）。"""
        out: list[dict[str, Any]] = []
        for m in self.messages:
            if m.role is MessageRole.USER:
                out.append({"role": "user", "content": m.content or ""})
            elif m.role is MessageRole.SYSTEM:
                out.append({"role": "system", "content": m.content or ""})
            elif m.role is MessageRole.ASSISTANT:
                item: dict[str, Any] = {"role": "assistant", "content": m.content}
                if m.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in m.tool_calls
                    ]
                out.append(item)
            elif m.role is MessageRole.TOOL:
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content or "",
                })
            elif m.role is MessageRole.RETRIEVAL:
                out.append({"role": "system", "content": f"检索结果：\n{m.content or ''}"})
            elif m.role is MessageRole.EVENT:
                out.append({"role": "system", "content": f"系统提示：{m.content or ''}"})
        return out

    # ── 窗口收缩 ────────────────────────────────────────────────────

    def prune_window(self, max_messages: int, max_chars: int) -> None:
        """轮结束、落库之后调用：把工作窗口收缩到上限内。

        两步：
          1. 移除 `RETRIEVAL` / `TOOL` —— 已完整落库，对后续轮次无价值；
          2. 按条数与字符数从头部淘汰，但**永远保留第一条 USER**（会话原点）。
        """
        self.messages = [
            m for m in self.messages
            if m.role not in (MessageRole.RETRIEVAL, MessageRole.TOOL)
        ]
        # ★ 工具消息一走，assistant 上的 tool_calls 就没有配对项了。
        # 多步循环把 `messages` 原样发给模型，这种半截交换会被接口直接 400。
        # 降级成普通 assistant 消息 —— 窗口本来就允许丢细节，完整转录在 agent_turn。
        for m in self.messages:
            if m.role is MessageRole.ASSISTANT and m.tool_calls:
                m.tool_calls = None
                m.content = m.content or "（上一轮查过图，工具结果已归档）"
        if len(self.messages) <= max_messages and self._chars() <= max_chars:
            return

        origin = next(
            (m for m in self.messages if m.role is MessageRole.USER), None
        )
        kept: list[ChatMessage] = []
        total = 0
        for m in reversed(self.messages):
            size = len(m.content or "")
            if len(kept) >= max_messages - (1 if origin else 0):
                break
            if total + size > max_chars and kept:
                break
            kept.append(m)
            total += size
        kept.reverse()
        if origin is not None and origin not in kept:
            kept.insert(0, origin)
        self.messages = kept

    def _chars(self) -> int:
        return sum(len(m.content or "") for m in self.messages)
