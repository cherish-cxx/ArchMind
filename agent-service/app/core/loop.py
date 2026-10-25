"""一轮问答的编排 —— intent → 取数 → 讲解 → 出图命令。

这是 P3 的主干，也是 P2 那个 `if focus: ... else: ...` 的正式替代品。
它自己不查图、不调 LLM、不拼 prompt —— 四件事分别由 registry / llm / prompt / explain 负责。
**编排层只做分支和组装**，这样每条分支都能单独测，也让「慢在哪」能从 steps 里看出来。
"""

import logging
import time

from app.core.explain import ExplainResult, explain
from app.core.guard import evidence_uids, filter_ui_commands
from app.core.graph import build_graph_patch
from app.core.intent import classify_intent
from app.core.llm import Completer, LLMError, LLMNotConfiguredError
from app.models.context import AgentContext, HistoryItem
from app.models.intent import Intent, IntentResult
from app.models.response import (
    AgentResponse,
    Citation,
    GraphPatch,
    Meta,
    NodeRef,
    NodeRefType,
    Section,
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

# UICommand 的 `rev` 一律取 `ctx.viewport.rev` —— 标出「这条命令基于哪个版本的屏幕」。
# 前端执行前对比自己的 rev，**旧的就丢**，防止后端基于旧屏幕的命令把新屏幕覆盖回去。
# 这和 `/viewport` 上报那边的 rev 锁是**同一个机制的两端**。
# （P3 这里写死 0，因为那时还没有 viewport —— 注释当时就写了「P4 接上 viewport 后
#   这里会换成 ctx.viewport.rev」。P4.2 兑现了。）


async def run_turn(
    ctx: AgentContext,  #会话上下文对象
    message: str, #用户本轮发来的提问文本
    java: JavaClient, #依赖注入拿到的java服务客户端
    llm: Completer, #大模型客户端用来调用LLM生成内容
) -> AgentResponse:
    ctx.reset_turn_budget() #重置本轮对话的配额
    started = time.perf_counter() #记录当前时间戳，用来统计本轮回答一共花了多少耗时
    steps: list[Step] = [] #新建一个空列表，用来存放这一轮Agent执行的每一步记录

    # ── 1. Intent 判定 ──────────────────────────────────────────────────────
    # 调用异步函数_classify 交给大模型判断用户这句话的意图
    # intent_result（意图结果对象，包含识别出的意图类型+置信度conf）
    # degraded_intent（布尔标记，True = LLM不可用，退化成老的规则模板来判断意图
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
        return navigate(ctx, steps, started)

    if intent_result.intent is Intent.OUT_OF_SCOPE:
        return _refuse(ctx, intent_result, steps, started)

    # ── 3. 选工具 ───────────────────────────────────────────────────────────
    spec = registry.plan(intent_result.intent, ctx)
    if spec is None:
        # 走到这里只剩一种情况：这个意图要查图，但用户还没点任何类（9月20 的入口约束）
        return _need_anchor(ctx, intent_result, steps, started)

    # ── 4. 取数 ────────────────────────────────────────────────────────────
    # **组装一次工具调用的完整入参对象 `ToolInvocation`**，把会话上下文、LLM 提取的槽位，加工成工具 handler 统一接收的入参结构体。
    inv = registry.build_invocation(ctx, intent_result.slots)
    # 记录准备调用工具的这一刻时间点，后面用来算这个工具花了多少毫秒
    tool_started = time.perf_counter()
    # 真正调用选中的工具，把准备好的客户端的参数传进去等待工具返回结果，结果存到envelope
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
    # 审计显示实际用的那个锚点：锁了方法时打印方法 uid，否则只看到类会误判「没生效」
    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"{spec.name}({inv.method_uid or inv.uid or '-'})",
                      ms=_ms(tool_started)))

    if not envelope.ok:
        # ok:false 是**业务结果**（类不存在、项目未分析…），不是异常 —— 原样说给用户
        return _tool_said_no(ctx, intent_result, envelope, steps, started)

    # ── 4.5 锁定方法的源码：本轮讲解的直接素材 ─────────────────────────────
    # 用户在大纲里锁了方法，就等于说「现在问的是这段代码」。只把它当成一个 uid 传给代
    # 词消解是不够的：讲解时手上只有类名，答「谁调用了它」给不出方法里的依据。
    # **只进本轮 prompt，不写进 Context** —— 设计 §7：Context 存状态与引用，不存代码。
    envelopes = [envelope]
    if ctx.method_uid and spec.name != "method-body":
        body = await _locked_method_body(ctx, java, steps)
        if body is not None:
            envelopes.append(body)

    # ── 5. 讲解 + 出图命令 ──────────────────────────────────────────────────
    evidenced = evidence_uids(envelopes)
    llm_started = time.perf_counter()
    explanation = await explain(
        ctx,
        intent_result.intent,
        envelopes,
        evidenced,
        # 原话是**问题本身**，改写句只作为消歧提示 —— 两者都给 LLM #2，
        # 才既不失真、又能消解「它」。
        message,
        llm,
        rewritten=intent_result.rewritten_query,
    )
    steps.append(Step(n=len(steps) + 1, type=StepType.LLM,
                      detail="讲解(降级)" if explanation.degraded else "讲解",
                      ms=_ms(llm_started)))

    commands = _ui_commands(intent_result.intent, envelope, evidenced, ctx)
    kept, dropped = filter_ui_commands(commands, evidenced | _ctx_uids(ctx))
    if dropped:
        logger.warning("丢弃了 %d 条指向不存在节点的 UICommand：%s",
                       len(dropped), [c.op.value for c in dropped])

    # P5：把本轮 tool 的真实子图一起带回前端。命令说「看哪里」，它说「那里有什么」。
    graph = build_graph_patch(envelope)

    _remember(ctx, message, explanation, intent_result)
    return _response(ctx, explanation.message, explanation.headline,
                     explanation.citations, explanation.sections,
                     kept, graph, _suggestions(intent_result.intent, ctx), steps, intent_result,
                     envelope.graph_rev, started)


# ==================== 锁定方法的源码 ====================


async def _locked_method_body(
    ctx: AgentContext, java: JavaClient, steps: list[Step]
) -> ToolEnvelope | None:
    """取锁定方法的源码。

    **取不到就不加**：这是「锦上添花」的一步，调用链/关系已经取到了，
    为了附带的代码让整轮失败，等于用次要信息换掉主要信息。
    """
    assert ctx.method_uid is not None
    started = time.perf_counter()
    try:
        env = await java.method_body(
            ctx.project_id,
            method_uid=ctx.method_uid,
            max_lines=registry.DEFAULT_METHOD_MAX_LINES,
        )
    except InternalAuthError:
        raise
    except ToolTransportError as e:
        logger.warning("锁定方法源码取不到，本轮按图作答：%s", e)
        return None

    if not env.ok:
        logger.info("锁定方法 %s 取不到源码：%s", ctx.method_uid, env.note)
        return None

    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"method-body({ctx.method_uid})", ms=_ms(started)))
    return env


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
    except LLMNotConfiguredError: #配置缺失，不走降级直接异常
        raise
    except LLMError as e: #凡LLM运行时临时报错，超时，服务挂了，进入降级逻辑
        """
         如果`ctx.focus`有值 → 意图定为 **EXPLORE_FLOW（查询流程）**
         如果`ctx.focus`为空 → 意图定为 **EXPLORE_OVERVIEW（概览查询）**
         置信度直接写死 1.0（规则判定，百分百确定）
         返回的第二个值`True`，代表**本次是降级模式**，回到上层就会在 step 日志打上`[规则降级]`标记-
        """
        logger.warning("Intent 判定不可用，回落到规则：%s", e)
        fallback = (
            # python的三元表达式写法：X if 条件 else Y
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


def navigate(ctx: AgentContext, steps: list[Step], started: float) -> AgentResponse:
    """纯导航：只动焦点，不查图、不调 LLM #2（E2：省掉一半成本）。

    **有两个调用方**，这是 P4.3 把它从 `_navigate` 提为公开的原因：

      - `run_turn`：用户**打字**说「退回去」，要靠 LLM #1 判出 NAVIGATE 才走到这里
      - `api/agent.py` 的 `POST /navigate`（P4.3）：前端**点按钮**直接声明意图，
        跳过 LLM #1 —— 那条路零 token，代价是要自己补 `steps` / `started`

    副作用是 **pop `focus_stack` 并改 `ctx.focus`** —— 调用方（尤其新路由）
    **必须 `store.save(ctx)`**，否则这次出栈不会持久化。
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
        return [UICmd(op=UiOp.SHOW_PATH, target=target, rev=ctx.viewport.rev)]
    if getattr(data, "center", None) is not None:                # class-relations
        return [UICmd(op=UiOp.FOCUS_NODE,
                      target=NodeRef(type=NodeRefType.CLASS, uid=data.center.qualified_name),
                      rev=ctx.viewport.rev)]
    if ctx.focus is not None:                                    # method-body 等，就聚焦当前锚点
        return [UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=ctx.viewport.rev)]
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

    优先用 `headline`：`message` 现在是后端拼出来的 Markdown，直接截前 120 字
    会把 `##`/`**` 和半截语法一起塞进下一轮的 prompt。
    """
    summary = (
        explanation.headline
        or next((s.body for s in explanation.sections if s.body), None)
        or explanation.message
    )
    summary = summary.strip().replace("\n", " ")
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
    ctx: AgentContext, message: str, headline: str | None, citations: list[Citation],
    sections: list[Section], commands: list[UICmd], graph: GraphPatch | None,
    suggestions: list[Suggestion], steps: list[Step], intent_result: IntentResult,
    graph_rev: int, started: float,
) -> AgentResponse:
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=message,
        headline=headline,
        sections=sections,
        citations=citations,
        ui_commands=commands,
        graph=graph,
        suggestions=suggestions,
        steps=steps,
        meta=Meta(intent=intent_result.intent.value, confidence=intent_result.confidence,
                  graph_rev=graph_rev, latency_ms=_ms(started)),
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
