"""一轮问答的公共构件 —— 检索、记忆、下一步建议、出图命令。

这些能力与「谁决定调哪个工具」无关：六选一时代它们由 `core/loop.py` 持有，
删掉分发之后多步循环直接复用，所以搬到这里。**它们不认识 Intent，也不认识 tool_calls。**

四个边界刻意留在调用方：
  - 要不要检索（有锚点就不查）由 `retrieve` 自己判断，它需要 `ctx`；
  - 记忆触发规则在 `memory.should_recall`，这里只负责调用与降级；
  - 出图命令从信封**确定性推导**，不由模型生成；
  - 「下一步问什么」是固定的两条，不读模型输出。
"""

import logging
import time

from app.core.explain import ExplainResult
from app.core.memory import MemoryService, explicit_draft, should_recall
from app.core.retriever import Retriever
from app.models.context import AgentContext
from app.models.memory import MemoryHit
from app.models.response import NodeRef, NodeRefType, Step, StepType, Suggestion, UICmd, UiOp
from app.models.retrieval import Retrieval
from app.models.tool import ToolEnvelope

logger = logging.getLogger(__name__)


# ==================== 检索（R4）====================


async def retrieve(
    ctx: AgentContext, message: str, retriever: Retriever | None, steps: list[Step]
) -> Retrieval:
    """无锚点时检索，并把"查没查、查了几个"记进 steps。

    **有锚点就不查**：用户已经点了类，这句问的十有八九就是它 —— 检索纯属浪费，
    而且会把一个本来明确的问句引到别的类上去。

    `StepType` 复用 `TOOL` 而不是新增枚举值：新增值会让前端的 switch 收到
    不认识的类型。detail 里写 `retrieve(...)` 已经足够区分它和真正的工具。
    """
    if retriever is None or ctx.focus is not None or ctx.method_uid:
        return Retrieval()

    started = time.perf_counter()
    result = await retriever.search(ctx.project_id, message)
    steps.append(
        Step(n=len(steps) + 1, type=StepType.TOOL,
             detail=_retrieve_detail(result), ms=_ms(started))
    )
    return result


def _retrieve_detail(result: Retrieval) -> str:
    if not result.attempted:
        return f"retrieve(skipped:{result.reason})"
    if result.reason:
        return f"retrieve(failed:{result.reason})"
    extra = f", partial {result.partial}" if result.partial else ""
    return f"retrieve({len(result.candidates)}{extra})"


# ==================== 记忆 ====================


async def recall_memories(
    ctx: AgentContext,
    message: str,
    memory: MemoryService,
    steps: list[Step],
    reason: str,
) -> list[MemoryHit]:
    """按触发原因召回。失败返回空列表 —— 记忆是附加路径。"""
    started = time.perf_counter()
    try:
        hits = await memory.recall(ctx.user_id, ctx.project_id, message)
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆召回失败，本轮不注入：%s", e)
        return []
    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"recall-memory({len(hits)}:{reason})", ms=_ms(started)))
    return hits


def _window_uids(ctx: AgentContext, k: int = 8) -> set[str]:
    """本轮之前的工作窗口里出现过的实体。

    **不含本轮**：如果把本轮刚检索出来的候选也算进来，
    "指代没有着落"这条触发就永远不会成立 —— 检索一出结果，窗口就不空了。
    """
    uids: set[str] = set()
    if ctx.focus is not None:
        uids.add(ctx.focus.uid)
    if ctx.method_uid:
        uids.add(ctx.method_uid)
    prior = ctx.messages[:ctx.turn_start]
    for m in prior[-k:]:
        meta = m.meta or {}
        uids.update(meta.get("uids") or ())
        for c in meta.get("citations") or ():
            if isinstance(c, dict) and c.get("uid"):
                uids.add(c["uid"])
    return uids


def memory_trigger(ctx: AgentContext, message: str, retrieval: Retrieval) -> str | None:
    return should_recall(
        message=message,
        turns=ctx.budget.turns,
        last_outcome=ctx.last_outcome,
        has_anchor=ctx.focus is not None or ctx.method_uid is not None,
        candidate_count=len(retrieval.candidates),
        window_entities=_window_uids(ctx),
    )


async def remember_explicit(
    ctx: AgentContext,
    message: str,
    explanation: ExplainResult,
    graph_rev: int,
    memory: MemoryService,
) -> str | None:
    """显式记忆 → 落库 + 索引，返回要追加到回答末尾的确认行。

    **不满足就静默跳过**：没有触发词、没有 evidence、挑不出 subject，
    都不写。写失败也只记日志 —— 用户问的是代码，不是记忆。
    """
    draft = explicit_draft(
        message=message,
        evidence_uids=sorted(ctx.turn.evidence),
        focus_uid=ctx.focus.uid if ctx.focus else None,
        graph_rev=graph_rev,
        conversation_id=ctx.conversation_id,
        turn_no=ctx.budget.turns + 1,
        headline=explanation.headline,
    )
    if draft is None:
        return None
    try:
        result = await memory.remember(ctx.user_id, ctx.project_id, draft)
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆写入失败，本轮照常返回：%s", e)
        return None
    if result is None:
        return None
    _, action = result
    if action == "ignored":
        return None
    return f"（已记住：{draft.text}）"


# ==================== 出图命令 ====================


def ui_commands_for(env: ToolEnvelope, ctx: AgentContext) -> list[UICmd]:
    """从工具结果**确定性推导**出图命令。

    ⚠️ **与设计 §6 的差异**：设计里 uiCommands 由 LLM 生成、再由 Java 侧校验。
    P3 改成推导，理由是：4 个 op 里没有一个需要「理解」，全都直接来自工具结果
    （call-chain 天然对应 SHOW_PATH）。让 LLM 生成再事后校验，
    是**先制造幻觉再拦截**；推导则是根本不产生幻觉。

    R6 第二步起按信封逐个推导，由循环取第一个能产命令的结果。
    """
    data = env.data
    if getattr(data, "start", None) is not None:                 # call-chain
        target = NodeRef(type=NodeRefType.CLASS, uid=data.start.qualified_name)
        return [UICmd(op=UiOp.SHOW_PATH, target=target, rev=ctx.viewport.rev)]
    if getattr(data, "center", None) is not None:                # class-relations
        return [UICmd(op=UiOp.FOCUS_NODE,
                      target=NodeRef(type=NodeRefType.CLASS, uid=data.center.qualified_name),
                      rev=ctx.viewport.rev)]
    if ctx.focus is not None:                                    # method-body 等，就聚焦当前锚点
        return [UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=ctx.viewport.rev)]
    return []


# ==================== 下一步建议 ====================


def suggestions(ctx: AgentContext) -> list[Suggestion]:
    """下一步能问什么。闭环的发动机 —— 没有它，用户不知道该说啥。

    只给两条：多了是噪音，而工具就那么几个。
    `message` 就是点下去之后发回来的那句话 —— 前端不再需要把 label 翻译成意图。
    """
    if ctx.focus is None:
        return [Suggestion(label="这个项目是干嘛的", message="这个项目是干嘛的")]
    return [
        Suggestion(label="它的调用链是什么样的", message="它的调用链是什么样的"),
        Suggestion(label="谁调用了它", message="谁调用了它"),
    ]


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
