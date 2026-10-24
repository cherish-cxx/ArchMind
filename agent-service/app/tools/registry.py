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
    DEFAULT_METHOD_MAX_LINES,
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
    #: 源码大纲里锁定的方法 uid（`owner#signature`）。目前只有 method-body 消费。
    method_uid: str | None = None

""" 
这里是python类型别名，不是业务执行代码，制作类型标注，运行时不会执行任何逻辑，用来描述一类异步函数长什么样
ToolHandler给这个函数起个名字，后面代码直接复用这个名字
Callable[参数列表，返回类型]:Python typing库表示可调用对象
"[JavaClient", ToolInvocation]：代表这个函数必须接收 2 个参数
参数 1：JavaClient 对象
参数 2：ToolInvocation 对象
Awaitable[...]：代表这是async 异步函数，调用它必须加await；
Awaitable[ToolEnvelope[Any]]：await之后拿到的返回值，必须是ToolEnvelope对象，里面可以装任意业务数据Any。
"""
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
    # 是否需要锚点true/false
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
        # 锁了方法就只查这一个方法的上下游 —— 否则「谁调用了它」会退化成整个类
        method_uid=inv.method_uid,
        # 预算由这边显式传，Java 不会替 Agent 拦（见 java_client 顶部的约束 ③）
        max_depth=inv.slots.depth or DEFAULT_CHAIN_DEPTH,
        max_nodes=DEFAULT_MAX_NODES,
    )


async def _method_body(client: JavaClient, inv: ToolInvocation) -> ToolEnvelope[Any]:
    # 锁定方法时只切这一个；方法级只切 1 个方法，行数预算可以放宽
    if inv.method_uid:
        return await client.method_body(
            inv.project_id, method_uid=inv.method_uid, max_lines=DEFAULT_METHOD_MAX_LINES
        )
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
#: 由循环单独短路处理。查不到就是「这个 intent 不该调工具」，不是漏配。用来匹配目前的五个方法名
INTENT_TOOL: dict[Intent, str] = {
    Intent.EXPLORE_OVERVIEW: "overview",
    Intent.EXPLORE_FLOW: "call-chain",
    Intent.EXPLORE_RELATION: "class-relations",
    Intent.EXPLORE_CODE: "method-body",
}


def owner_of(uid: str) -> str:
    """方法 uid 是 `owner#signature`；任何类级工具要的都是 `#` 左边那半。"""
    return uid.split("#", 1)[0]


def resolve_uid(ctx: AgentContext, slots: Slots) -> str | None:
    """决定这次调用以哪个类为锚点。

    `slots.entityRef` 优先 —— LLM 明确指了某个类时听它的；
    没指（比如用户说「它」）就回落到 `ctx.focus`，这正是代词消解规则表里
    「它 → focus.uid」那条的代码落点。

    两种来源都可能是方法 uid：锁了方法时 prompt 会把 `owner#signature` 交给
    LLM，「它」很容易被填成方法 uid。类级工具拿它去查必然「类不存在」，
    所以出口统一收敛到 owner。
    """
    raw = slots.entity_ref or (ctx.focus.uid if ctx.focus else None)
    return owner_of(raw) if raw else None


def plan(intent: Intent, ctx: AgentContext) -> ToolSpec | None:
    """给一个 intent，决定调哪个工具。返回 None 表示「不该调工具」。

    三种返回 None 的情况，含义不同但处置相同（都走澄清/兜底）：
      - NAVIGATE / OUT_OF_SCOPE：设计如此，根本不查图
      - 工具需要锚点但当前没有：调下去必然 `ok:false`，不如提前拦住并告诉用户「先点个类」
    """
    # 匹配方法名：overview等
    name = INTENT_TOOL.get(intent)
    if name is None:
        logger.debug("intent=%s 不产生工具调用（设计如此）", intent)
        return None
    # anchor翻译：锚点
    spec = TOOLS[name]
    # 锚点有两种：画布上选中的类，或源码大纲里锁定的方法（仅 EXPLORE_CODE 用得上）
    has_anchor = ctx.focus is not None or (intent is Intent.EXPLORE_CODE and ctx.method_uid)
    if spec.needs_anchor and not has_anchor:
        logger.info("intent=%s 需要锚点但当前无 focus —— 提前拦住，不空跑一次工具", intent)
        return None
    return spec

# 上下文中获取项目id，被定的类的uid，
def build_invocation(ctx: AgentContext, slots: Slots) -> ToolInvocation:
    return ToolInvocation(
        project_id=ctx.project_id,
        uid=resolve_uid(ctx, slots),
        slots=slots,
        method_uid=_resolve_method_uid(ctx, slots),
    )


def _resolve_method_uid(ctx: AgentContext, slots: Slots) -> str | None:
    """方法锁只在「用户没改口径」时生效。

    用户显式点名了另一个类（`entityRef` 存在且不是这个方法的 owner），
    说明锁已经过期 —— 这时按类走，绝不能用旧方法答题。
    """
    if not ctx.method_uid:
        return None
    owner = owner_of(ctx.method_uid)
    # entityRef 可能就是这个方法 uid 本身（「谁调用了它」），按 owner 比较才不会误判成换了口径
    if slots.entity_ref and owner_of(slots.entity_ref) != owner:
        logger.info("方法锁 %s 与用户点名的 %s 不一致，本轮按类走", ctx.method_uid, slots.entity_ref)
        return None
    return ctx.method_uid
