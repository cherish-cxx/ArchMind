"""多步 Agent 循环（R6 第二步）—— 与 legacy 的「六选一」并存。

`core/loop.py` 是单步：判意图 → 查表 → 调**一个**工具 → 讲解。
这里是多步：把工具清单交给模型，它决定调哪个、调几次，最后用 `answer` 收口。
两条路共用 `execute_tool` / `render_tool_result` / `guard`，区别只在「谁决定调什么」。

循环体只有四步：发消息 → 收 `tool_calls` → 执行并回填 → 看要不要继续。

三条不变量都由**代码**保证，不交给模型：
  1. `citedUids` 必须是本轮 evidence 的子集 —— 复用 `explain.parse_answer`；
  2. 预算（步数 / 工具次数 / 模型次数）到点就停，并强制收尾；
  3. 工具失败变成一条 `tool` 消息，不终止整轮 —— 只有认证错误往上抛。

⚠️ `ctx.focus` **只有 `navigate` 工具会写**：查图不等于改用户的画布。
出图靠轮末从信封推导的 `uiCommands`；没有任何工具产出图时，回退到 `navigate`
留下的最后一条命令。下一轮「它」的基准 = 本轮结束时的 `ctx.focus`。
"""

import json
import logging
import re
import time
from typing import Any

from app.config import settings
from app.core.explain import (
    ExplainParseError,
    ExplainResult,
    parse_answer,
    template_answer,
)
from app.core.graph import build_graph_patch, cap_patch
from app.core.guard import evidence_uids, filter_ui_commands
from app.core.llm import (
    Completer,
    LLMError,
    LLMNotConfiguredError,
    LLMResult,
)
# legacy 里的这几段与六选一无耦合，直接复用；改名留给第三步删六选一时一并做。
from app.core.loop import (
    _memory_trigger,
    _recall_memories,
    _remember_explicit,
    _retrieve,
    _suggestions,
    ui_commands_for,
)
from app.core.memory import MemoryService
from app.core.prompt import agent_system, render_candidates, render_tool_result
from app.core.retriever import Retriever
from app.models.context import AgentContext, ChatMessage, MessageRole, ToolCall, ToolResult
from app.models.memory import MemoryHit
from app.models.response import (
    AgentResponse,
    GraphPatch,
    Meta,
    NodeRef,
    NodeRefType,
    Step,
    StepType,
    Suggestion,
    UICmd,
    UiOp,
)
from app.models.tool import MethodBody, ToolEnvelope
from app.tools.errors import ToolError
from app.tools.executor import execute_tool
from app.tools.java_client import (
    DEFAULT_METHOD_MAX_LINES,
    InternalAuthError,
    JavaClient,
    ToolTransportError,
)
from app.tools.registry import TOOLS, ToolContext, owner_of

logger = logging.getLogger(__name__)

#: 终止工具名。模型调它 = 本轮结束。
ANSWER = "answer"
CLARIFY = "clarify"
#: 本地工具：不碰 Java，只改会话状态 —— 所以**不扣工具预算、也不走去重缓存**
#: （`back` 调两次就该退两级，缓存它等于没退）。
NAVIGATE = "navigate"

#: 单条 tool 消息的字符上限。多步循环里 `messages` 只增不减，
#: 而 `message_window_max_chars` 只管落库后的窗口 —— 不在这里截，
#: 一次 `method-body` 就能把第三步顶爆。
MAX_TOOL_MESSAGE_CHARS = 8000

#: 交给模型的工具清单。字段说明来自 `ToolSpec`，不在这里重写一遍。
_ANSWER_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ANSWER,
        "description": (
            "给出最终回答并结束本轮。证据足够时调用；"
            "不要把工具结果原样复述，要讲清谁触发、按什么顺序、为什么。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "一句话结论，不超过 40 字。",
                },
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "description": "顺序固定：先结论、再路径、再细节。每段 1~3 句。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {
                                "type": "string",
                                "description": "Markdown 正文。类名、方法名用反引号包起来。",
                            },
                            "citedUids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "只填给这一段提供依据的 uid，必须出现在工具结果里。"
                                    "讲图的边界时留空数组。"
                                ),
                            },
                        },
                        "required": ["body"],
                    },
                },
            },
            "required": ["sections"],
        },
    },
}


_CLARIFY_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": CLARIFY,
        "description": (
            "问题有歧义、缺少起点、或者超出这个项目能回答的范围时，"
            "向用户反问一句并结束本轮。**不要用它替代回答** —— 能查就先查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问用户的话，一句话。",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可点的候选（0~3 个），每项会被当成用户下一句话发回来。",
                },
            },
            "required": ["question"],
        },
    },
}

_NAVIGATE_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": NAVIGATE,
        "description": (
            "移动会话的锚点（画布焦点）。focus 把锚点移到一个已知的类，"
            "back 退回上一个，clear 清空。查图本身**不会**改用户画布，只有这个工具会。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["focus", "back", "clear"],
                    "description": "focus 需要同时给 uid。",
                },
                "uid": {
                    "type": "string",
                    "description": "action=focus 时的类全限定名，必须是本轮见过的。",
                },
            },
            "required": ["action"],
        },
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    """五个数据工具 + 导航 / 澄清 / 作答。顺序稳定，方便测试和日志对照。"""
    definitions = [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in TOOLS.values()
    ]
    definitions.extend([_NAVIGATE_DEF, _CLARIFY_DEF, _ANSWER_DEF])
    return definitions


# ==================== 主入口 ====================


async def run_agent_turn(
    ctx: AgentContext,
    message: str,
    java: JavaClient,
    llm: Completer,
    retriever: Retriever | None = None,
    memory: MemoryService | None = None,
) -> AgentResponse:
    """跑一轮多步调查。

    `retriever` 只用来做**初始提示**（和 legacy 一样，在第一次 LLM 之前），
    模型仍然可以自己再 `classes` 一次 —— 预检索是省一次往返，不是唯一入口。

    记忆和 legacy 走**同一套触发规则**（`should_recall`）与同一条写入路径
    （`explicit_draft` → `MemoryService.remember`）—— 两侧各写一份，
    迟早会出现「同一个问题在两种模式下记的东西不一样」。
    会话结算蒸馏不在这里：那是后台任务扫 MySQL，与本轮走哪条链路无关。
    """
    ctx.begin_turn(message)
    started = time.perf_counter()
    steps: list[Step] = []

    retrieval = await _retrieve(ctx, message, retriever, steps)
    ctx.turn.retrieval = retrieval
    if retrieval.candidates:
        ctx.append_retrieval(
            render_candidates(retrieval.candidates),
            uids=[c.uid for c in retrieval.candidates],
        )

    memory_hits: list[MemoryHit] = []
    if memory is not None and memory.enabled:
        trigger = _memory_trigger(ctx, message, retrieval)
        if trigger is not None:
            memory_hits = await _recall_memories(ctx, message, memory, steps, trigger)
    ctx.turn.memory_hits = memory_hits

    known = _seed_known(ctx, retrieval)
    evidence: set[str] = set()
    cache: dict[str, ToolEnvelope] = {}
    envelopes: list[ToolEnvelope] = []
    #: `navigate` 想画到屏幕上的命令。只有本轮一个能产图的工具都没有时才用得上
    #: —— 有 SHOW_PATH 时以前者为准，否则用户每次都会被打回焦点视图。
    nav_commands: list[UICmd] = []

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent_system(ctx, memory_hits)},
        *ctx.to_llm_messages(),
    ]

    if ctx.method_uid:
        await _prefetch_locked_method(
            ctx, java, messages, known, cache, envelopes, evidence, steps
        )

    try:
        while not _exhausted(ctx):
            result = await _ask(ctx, messages, llm, steps)
            _remember(ctx, messages, result)

            if not result.tool_calls:
                # 模型只说话不调工具：不猜它的自由文本，去强制要一次结构化作答。
                break

            for index, call in enumerate(result.tool_calls):
                if call.name == ANSWER:
                    explanation = await _accept_answer(call, evidence, envelopes, java, ctx)
                    if explanation is None:
                        _append_tool(
                            ctx, messages, call,
                            _error_content(
                                "invalid_arguments",
                                "answer 的参数不合规：需要非空的 sections，每段有 body，"
                                "citedUids 只能是字符串数组。请重试。",
                                field="sections",
                            ),
                        )
                        continue
                    # ★ 收尾前先把「answer 及之后没执行的调用」都补上 tool 消息 ——
                    # 否则这份 messages 里会留下没有配对结果的 tool_call，下一轮直接 400。
                    _settle(ctx, messages, result.tool_calls[index:])
                    return await _respond_answer(
                        ctx, message, explanation, envelopes, evidence, steps, started,
                        nav_commands, memory,
                    )

                if call.name == CLARIFY:
                    asked = _accept_clarify(call)
                    if asked is None:
                        _append_tool(ctx, messages, call, _error_content(
                            "invalid_arguments",
                            "clarify 需要非空的 question；options 是可选的字符串数组。",
                            field="question",
                        ))
                        continue
                    question, options = asked
                    _settle(ctx, messages, result.tool_calls[index:])
                    return _respond_clarify(
                        ctx, question, options, envelopes, steps, started
                    )

                if call.name == NAVIGATE:
                    # 本地工具：不扣工具预算、不进缓存（back 两次就该退两级）。
                    content, command = _run_navigate(ctx, call, known, steps)
                    _append_tool(ctx, messages, call, content)
                    if command is not None:
                        nav_commands.clear()
                        nav_commands.append(command)
                    continue

                if not _tool_budget_left(ctx):
                    _append_tool(
                        ctx, messages, call,
                        _error_content("budget", "本轮工具调用次数已用完，请直接调 answer 收尾"),
                    )
                    continue

                content, env = await _run_call(ctx, call, java, known, cache, steps)
                _append_tool(ctx, messages, call, content)
                if env is not None:
                    envelopes.append(env)
                    gained = evidence_uids([env])
                    evidence |= gained           # 白名单：只认 envelope.evidence
                    known |= gained | _collect_uids(env.data)   # 起点：连 data 里的 uid 也算
    except LLMNotConfiguredError:
        raise
    except LLMError as e:
        logger.warning("循环里 LLM 不可用，退回模板：%s", e)
        return await _template_response(
            ctx, message, envelopes, evidence, steps, started, nav_commands, memory
        )

    return await _force_answer(
        ctx, message, java, messages, envelopes, evidence, steps, started, llm,
        nav_commands, memory,
    )


# ==================== 单次模型调用 ====================


async def _ask(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    llm: Completer,
    steps: list[Step],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> LLMResult:
    ctx.budget.steps_used += 1
    ctx.budget.llm_calls_this_turn += 1
    started = time.perf_counter()
    result = await llm.complete(
        messages,
        tools=tools if tools is not None else tool_definitions(),
        tool_choice=tool_choice,
    )

    ctx.turn.llm_calls.append({
        "purpose": "agent",
        "model": settings.llm_model,
        "rawResponse": result.text,
        # 连 arguments 一起留：`answer` 的结构化输出只在 tool_call 里，
        # 不存就没法在评测里数「引用越界」——落库的 citations 已经被过滤过了。
        "toolCalls": [{"name": c.name, "arguments": c.arguments} for c in result.tool_calls],
        # 收尾那次要能认出来：DeepSeek 会**无视**强制指定函数的 tool_choice，
        # 不记录的话「为什么退化成模板」只能靠猜。
        "toolChoice": tool_choice,
        "tokensIn": result.tokens_in,
        "tokensOut": result.tokens_out,
        "latencyMs": _ms(started),
    })
    detail = f"规划#{ctx.budget.llm_calls_this_turn}"
    if result.tool_calls:
        detail += "：" + "、".join(c.name for c in result.tool_calls)
    steps.append(Step(n=len(steps) + 1, type=StepType.LLM, detail=detail, ms=_ms(started)))
    return result


# ==================== 单次工具调用 ====================


async def _run_call(
    ctx: AgentContext,
    call: ToolCall,
    java: JavaClient,
    known: set[str],
    cache: dict[str, ToolEnvelope],
    steps: list[Step],
) -> tuple[str, ToolEnvelope | None]:
    """执行一次工具调用，返回 `(回填给模型的文本, 信封)`。

    信封为 None 表示「没有新证据」：命中缓存、参数错、工具挂了都算。
    这三种情况下模型仍然要收到一条 `tool` 消息，否则协议就断了。
    """
    key = _cache_key(call)
    if key in cache:
        logger.info("工具 %s 同参数重复调用，复用上一份结果（不扣预算）", call.name)
        steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                          detail=f"{call.name}(cached)", ms=0))
        return _clip(render_tool_result(cache[key])), None

    try:
        args = json.loads(call.arguments or "{}")
    except ValueError as e:
        return _clip(_error_content(
            "invalid_arguments", f"arguments 不是合法 JSON：{e}", field="arguments"
        )), None
    if not isinstance(args, dict):
        return _clip(_error_content(
            "invalid_arguments", "arguments 必须是 JSON 对象", field="arguments"
        )), None

    started = time.perf_counter()
    try:
        env = await execute_tool(
            call.name,
            java,
            ToolContext(project_id=ctx.project_id, known_uids=frozenset(known)),
            args,
        )
    except InternalAuthError:
        raise
    except ToolError as e:
        # 参数错 / 未知工具 / 起点不在已知集合 —— 都是「模型能自己改对」的失败。
        steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                          detail=f"{call.name}({e.reason.value})", ms=_ms(started)))
        return _clip(e.render()), None
    except ToolTransportError as e:
        logger.warning("工具 %s 不可用：%s", call.name, e)
        steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                          detail=f"{call.name}(down)", ms=_ms(started)))
        return _clip(_error_content("transport", f"{type(e).__name__}: {e}")), None

    cache[key] = env
    ctx.budget.tool_calls_this_turn += 1
    ctx.turn.tool_results.append(ToolResult(call.name, env))
    steps.append(Step(
        n=len(steps) + 1, type=StepType.TOOL,
        detail=f"{call.name}({args.get('uid') or args.get('methodUid') or '-'})",
        ms=_ms(started),
    ))
    return _clip(render_tool_result(env)), env


# ==================== 锁定方法 ====================


async def _prefetch_locked_method(
    ctx: AgentContext,
    java: JavaClient,
    messages: list[dict[str, Any]],
    known: set[str],
    cache: dict[str, ToolEnvelope],
    envelopes: list[ToolEnvelope],
    evidence: set[str],
    steps: list[Step],
) -> None:
    """用户锁了方法：循环开始前先把这段源码取回来，放进对话。

    legacy 里这是硬编码的第二步。循环里的模型**本来可以**自己调 `method-body`，
    但没有任何东西保证它会调 —— 而"锁了方法"等于用户说「现在问的是这段代码」，
    回答里没有那段代码的依据就是明显退步。所以这里替它取一次。

    取不到就**静默跳过**：这是锦上添花的一步，不能拿次要信息换掉整轮。
    """
    assert ctx.method_uid is not None
    started = time.perf_counter()
    try:
        env = await execute_tool(
            "method-body",
            java,
            ToolContext(project_id=ctx.project_id, known_uids=frozenset(known)),
            {"methodUid": ctx.method_uid, "maxLines": DEFAULT_METHOD_MAX_LINES},
        )
    except InternalAuthError:
        raise
    except (ToolError, ToolTransportError) as e:
        logger.info("锁定方法 %s 预取失败，本轮按图作答：%s", ctx.method_uid, e)
        return

    if not env.ok:
        logger.info("锁定方法 %s 取不到源码：%s", ctx.method_uid, env.note)
        return

    call = ToolCall(
        id="call_locked_method",
        name="method-body",
        arguments=json.dumps({"methodUid": ctx.method_uid}, ensure_ascii=False),
    )
    # 只进**在途**的 messages，不进 ctx.messages：这条交换是替模型做的，
    # 而 commit_turn 的窗口收缩会把 tool 消息摘掉，留下一条空的 assistant 反而制造噪音。
    # 审计轨迹在 `ctx.turn.tool_results` 里，那条会落库。
    messages.append(LLMResult(content=None, tool_calls=(call,)).to_message())
    messages.append({
        "role": "tool",
        "tool_call_id": call.id,
        "content": _clip(render_tool_result(env)),
    })

    cache[_cache_key(call)] = env
    ctx.budget.tool_calls_this_turn += 1
    ctx.turn.tool_results.append(ToolResult("method-body", env))
    envelopes.append(env)
    gained = evidence_uids([env])
    evidence |= gained
    known |= gained | _collect_uids(env.data)
    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"method-body({ctx.method_uid})", ms=_ms(started)))


# ==================== 收尾 ====================


async def _force_answer(
    ctx: AgentContext,
    message: str,
    java: JavaClient,
    messages: list[dict[str, Any]],
    envelopes: list[ToolEnvelope],
    evidence: set[str],
    steps: list[Step],
    started: float,
    llm: Completer,
    nav_commands: list[UICmd],
    memory: MemoryService | None,
) -> AgentResponse:
    """预算耗尽 / 模型一直不给答案：逼一次 `answer`，再不行退回模板。

    ⚠️ **不能只靠 `tool_choice` 指定函数名**：DeepSeek 只认
    `none / auto / required`，传对象形式会被静默当成 `auto`。
    2026-11 实测：强制收尾那次它照样调了 `method-body`，于是整轮退化成模板。
    所以这里**只把 `answer` 这一个工具递过去**，再配 `required` ——
    没有别的工具可选，它就没有跑偏的余地。
    """
    nudge = {
        "role": "system",
        "content": "本轮工具调用预算已用完。现在只能调用 answer 给出最终回答，不要再调用任何其他工具。",
    }
    try:
        result = await _ask(
            ctx, [*messages, nudge], llm, steps,
            tools=[_ANSWER_DEF], tool_choice="required",
        )
    except LLMNotConfiguredError:
        raise
    except LLMError as e:
        logger.warning("强制收尾失败：%s", e)
        return await _template_response(
            ctx, message, envelopes, evidence, steps, started, nav_commands, memory
        )

    for call in result.tool_calls:
        if call.name != ANSWER:
            continue
        explanation = await _accept_answer(call, evidence, envelopes, java, ctx)
        if explanation is not None:
            _settle(ctx, messages, result.tool_calls)
            return await _respond_answer(
                ctx, message, explanation, envelopes, evidence, steps, started,
                nav_commands, memory,
            )
        break

    return await _template_response(
        ctx, message, envelopes, evidence, steps, started, nav_commands, memory
    )


async def _accept_answer(
    call: ToolCall,
    evidence: set[str],
    envelopes: list[ToolEnvelope],
    java: JavaClient,
    ctx: AgentContext,
) -> ExplainResult | None:
    """`answer` 的参数就是旧 LLM #2 的 JSON —— 走同一套 grounding。

    白名单 = 图上证据 ∪ 源码里出现过的类。后者是循环特有的：
    读了 `method-body` 之后模型自然会引用源码里的类型，而那些类型
    在图上没有边（见 `_source_uids`）。
    """
    grounded = evidence | await _source_uids(
        envelopes, java, ctx, _proposed_uids(call.arguments) - evidence
    )
    try:
        return parse_answer(call.arguments, grounded)
    except ExplainParseError as e:
        logger.warning("answer 参数不合规：%s", e)
        return None


def _accept_clarify(call: ToolCall) -> tuple[str, list[str]] | None:
    """`clarify` 只要求一句非空的问话；options 是可选的字符串数组，最多留 3 个。"""
    try:
        args = json.loads(call.arguments or "{}")
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return None

    raw_options = args.get("options")
    options = []
    if isinstance(raw_options, list):
        options = [o.strip() for o in raw_options if isinstance(o, str) and o.strip()]
    return question.strip(), options[:3]


def _respond_clarify(
    ctx: AgentContext,
    question: str,
    options: list[str],
    envelopes: list[ToolEnvelope],
    steps: list[Step],
    started: float,
) -> AgentResponse:
    """反问并结束。

    **不返回图**（和 legacy 的 `_clarify` 一致）：这一轮的意思是「我还不确定」，
    这时候再画一张图只会引导用户往错的方向看。

    `append_event` 是必须的 —— 窗口收缩会把 `tool_calls` 摘掉，
    只靠 assistant 消息里的 clarify 参数，下一轮就看不到自己问过什么了。
    """
    ctx.append_event(question, outcome="CLARIFY")
    ctx.turn.answer_message = question
    ctx.turn.steps = list(steps)
    ctx.finish_turn("CLARIFY", _ms(started))

    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=question,
        suggestions=[Suggestion(label=o) for o in options],
        steps=steps,
        meta=Meta(intent="AGENT", confidence=1.0,
                  graph_rev=_graph_rev(envelopes), latency_ms=_ms(started)),
    )


def _run_navigate(
    ctx: AgentContext, call: ToolCall, known: set[str], steps: list[Step]
) -> tuple[str, UICmd | None]:
    """本地导航：只改 `ctx.focus` / `focus_stack`，**不碰 Java、不扣工具预算**。

    返回 `(回填给模型的文本, 要带给前端的命令)`。命令只在**没有任何工具能产图**
    时才会被采用 —— 有 `SHOW_PATH` 的时候用它会让用户每次都被打回焦点视图。
    """
    try:
        args = json.loads(call.arguments or "{}")
    except ValueError as e:
        return _error_content(
            "invalid_arguments", f"arguments 不是合法 JSON：{e}", field="arguments"
        ), None
    if not isinstance(args, dict):
        return _error_content(
            "invalid_arguments", "arguments 必须是 JSON 对象", field="arguments"
        ), None

    action = args.get("action")
    started = time.perf_counter()
    command: UICmd | None = None

    if action == "focus":
        raw = args.get("uid")
        if not isinstance(raw, str) or not raw.strip():
            return _error_content(
                "invalid_arguments", "action=focus 需要 uid", field="uid"
            ), None
        uid = owner_of(raw.strip())
        if uid not in known:
            sample = sorted(known)[:5]
            hint = (
                f"{uid} 不在本轮已见过的实体里；先用 classes 或 call-chain 找到它，"
                f"或从这些已知起点里选：{sample}"
                if sample
                else "本轮还没查到任何实体；先调 classes 或 overview"
            )
            return _error_content("invalid_arguments", hint, field="uid"), None
        if ctx.focus is None or ctx.focus.uid != uid:
            if ctx.focus is not None:
                ctx.focus_stack.append(ctx.focus.uid)
            ctx.focus = NodeRef(type=NodeRefType.CLASS, uid=uid)
        note = f"会话锚点已移到 {uid}"
        command = UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=ctx.viewport.rev)

    elif action == "back":
        if ctx.focus_stack:
            uid = ctx.focus_stack.pop()
            ctx.focus = NodeRef(type=NodeRefType.CLASS, uid=uid)
            note = f"回到上一个锚点：{uid}"
            command = UICmd(op=UiOp.FOCUS_NODE, target=ctx.focus, rev=ctx.viewport.rev)
        else:
            ctx.focus = None
            note = "已经在最开始了"
            command = UICmd(op=UiOp.CLEAR_VIEW, rev=ctx.viewport.rev)

    elif action == "clear":
        ctx.focus = None
        note = "已清空锚点"
        command = UICmd(op=UiOp.CLEAR_VIEW, rev=ctx.viewport.rev)

    else:
        return _error_content(
            "invalid_arguments", "action 只能是 focus / back / clear", field="action"
        ), None

    steps.append(Step(n=len(steps) + 1, type=StepType.TOOL,
                      detail=f"navigate({action})", ms=_ms(started)))
    return json.dumps(
        {
            "ok": True,
            "action": action,
            "focus": ctx.focus.uid if ctx.focus is not None else None,
            "note": note,
        },
        ensure_ascii=False,
    ), command


async def _respond_answer(
    ctx: AgentContext,
    message: str,
    explanation: ExplainResult,
    envelopes: list[ToolEnvelope],
    evidence: set[str],
    steps: list[Step],
    started: float,
    nav_commands: list[UICmd],
    memory: MemoryService | None,
) -> AgentResponse:
    # 显式"记住…"要在落库**之前**写，否则 ctx.messages 里那条回答会少一行确认。
    # ★ 顺序不能反：`explicit_draft` 要从 `ctx.turn.evidence` 里挑 subject，
    #   证据没先落进去，这条"记住"就会被静默丢掉。
    ctx.turn.evidence = sorted(evidence)
    note = await _write_memory(ctx, message, explanation, _graph_rev(envelopes), memory)
    if note:
        explanation.message = f"{explanation.message}\n\n{note}"

    # 有能产图的工具就用它推出来的命令；一个都没有时才用 navigate 留下的那条。
    commands = _ui_commands_for_all(envelopes, ctx) or nav_commands
    kept, dropped = filter_ui_commands(commands, evidence | _canvas_uids(ctx))
    if dropped:
        logger.warning("丢弃了 %d 条指向不存在节点的 UICommand：%s",
                       len(dropped), [c.op.value for c in dropped])

    graph = _merge_graph(envelopes)
    _record_answer(ctx, explanation, evidence, kept, graph, steps, started)
    return _response(ctx, explanation.message, explanation.headline,
                     explanation.citations, explanation.sections,
                     kept, graph, steps, evidence, envelopes, started)


async def _template_response(
    ctx: AgentContext,
    message: str,
    envelopes: list[ToolEnvelope],
    evidence: set[str],
    steps: list[Step],
    started: float,
    nav_commands: list[UICmd],
    memory: MemoryService | None,
) -> AgentResponse:
    """最后的兜底：拼一段句句有出处的话，并如实说是降级。

    前缀在这里加而不是在 `template_answer` 里 —— 预算耗尽时模型本身是好的，
    写「讲解服务不可用」就是在撒谎。
    """
    citations, body = template_answer(envelopes, evidence)
    text = f"（本轮没能生成结构化讲解，以下是已经查到的结果）{body}"

    ctx.turn.evidence = sorted(evidence)
    note = await _write_memory(
        ctx, message,
        ExplainResult(message=text, citations=citations, degraded=True),
        _graph_rev(envelopes), memory,
    )
    if note:
        text = f"{text}\n\n{note}"

    graph = _merge_graph(envelopes)
    commands = _ui_commands_for_all(envelopes, ctx) or nav_commands
    kept, _ = filter_ui_commands(commands, evidence | _canvas_uids(ctx))

    ctx.append_answer(text, citations=[c.model_dump(mode="json", by_alias=True)
                                       for c in citations], degraded=True)
    ctx.turn.headline = None
    ctx.turn.answer_message = text
    ctx.turn.sections = []
    ctx.turn.citations = citations
    ctx.turn.evidence = sorted(evidence)
    ctx.turn.ui_commands = kept
    ctx.turn.graph = graph
    ctx.turn.steps = list(steps)
    ctx.finish_turn("DEGRADED", _ms(started))

    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=text,
        citations=citations,
        ui_commands=kept,
        graph=graph,
        suggestions=_suggestions(None, ctx),
        steps=steps,
        meta=Meta(intent="AGENT", confidence=1.0,
                  graph_rev=_graph_rev(envelopes), latency_ms=_ms(started)),
    )


async def _write_memory(
    ctx: AgentContext,
    message: str,
    explanation: ExplainResult,
    graph_rev: int,
    memory: MemoryService | None,
) -> str | None:
    """显式记忆的写入。记忆是附加路径：没开、没触发词、写失败，一律返回 None。"""
    if memory is None or not memory.enabled:
        return None
    return await _remember_explicit(ctx, message, explanation, graph_rev, memory)


def _record_answer(
    ctx: AgentContext,
    explanation: ExplainResult,
    evidence: set[str],
    commands: list[UICmd],
    graph: GraphPatch | None,
    steps: list[Step],
    started: float,
) -> None:
    ctx.append_answer(
        explanation.message,
        headline=explanation.headline,
        sections=[s.model_dump(mode="json", by_alias=True) for s in explanation.sections],
        citations=[c.model_dump(mode="json", by_alias=True) for c in explanation.citations],
        degraded=explanation.degraded,
    )
    ctx.turn.headline = explanation.headline
    ctx.turn.answer_message = explanation.message
    ctx.turn.sections = explanation.sections
    ctx.turn.citations = explanation.citations
    ctx.turn.evidence = sorted(evidence)
    ctx.turn.ui_commands = commands
    ctx.turn.graph = graph
    ctx.turn.steps = list(steps)
    ctx.finish_turn("OK", _ms(started))


def _response(
    ctx: AgentContext,
    message: str,
    headline: str | None,
    citations: list,
    sections: list,
    commands: list[UICmd],
    graph: GraphPatch | None,
    steps: list[Step],
    evidence: set[str],
    envelopes: list[ToolEnvelope],
    started: float,
) -> AgentResponse:
    return AgentResponse(
        conversation_id=ctx.conversation_id,
        message=message,
        headline=headline,
        sections=sections,
        citations=citations,
        ui_commands=commands,
        graph=graph,
        suggestions=_suggestions(None, ctx),
        steps=steps,
        # 六选一还在，意图这一栏对本轮没有意义 —— 写一个可识别的标签，
        # 而不是硬塞一个假的 EXPLORE_xxx。第三步会把它换成 None。
        meta=Meta(intent="AGENT", confidence=1.0,
                  graph_rev=_graph_rev(envelopes), latency_ms=_ms(started)),
    )


# ==================== 消息流 ====================


def _remember(ctx: AgentContext, messages: list[dict[str, Any]], result: LLMResult) -> None:
    """把模型这次说的话原样记进两条流：在途的 `messages` 和持久化的 `ctx.messages`。"""
    messages.append(result.to_message())
    ctx.append_message(ChatMessage(
        role=MessageRole.ASSISTANT,
        content=result.content,
        tool_calls=list(result.tool_calls) or None,
    ))


def _append_tool(
    ctx: AgentContext, messages: list[dict[str, Any]], call: ToolCall, content: str
) -> None:
    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    ctx.append_message(ChatMessage(
        role=MessageRole.TOOL,
        content=content,
        name=call.name,
        tool_call_id=call.id,
    ))


def _settle(ctx: AgentContext, messages: list[dict[str, Any]], calls: list[ToolCall]) -> None:
    """给还没执行的调用补一条 tool 结果，保住「每个 tool_call 都有配对」这条协议。"""
    notes = {ANSWER: "已采纳为最终回答", CLARIFY: "已采纳为澄清"}
    for call in calls:
        note = notes.get(call.name, "本轮已结束，未执行")
        _append_tool(ctx, messages, call,
                     json.dumps({"ok": True, "note": note}, ensure_ascii=False))


# ==================== 已知实体 ====================


def _seed_known(ctx: AgentContext, retrieval: Any) -> set[str]:
    """本轮的初始起点：用户选中的类、锁定的方法、预检索的候选。

    **不等于证据** —— 它只决定「模型能不能拿这个 uid 去查」，
    引用白名单仍然只认工具返回的 evidence。
    """
    known: set[str] = set()
    if ctx.focus is not None:
        known.add(ctx.focus.uid)
    # 焦点栈里的都是**上一轮真实工具结果**存下来的，`back` 回去之后要能接着查
    known.update(ctx.focus_stack)
    if ctx.method_uid:
        known.add(ctx.method_uid)
        known.add(owner_of(ctx.method_uid))
    for candidate in getattr(retrieval, "candidates", ()) or ():
        known.add(candidate.uid)
    return known


#: 契约里所有表示实体的字段名。新增 DTO 时如果换了名字，这里要跟着加。
_UID_FIELDS = frozenset({"qualifiedName", "uid", "from", "to", "owner"})

#: 源码里的类名 token。`String` / `Integer` 这类也会命中，靠 roster 过滤掉。
_SIMPLE_NAME = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")


def _collect_uids(data: Any) -> set[str]:
    """从工具 data 里捞出所有全限定名。

    为什么不只看 `evidence`：`classes` 是个纯清单，没有 evidence ——
    模型拿它挑出来的类名，必须能成为下一次调用的起点，否则「先列再查」永远走不通。
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if hasattr(node, "model_dump"):
            node = node.model_dump(by_alias=True)
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _UID_FIELDS and isinstance(value, str) and value:
                    found.add(value)
                else:
                    walk(value)

    walk(data)
    return found


def _uids_in(envelopes: list[ToolEnvelope]) -> set[str]:
    uids: set[str] = set()
    for env in envelopes:
        uids |= _collect_uids(env.data)
        for e in env.evidence:
            uids.add(e.from_)
            if e.to:
                uids.add(e.to)
    return uids


async def _class_uids(java: JavaClient, ctx: AgentContext) -> set[str]:
    """项目全量类名表。**只进内存、不进 prompt、不扣工具预算**。

    只在本轮读过源码、且源码里出现了花名册里没有的类名时才补这一次 ——
    它是**校验**用的权威名单，不是模型手里的工具。
    """
    try:
        env = await execute_tool(
            "classes", java, ToolContext(project_id=ctx.project_id), {"limit": 200}
        )
    except (ToolError, ToolTransportError) as e:
        logger.info("补全类名表失败，引用只能按已知实体校验：%s", e)
        return set()
    if not env.ok or env.data is None:
        return set()
    # 不假设 data 一定是 list[ClassNode]：走通用采集，非清单形状自然得空集
    return _collect_uids(env.data)


def _proposed_uids(arguments: str) -> set[str]:
    """从 `answer` 的参数里取出模型提出的引用。只为校验用，不做别的。"""
    try:
        obj = json.loads(arguments or "{}")
    except ValueError:
        return set()
    if not isinstance(obj, dict):
        return set()

    uids: set[str] = set()
    for section in obj.get("sections") or []:
        if isinstance(section, dict):
            uids.update(u for u in (section.get("citedUids") or []) if isinstance(u, str))
    uids.update(u for u in (obj.get("citedUids") or []) if isinstance(u, str))
    return uids


async def _source_uids(
    envelopes: list[ToolEnvelope],
    java: JavaClient,
    ctx: AgentContext,
    wanted: set[str],
) -> set[str]:
    """`wanted` 里**源码提到过、且图上真实存在**的那部分。

    **为什么要放宽到源码**：局部变量类型不产生图的边
    （`Order order = new Order()` 是方法体里的局部变量，而关系解析只认
    字段 / 参数 / 返回类型 / 方法调用）。但模型确实读到了这段源码，
    引用 `Order` 是有出处的 —— 把标准卡在「图上有没有边」，
    等于让它亲眼看到的事实不算数。

    两条边界：
      - **只认模型自己给出的全限定名**，不做短名猜测 —— 同名不同包时
        （`Order` 在 hmall 里有两份）猜哪一半都可能猜错；
      - 仍然要求这个类在图里存在，否则引用会指向不存在的节点。
        花名册只在「源码提到了、但本轮没查过」时才补一次
        （只进内存、不进 prompt、不扣工具预算）。
    """
    bodies = [env.data for env in envelopes if isinstance(env.data, MethodBody)]
    if not bodies or not wanted:
        return set()

    tokens = {
        token
        for data in bodies
        for method in data.methods
        for token in _SIMPLE_NAME.findall(method.code or "")
    }
    hits = {uid for uid in wanted if uid.rsplit(".", 1)[-1] in tokens}
    if not hits:
        return set()

    uids = _uids_in(envelopes)
    if not hits <= uids:
        uids |= await _class_uids(java, ctx)
    return {uid for uid in hits if uid in uids}


# ==================== 出图 ====================


def _ui_commands_for_all(
    envelopes: list[ToolEnvelope], ctx: AgentContext
) -> list[UICmd]:
    """按查询顺序取第一个能产出命令的信封。

    **不合并**：多个 SHOW_PATH 会互相覆盖，前端只会执行最后一条。
    取第一个（通常是最开始的 call-chain）最稳定。
    """
    for env in envelopes:
        commands = ui_commands_for(env, ctx)
        if commands:
            return commands
    return []


def _merge_graph(envelopes: list[ToolEnvelope]) -> GraphPatch | None:
    """多步之后把各轮的子图并成一张，去重与截断沿用 `cap_patch`。"""
    nodes, edges, gaps = [], [], []
    seen_edges: set[tuple[str, str, str]] = set()
    seen_gaps: set[tuple[str, str]] = set()
    truncated = False

    for env in envelopes:
        patch = build_graph_patch(env)
        if patch is None:
            continue
        nodes.extend(patch.nodes)
        truncated = truncated or patch.truncated
        for gap in patch.gaps:
            key = (gap.at, gap.reason.value)
            if key not in seen_gaps:
                seen_gaps.add(key)
                gaps.append(gap)
        for edge in patch.edges:
            key = (edge.from_, edge.to, edge.rel.value)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append(edge)

    if not nodes and not edges and not gaps:
        return None
    return cap_patch(GraphPatch(
        nodes=nodes, edges=edges, gaps=gaps, truncated=truncated,
    ))


def _canvas_uids(ctx: AgentContext) -> set[str]:
    """屏幕上确实存在的 uid。

    给 `filter_ui_commands` 一个正确的权威来源：`FOCUS_NODE` 指向的可能是
    用户上一轮点过的类，而本轮没查它，evidence 里当然没有。
    这不是放松校验 —— 这些 uid 来自**上一轮真实 tool 结果**存下来的状态。
    """
    uids = set(ctx.focus_stack)
    if ctx.focus is not None:
        uids.add(ctx.focus.uid)
    return uids


# ==================== 预算 ====================


def _exhausted(ctx: AgentContext) -> bool:
    budget = ctx.budget
    return (
        budget.steps_used >= budget.steps_limit
        or budget.llm_calls_this_turn >= budget.llm_calls_limit
        or budget.tool_calls_this_turn >= budget.tool_calls_limit
    )


def _tool_budget_left(ctx: AgentContext) -> bool:
    return ctx.budget.tool_calls_this_turn < ctx.budget.tool_calls_limit


# ==================== 小工具 ====================


def _cache_key(call: ToolCall) -> str:
    """同工具 + 同参数 → 同一个键。参数按 key 排序，换个顺序也算同一个。"""
    try:
        canonical = json.dumps(
            json.loads(call.arguments or "{}"), sort_keys=True, ensure_ascii=False
        )
    except ValueError:
        canonical = call.arguments
    return f"{call.name}:{canonical}"


def _error_content(reason: str, hint: str, *, field: str | None = None) -> str:
    """失败回填成模型能读的 JSON —— 和 `ToolError.render()` 同一个形状。"""
    payload: dict[str, Any] = {"ok": False, "reason": reason, "hint": hint}
    if field is not None:
        payload["field"] = field
    return json.dumps(payload, ensure_ascii=False)


def _clip(content: str) -> str:
    if len(content) <= MAX_TOOL_MESSAGE_CHARS:
        return content
    return content[:MAX_TOOL_MESSAGE_CHARS] + "\n…（工具结果过长已截断，以上不是全部）"


def _graph_rev(envelopes: list[ToolEnvelope]) -> int:
    return max((env.graph_rev for env in envelopes), default=0)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
