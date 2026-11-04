"""工具注册表 —— 工具契约本身（名字、用途、参数 schema、执行体）。

R6 第三步删掉了「意图 → 工具」查表：派发已经归模型，这里只剩**工具是什么**。
循环与执行入口都从 `TOOLS` 取清单，加一个工具仍然是往表里加一项。

⚠️ `needs_anchor` 不是装饰性字段 —— 它决定「参数里的起点必须已知」这条校验
要不要做。判断由 `tools/executor.py` 执行，注册表只声明。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.models.tool import ChainDirection, RelationDirection, ToolEnvelope
from app.tools.schemas import TOOL_SCHEMAS
from app.tools.java_client import JavaClient


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的执行上下文。

    `known_uids` 是**本轮已经见过**的实体（用户选中的类、锁定的方法、
    检索候选、前面工具返回的类）。执行层检查的参数语义是
    「你给的 uid 在不在本轮见过的东西里」—— 这样「先查再查」才成立。

    后续加 `retriever` 等依赖时，只扩展这里，不改五个执行体的签名。
    """

    project_id: int
    known_uids: frozenset[str] | None = None


ToolExecutor = Callable[
    ["JavaClient", ToolContext, dict[str, Any]], Awaitable[ToolEnvelope[Any]]
]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的注册项。

    `needs_anchor` 不是装饰性字段 —— 它决定「没有锚点时这个工具能不能调」。
    V1 的入口约束是「先点类再提问」（9月20），所以这个判断必须显式存在于表里，
    而不是等调下去拿到 `ok:false` 才知道。
    """

    name: str
    #: 唯一执行体：接收已校验的 camelCase 参数，返回信封。
    execute: ToolExecutor
    # 是否需要锚点true/false
    needs_anchor: bool
    #: 给模型看的用途说明：什么时候用、什么时候不用、和相邻工具怎么区分。
    description: str
    #: 参数的 JSON Schema，同时是 `args.normalize_args` 的唯一依据。
    input_schema: dict[str, Any]


# ==================== 五个执行体 ====================


async def _overview(
    client: JavaClient, ctx: ToolContext, args: dict[str, Any]
) -> ToolEnvelope[Any]:
    return await client.overview(ctx.project_id)


async def _classes(
    client: JavaClient, ctx: ToolContext, args: dict[str, Any]
) -> ToolEnvelope[Any]:
    return await client.classes(ctx.project_id, limit=args["limit"])


async def _class_relations(
    client: JavaClient, ctx: ToolContext, args: dict[str, Any]
) -> ToolEnvelope[Any]:
    # schema 已把 direction 归一成 canonical 字符串；这里只做枚举转换。
    return await client.class_relations(
        ctx.project_id,
        args["uid"],
        direction=RelationDirection(args["direction"]),
        limit=args["limit"],
    )


async def _call_chain(
    client: JavaClient, ctx: ToolContext, args: dict[str, Any]
) -> ToolEnvelope[Any]:
    return await client.call_chain(
        ctx.project_id,
        args["uid"],
        direction=ChainDirection(args["direction"]),
        # 锁了方法就只查这一个方法的上下游 —— 否则「谁调用了它」会退化成整个类
        method_uid=args.get("methodUid"),
        # 预算由这边显式传，Java 不会替 Agent 拦（见 java_client 顶部的约束 ③）
        max_depth=args["maxDepth"],
        max_nodes=args["maxNodes"],
    )


async def _method_body(
    client: JavaClient, ctx: ToolContext, args: dict[str, Any]
) -> ToolEnvelope[Any]:
    # 方法锁定时只切这一个；行数预算由调用方（adapter 或模型）显式给出。
    if args.get("methodUid"):
        return await client.method_body(
            ctx.project_id,
            method_uid=args["methodUid"],
            max_methods=args["maxMethods"],
            max_lines=args["maxLines"],
        )
    return await client.method_body(
        ctx.project_id,
        uid=args["uid"],
        max_methods=args["maxMethods"],
        max_lines=args["maxLines"],
    )


# ==================== 两张表 ====================


TOOLS: dict[str, ToolSpec] = {
    "overview": ToolSpec(
        "overview", _overview, needs_anchor=False,
        description=(
            "查看项目的整体概况：项目类型、摘要、技术栈、架构分层和模块划分。"
            "不需要锚点。用户问“这个项目是做什么的”时使用；"
            "问具体流程或某个类时不要用。"
        ),
        input_schema=TOOL_SCHEMAS["overview"],
    ),
    "classes": ToolSpec(
        "classes", _classes, needs_anchor=False,
        description=(
            "列出项目中的类清单，用于在不知道类名时从候选里挑选。不需要锚点。"
            "只在没有明确目标类时使用；已经知道起点、要看调用关系时改用 call-chain，"
            "要看实现时改用 method-body。"
        ),
        input_schema=TOOL_SCHEMAS["classes"],
    ),
    "class-relations": ToolSpec(
        "class-relations", _class_relations, needs_anchor=True,
        description=(
            "查看某个类的上下游关系：谁调用/依赖/继承它，以及它调用/依赖/继承谁。"
            "需要类 uid 作为锚点。direction 可选 UP/DOWN/BOTH，默认 BOTH；"
            "limit 是每个关系列表各自的上限。想沿业务流程逐层往下看时改用 call-chain。"
        ),
        input_schema=TOOL_SCHEMAS["class-relations"],
    ),
    "call-chain": ToolSpec(
        "call-chain", _call_chain, needs_anchor=True,
        description=(
            "从一个类或方法出发追踪调用链，返回节点、边和断点（gap），"
            "是回答“流程怎么走”的主工具。需要锚点。"
            "direction 只有 UP/DOWN（没有 BOTH），默认 DOWN；"
            "maxDepth 控制往下几层，maxNodes 控制返回节点数。"
            "只想看某段代码实现时改用 method-body。"
        ),
        input_schema=TOOL_SCHEMAS["call-chain"],
    ),
    "method-body": ToolSpec(
        "method-body", _method_body, needs_anchor=True,
        description=(
            "读取方法的源码切片，是讲解实现细节的唯一来源。"
            "需要类 uid 或方法 uid 作为锚点，二者至少给一个；"
            "给接口方法 uid 时会自动落到实现类。"
            "只想知道谁调用谁时不要用，改用 call-chain。"
        ),
        input_schema=TOOL_SCHEMAS["method-body"],
    ),
}

def owner_of(uid: str) -> str:
    """方法 uid 是 `owner#signature`；任何类级工具要的都是 `#` 左边那半。"""
    return uid.split("#", 1)[0]
