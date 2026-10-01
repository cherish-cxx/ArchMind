"""一轮问答的编排 —— intent → 取数 → 讲解 → 出图命令。

这是 P3 的主干，也是 P2 那个 `if focus: ... else: ...` 的正式替代品。
它自己不查图、不调 LLM、不拼 prompt —— 四件事分别由 registry / llm / prompt / explain 负责。
**编排层只做分支和组装**，这样每条分支都能单独测，也让「慢在哪」能从 steps 里看出来。
"""

import logging
import time

from app.core.explain import ExplainResult, explain
from app.core.guard import evidence_uids, filter_ui_commands
from app.core.intent import classify_intent
from app.core.llm import Completer, LLMError, LLMNotConfiguredError
from app.models.context import AgentContext, HistoryItem
from app.models.intent import Intent, IntentResult
from app.models.response import (
    AgentResponse,
    Citation,
    Meta,
    NodeRef,
    NodeRefType,
    Step,
    StepType,
    Suggestion,
    UICmd,
    UiOp,
)
from app.models.tool import ToolEnvelope
from app.tools import registry
from app.tools.java_client import InternalAuthError, JavaClient, ToolTransportError

logger = logging.getLogger(__name__)

#: P3 还没有 viewport（前端上报是 P4），所以 UICommand 的 rev 固定 0。
#: 留 0 而不是 None：字段是 int，且 P4 接上 viewport 后这里会换成 ctx.viewport.rev。
_REV_PLACEHOLDER = 0


async def run_turn(
    ctx: AgentContext,
    message: str,
    java: JavaClient,
    llm: Completer,
) -> AgentResponse:
    ctx.reset_turn_budget()
    started = time.perf_counter()
    steps: list[Step] = []

    # ── 1. Intent 判定 ──────────────────────────────────────────────────────
    intent_result, degraded_intent = await _classify(ctx, message, llm)
    steps.append(
        Step(n=len(steps) + 1, type=StepType.INTENT,
             detail=f"{intent_result.intent.value} (conf={intent_result.confidence:.2f})"
                    + (" [规则降级]" if degraded_intent else ""),
             ms=_ms(started))
    )

    # ── 2. 三条不查图的分支 ──────────────────────────────────────────────────
    if intent_result.needs_clarification:
        return _clarify(ctx, intent_result, steps, started)

    if intent_result.intent is Intent.NAVIGATE:
        return _navigate(ctx, steps, started)

    if intent_result.intent is Intent.OUT_OF_SCOPE:
        return _refuse(ctx, intent_result, steps, started)

    # ── 3. 选工具 ───────────────────────────────────────────────────────────
    spec = registry.plan(intent_result.intent, ctx)
    if spec is None:
        # 走到这里只剩一种情况：这个意图要查图，但用户还没点任何类（9月20 的入口约束）
        return _need_anchor(ctx, intent_result, steps, started)

    # ── 4. 取数 ────────────────────────────────────────────────────────────
    inv = registry.build_invocation(ctx, intent_result.slots)
    tool_started = time.perf_counter()
    try:
        envelope = await spec.handler(java, inv)
    except InternalAuthError:
        # 配置错误（两边 token 不一致）—— 必须炸成 500，绝不降级
        raise
    except ToolTransportError as e:
        logger.warning("工具 %s 不可用，本轮降级：%s", spec.name, e)
        steps.append(Step(n=len(steps) + 1, type=StepType.TOOL, detail=f"{spec.name} 失败",
                          ms=_ms(tool_started)))
        return _tool_down(ctx, intent_result, spec.name, steps, started)

    ctx.budget.tool_calls_this_turn += 1
    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"{spec.name}({inv.uid or '-'})", ms=_ms(tool_started)))

    if not envelope.ok:
        # ok:false 是**业务结果**（类不存在、项目未分析…），不是异常 —— 原样说给用户
        return _tool_said_no(ctx, intent_result, envelope, steps, started)

    # ── 5. 讲解 + 出图命令 ──────────────────────────────────────────────────
    envelopes = [envelope]
    evidenced = evidence_uids(envelopes)
    llm_started = time.perf_counter()
    explanation = await explain(
        intent_result.intent, envelopes, evidenced, intent_result.rewritten_query or message, llm
    )
    steps.append(Step(n=len(steps) + 1, type=StepType.LLM,
                      detail="讲解(降级)" if explanation.degraded else "讲解",
                      ms=_ms(llm_started)))

    commands = _ui_commands(intent_result.intent, envelope, evidenced, ctx)
    kept, dropped = filter_ui_commands(commands, evidenced | _ctx_uids(ctx))
    if dropped:
        logger.warning("丢弃了 %d 条指向不存在节点的 UICommand：%s",
                       len(dropped), [c.op.value for c in dropped])

    _remember(ctx, message, explanation, intent_result)
    return _response(ctx, explanation.message, explanation.citations, kept,
                     _suggestions(intent_result.intent, ctx), steps, intent_result,
                     envelope.graph_rev, started)


# ==================== Intent 判定 + 降级 ====================


async def _classify(
    ctx: AgentContext, message: str, llm: Completer
) -> tuple[IntentResult, bool]:
    """判 Intent。LLM 挂了就**回落到 P2 的规则**（有 focus 就当问流程）。

    这是 C2 降级在判定环节的对应物：讲解可以退化成模板，
    「判意图」退化成规则也比整轮失败强 —— 而且退化的那条规则正是 P2 一直在跑的行为，
    不是新写的一条没人验证过的路径。
    """
    try:
        return await classify_intent(ctx, message, llm), False
    except LLMNotConfiguredError:
        raise
    except LLMError as e:
        logger.warning("Intent 判定不可用，回落到规则：%s", e)
        fallback = (
            IntentResult(intent=Intent.EXPLORE_FLOW, confidence=1.0)
            if ctx.focus is not None
            else IntentResult(intent=Intent.EXPLORE_OVERVIEW, confidence=1.0)
        )
        return fallback, True


# ==================== 三条不查图的分支 ====================


def _clarify(
    ctx: AgentContext, intent_result: IntentResult, steps: list[Step], started: float
) -> AgentResponse:
    """置信度不够，问一句再动手。

    **不执行任何工具** —— 这是 A3 的核心：错的图比不回答伤害大得多。
    """
    options = _suggestions(intent_result.intent, ctx)
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=(
            f"我不太确定你想问什么（把握 {intent_result.confidence:.2f}）。"
            "你是想问下面哪个？"
        ),
        suggestions=options,
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=0, latency_ms=_ms(started)),
    )


def _navigate(ctx: AgentContext, steps: list[Step], started: float) -> AgentResponse:
    """纯导航：只动焦点，不查图、不调 LLM #2（E2：省掉一半成本）。"""
    if ctx.focus_stack:
        uid = ctx.focus_stack.pop()
        ctx.focus = NodeRef(type=NodeRefType.CLASS, uid=uid)
        cmd = UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=_REV_PLACEHOLDER)
        msg = f"好，回到上一个：{uid.rsplit('.', 1)[-1]}"
    else:
        ctx.focus = None
        cmd = UICmd(op=UiOp.CLEAR_VIEW, rev=_REV_PLACEHOLDER)
        msg = "已经在最开始了，帮你清空视图。"

    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=msg,
        ui_commands=[cmd],
        steps=steps,
        meta=Meta(intent=Intent.NAVIGATE.value, confidence=1.0, graph_rev=0,
                  latency_ms=_ms(started)),
    )


def _refuse(
    ctx: AgentContext, intent_result: IntentResult, steps: list[Step], started: float
) -> AgentResponse:
    """OUT_OF_SCOPE：一句话拒绝 + 给两个能点的选项。**不做道德说教。**"""
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message="这个不在我能回答的范围内 —— 我只能讲这个项目里已有的代码结构和调用关系。",
        suggestions=_suggestions(None, ctx),
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=0, latency_ms=_ms(started)),
    )


def _need_anchor(
    ctx: AgentContext, intent_result: IntentResult, steps: list[Step], started: float
) -> AgentResponse:
    """要查图但没锚点。**提前拦住**，不空跑一次注定 ok:false 的工具调用。"""
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message="这个问题得先有个起点 —— 请先在图上点一个类，或者直接说类名。",
        suggestions=_suggestions(None, ctx),
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=0, latency_ms=_ms(started)),
    )


# ==================== 工具失败的两条路 ====================


def _tool_down(
    ctx: AgentContext, intent_result: IntentResult, name: str, steps: list[Step], started: float
) -> AgentResponse:
    """Java 连不上 —— 诚实说，不 500。"""
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=f"查询服务（{name}）暂时连不上，这轮没取到数据。稍后再试。",
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=0, latency_ms=_ms(started)),
    )


def _tool_said_no(
    ctx: AgentContext, intent_result: IntentResult, env: ToolEnvelope,
    steps: list[Step], started: float,
) -> AgentResponse:
    """`ok:false` 是业务结果，原样转述它的原因，不猜、不美化。"""
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=f"这个我答不了：{env.note or '工具没有给出原因'}",
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=env.graph_rev, latency_ms=_ms(started)),
    )


# ==================== 出图命令 ====================


def _ui_commands(
    intent: Intent, env: ToolEnvelope, evidenced: set[str], ctx: AgentContext
) -> list[UICmd]:
    """从工具结果**确定性推导**出图命令。

    ⚠️ **与设计 §6 的差异**：设计里 uiCommands 由 LLM 生成、再由 Java 侧校验。
    P3 改成推导，理由是：4 个 op 里没有一个需要「理解」，全都直接来自工具结果
    （call-chain 天然对应 SHOW_PATH，设计自己也这么写）。让 LLM 生成再事后校验，
    是**先制造幻觉再拦截**；推导则是根本不产生幻觉。
    等出现了真正需要 LLM 判断的 op（如「重点看哪条边」），再把它交给 LLM 不迟 ——
    那时 `guard.filter_ui_commands` 才是必需品，现在它是安全网。
    """
    data = env.data
    if getattr(data, "start", None) is not None:                 # call-chain
        target = NodeRef(type=NodeRefType.CLASS, uid=data.start.qualified_name)
        return [UICmd(op=UiOp.SHOW_PATH, target=target, rev=_REV_PLACEHOLDER)]
    if getattr(data, "center", None) is not None:                # class-relations
        return [UICmd(op=UiOp.FOCUS_NODE,
                      target=NodeRef(type=NodeRefType.CLASS, uid=data.center.qualified_name),
                      rev=_REV_PLACEHOLDER)]
    if ctx.focus is not None:                                    # method-body 等，就聚焦当前锚点
        return [UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=_REV_PLACEHOLDER)]
    return []


def _ctx_uids(ctx: AgentContext) -> set[str]:
    """Context 里已知存在的 uid。

    用途只有一个：NAVIGATE 轮没有工具调用、因而没有 evidence，
    但「退回去」指向的那个 uid 是从**上一轮真实 tool 结果**里存下来的，它确实存在。
    把它算进白名单不是放松校验，而是给校验一个正确的权威来源。
    """
    uids = set(ctx.focus_stack)
    if ctx.focus is not None:
        uids.add(ctx.focus.uid)
    return uids


# ==================== 建议与历史 ====================


def _suggestions(intent: Intent | None, ctx: AgentContext) -> list[Suggestion]:
    """下一步能问什么。闭环的发动机 —— 没有它，用户不知道该说啥。

    只给两条：多了是噪音，而 V1 的工具就那么几个。
    """
    if ctx.focus is None:
        return [Suggestion(label="这个项目是干嘛的", intent=Intent.EXPLORE_OVERVIEW)]
    return [
        Suggestion(label="它的调用链是什么样的", intent=Intent.EXPLORE_FLOW),
        Suggestion(label="谁调用了它", intent=Intent.EXPLORE_RELATION),
    ]


def _remember(
    ctx: AgentContext, question: str, explanation: ExplainResult, intent_result: IntentResult
) -> None:
    """把这一轮压成一句摘要存起来。

    **存摘要不存原文**（设计 §7 决定⑤）—— 十轮之后塞原文，Context 就爆了。
    """
    summary = explanation.message.strip().replace("\n", " ")
    if len(summary) > 120:
        summary = summary[:120] + "…"
    ctx.history.append(
        HistoryItem(
            turn=ctx.budget.turns + 1,
            intent=intent_result.intent,
            q=question,
            a=summary,
            focus_after=ctx.focus.uid if ctx.focus else None,
        )
    )
    ctx.budget.turns += 1


# ==================== 组装 ====================


def _response(
    ctx: AgentContext, message: str, citations: list[Citation], commands: list[UICmd],
    suggestions: list[Suggestion], steps: list[Step], intent_result: IntentResult,
    graph_rev: int, started: float,
) -> AgentResponse:
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=message,
        citations=citations,
        ui_commands=commands,
        suggestions=suggestions,
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=graph_rev, latency_ms=_ms(started)),
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
