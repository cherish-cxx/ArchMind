"""工具的统一执行入口（R6 第一阶段）。

一次工具调用只走这里：

    normalize_args -> 起点检查 -> ToolSpec.execute -> ToolEnvelope

它不认识 LLM —— 只认识工具名和已声明的参数。派发已经归模型，
执行层只负责「这次调用协议上是否成立」。

失败策略：

- 参数/起点错误抛 `ToolError`（结构化，可被循环回喂）；
- `ToolTransportError` 原样向上抛 —— 循环把它变成一条 tool 消息；
- `InternalAuthError` 原样向上抛 —— 配置错误必须炸。
"""

from app.models.tool import ToolEnvelope
from app.tools.args import normalize_args
from app.tools.errors import ToolError
from app.tools.java_client import JavaClient
from app.tools.registry import TOOLS, ToolContext, ToolSpec, owner_of


async def execute_tool(
    name: str,
    java: JavaClient,
    ctx: ToolContext,
    raw_args: dict[str, object] | None = None,
) -> ToolEnvelope:
    """执行一次工具调用。参数在这里校验，锚点在这里检查。"""
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError.unknown_tool(name, sorted(TOOLS))

    normalized = normalize_args(name, raw_args)
    _check_anchor(spec, ctx, normalized.args)

    return await spec.execute(java, ctx, normalized.args)


def _check_anchor(spec: ToolSpec, ctx: ToolContext, args: dict[str, object]) -> None:
    """起点不满足时抛出 —— 错误信息要能让模型自己改对。

    参数里的 uid 必须在本轮已见过的实体里，否则告诉它有哪些可选。
    """
    if not spec.needs_anchor:
        return

    known = ctx.known_uids or frozenset()
    uid = args.get("uid")
    if isinstance(uid, str) and uid in known:
        return
    method_uid = args.get("methodUid")
    if isinstance(method_uid, str) and (
        method_uid in known or owner_of(method_uid) in known
    ):
        return

    given = uid if isinstance(uid, str) else method_uid
    sample = sorted(known)[:5]
    hint = (
        f"{given} 不在本轮已见过的实体里；先用 classes 或 call-chain 找到它，"
        f"或从这些已知起点里选：{sample}"
        if sample
        else f"{spec.name} 需要一个起点，但本轮还没查到任何实体；先调 classes 或 overview"
    )
    raise ToolError.invalid_arguments(field="uid", hint=hint)
