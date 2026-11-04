"""零 LLM 导航 —— 「退回去」只移动会话锚点，不查图、不调模型。

**唯一调用方是 `POST /internal/agent/navigate`**（前端按钮直接声明）。
用户打字说「退回去」走的是多步循环里的 `navigate` 工具，两条路殊途同归：
都改 `ctx.focus` / `ctx.focus_stack`，并把一条 UICmd 交给前端。

副作用是 **pop `focus_stack` 并改 `ctx.focus`** —— 调用方**必须 `store.save(ctx)`**。
"""

import time

from app.models.context import AgentContext
from app.models.response import AgentResponse, Meta, NodeRef, NodeRefType, Step, UICmd, UiOp


def navigate(ctx: AgentContext, steps: list[Step], started: float) -> AgentResponse:
    """纯导航：只动焦点，不查图、不调 LLM（E2：省掉一半成本）。

    `meta.intent` 仍写 `NAVIGATE`：它不再是六选一里的意图，只是**这条路自己声明的状态**，
    给前端和日志一个可读的分支标签。
    """
    if ctx.focus_stack:
        uid = ctx.focus_stack.pop()
        ctx.focus = NodeRef(type=NodeRefType.CLASS, uid=uid)
        cmd = UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=ctx.viewport.rev)
        msg = f"好，回到上一个：{uid.rsplit('.', 1)[-1]}"
    else:
        ctx.focus = None
        cmd = UICmd(op=UiOp.CLEAR_VIEW, rev=ctx.viewport.rev)
        msg = "已经在最开始了，帮你清空视图。"

    ctx.append_event(msg, outcome="NAVIGATE")
    ctx.turn.steps = list(steps)
    ctx.turn.ui_commands = [cmd]
    ctx.finish_turn("NAVIGATE", _ms(started))
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=msg,
        ui_commands=[cmd],
        steps=steps,
        meta=Meta(intent="NAVIGATE", confidence=1.0, graph_rev=0,
                  latency_ms=_ms(started)),
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
