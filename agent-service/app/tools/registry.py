"""工具注册表 —— 把「意图 → 调哪个工具、怎么调」变成**数据**而不是 if/else。

P2 里这一步是硬编码的：`if focus: call_chain else overview`。
P3 换成查表之后，`core/loop.py` 的循环体**不需要知道世界上有哪 5 个工具** ——
加第 6 个工具是往下面那张表里加一行，不是改循环。

⚠️ **P3 的边界（重要）**：这里做的是**确定性分发** —— 一个 intent 固定对应一个工具。
  设计 §6 的 ReAct 循环里还有一步 `plan_tool`（让 LLM 决定下一步调什么、要不要继续调），
  那是**多步探索**，本片没做。
  理由：先把「判定 → 取数 → 讲解 → 出图」这条主干跑通并测透，
  再换掉分发那一环 —— 那时预算计数与去重才真正有意义。
  接缝已经留好了：循环里那一句 `spec.handler(client, inv)` 换成
  「先问 LLM 要一个计划，再执行」即可，其余不动。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.models.context import AgentContext
from app.models.intent import Intent, Slots
from app.models.tool import (
    ChainDirection,
    RelationDirection,
    ToolEnvelope,
)
from app.tools.java_client import (
    DEFAULT_LIMIT,
    DEFAULT_CHAIN_DEPTH,
    DEFAULT_MAX_NODES,
    JavaClient,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用的全部输入。

    `uid` **已经消解过**（`slots.entityRef` 优先，回落到 `ctx.focus`）——
    消解只做一次，放在这里，工具本身不必各自再判一遍「该用 slots 还是 focus」。
    """

    project_id: int
    uid: str | None
    slots: Slots


ToolHandler = Callable[["JavaClient", ToolInvocation], Awaitable[ToolEnvelope[Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的注册项。

    `needs_anchor` 不是装饰性字段 —— 它决定「没有锚点时这个工具能不能调」。
    V1 的入口约束是「先点类再提问」（9月20），所以这个判断必须显式存在于表里，
    而不是等调下去拿到 `ok:false` 才知道。
    """

    name: str
    handler: ToolHandler
    needs_anchor: bool
    summary: str


# ==================== 五个 handler ====================


async def _overview(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    return await client.overview(inv.project_id)


async def _classes(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    return await client.classes(inv.project_id, limit=DEFAULT_LIMIT)


async def _class_relations(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    assert inv.uid is not None  # needs_anchor=True 保证的
    # ⚠️ 枚举是**另一个**：class-relations 有 BOTH（call-chain 没有）。
    # 没指明方向时给 BOTH —— 「点类看关联」的默认语义就是上下都看。
    direction = {
        ChainDirection.UP: RelationDirection.UP,
        ChainDirection.DOWN: RelationDirection.DOWN,
        None: RelationDirection.BOTH,
    }[inv.slots.direction]
    return await client.class_relations(inv.project_id, inv.uid, direction=direction, limit=DEFAULT_LIMIT)


async def _call_chain(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    assert inv.uid is not None
    return await client.call_chain(
        inv.project_id,
        inv.uid,
        direction=inv.slots.direction or ChainDirection.DOWN,
        # 预算由这边显式传，Java 不会替 Agent 拦（见 java_client 顶部的约束 ③）
        max_depth=inv.slots.depth or DEFAULT_CHAIN_DEPTH,
        max_nodes=DEFAULT_MAX_NODES,
    )


async def _method_body(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    assert inv.uid is not None
    return await client.method_body(inv.project_id, uid=inv.uid)


# ==================== 两张表 ====================


TOOLS: dict[str, ToolSpec] = {
    "overview": ToolSpec("overview", _overview, needs_anchor=False, summary="项目概况"),
    "classes": ToolSpec("classes", _classes, needs_anchor=False, summary="类清单（兜底）"),
    "class-relations": ToolSpec("class-relations", _class_relations, needs_anchor=True, summary="类的上下游关系"),
    "call-chain": ToolSpec("call-chain", _call_chain, needs_anchor=True, summary="调用链"),
    "method-body": ToolSpec("method-body", _method_body, needs_anchor=True, summary="方法源码切片"),
}

#: intent → 工具名。**NAVIGATE / OUT_OF_SCOPE 不在表里** —— 它们不查图，
#: 由循环单独短路处理。查不到就是「这个 intent 不该调工具」，不是漏配。
INTENT_TOOL: dict[Intent, str] = {
    Intent.EXPLORE_OVERVIEW: "overview",
    Intent.EXPLORE_FLOW: "call-chain",
    Intent.EXPLORE_RELATION: "class-relations",
    Intent.EXPLORE_CODE: "method-body",
}


def resolve_uid(ctx: AgentContext, slots: Slots) -> str | None:
    """决定这次调用以哪个类为锚点。

    `slots.entityRef` 优先 —— LLM 明确指了某个类时听它的；
    没指（比如用户说「它」）就回落到 `ctx.focus`，这正是代词消解规则表里
    「它 → focus.uid」那条的代码落点。
    """
    if slots.entity_ref:
        return slots.entity_ref
    return ctx.focus.uid if ctx.focus else None


def plan(intent: Intent, ctx: AgentContext) -> ToolSpec | None:
    """给一个 intent，决定调哪个工具。返回 None 表示「不该调工具」。

    三种返回 None 的情况，含义不同但处置相同（都走澄清/兜底）：
      - NAVIGATE / OUT_OF_SCOPE：设计如此，根本不查图
      - 工具需要锚点但当前没有：调下去必然 `ok:false`，不如提前拦住并告诉用户「先点个类」
    """
    name = INTENT_TOOL.get(intent)
    if name is None:
        logger.debug("intent=%s 不产生工具调用（设计如此）", intent)
        return None

    spec = TOOLS[name]
    if spec.needs_anchor and ctx.focus is None:
        logger.info("intent=%s 需要锚点但当前无 focus —— 提前拦住，不空跑一次工具", intent)
        return None
    return spec


def build_invocation(ctx: AgentContext, slots: Slots) -> ToolInvocation:
    return ToolInvocation(project_id=ctx.project_id, uid=resolve_uid(ctx, slots), slots=slots)
