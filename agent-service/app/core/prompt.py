"""Prompt 组装 —— 把 Context 和用户问题拼成 LLM 能读的消息。

**这里只发生字符串拼接，不发生任何 IO**：prompt 是这个系统里最需要反复调的东西，
把它和「调 LLM」「解析结果」分开，才能单独读、单独改、单独 diff。

⚠️ 两条容易被忽略的硬约束（踩过就会 400 / 解析失败）：
  1. `json_mode=True` 时 prompt 里**必须出现 "json" 字样** —— DeepSeek 同 OpenAI 的规矩。
     写提示词时顺手删掉那个词，接口立刻报错，且报的错跟真正的毛病八竿子打不着。
  2. 六个 intent 必须**连同各自的触发语义**一起列出来。「六个选项」是不够的 ——
     LLM 不知道 `EXPLORE_FLOW` 和 `EXPLORE_RELATION` 的分界，就会在两可之间乱选。
"""

import json

from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.tool import (
    CallChain,
    ClassRelations,
    GapReason,
    MethodBody,
    ProjectOverview,
    ToolEnvelope,
)

# 代词消解规则表（照抄设计 §6）。放进 prompt 而不是让 LLM 自由发挥 ——
# 「它」指代谁，在这个系统里是**有正确答案的**，答案就在 focus 里。
_PRONOUN_RULES = """\
代词消解规则（有锚点时必须照此消解，不要自由发挥）：
- 「它 / 这个 / 该类 / 上面那个」        → 指当前锚点 focus
- 「它的调用方 / 谁调它 / 谁依赖它」      → focus + direction=UP
- 「回到上一个 / 退回去 / 返回」          → intent=NAVIGATE（焦点出栈，不查图）
- 「往下看 / 展开 / 继续深挖」            → intent=NAVIGATE（展开邻居）
"""


def _focus_line(ctx: AgentContext) -> str:
    """把当前锚点写成一行给 LLM 看。

    只给 uid，不给类名猜测 —— 9月22 实测踩过：hmall 的实现类在 `.impl` 子包
    （`com.hmall.trade.service.impl.OrderServiceImpl`）。让 LLM 猜包名会得到「类不存在」，
    白白浪费一轮。**它需要的就是全限定名这个字符串本身。**
    """
    if ctx.focus is None:
        return "当前没有锚点（用户没点任何类）—— 代词无处消解，遇到「它」应回澄清。"
    return f"当前锚点 focus = {ctx.focus.uid}"


def _history_line(ctx: AgentContext, limit: int = 3) -> str:
    """最近几轮的摘要。用于「再往上看看」这类承接上文的话。"""
    if not ctx.history:
        return ""
    recent = ctx.history[-limit:]
    lines = [f"  第{h.turn}轮：用户问「{h.q}」，你答「{h.a}」" for h in recent]
    return "最近几轮对话：\n" + "\n".join(lines)


def intent_messages(ctx: AgentContext, message: str) -> list[dict[str, str]]:
    """Intent 判定（LLM #1）的输入。"""
    intents = "\n".join(f"- {i.value}：{_TRIGGERS[i]}" for i in Intent)
    context_block = "\n".join(
        x for x in (_focus_line(ctx), _history_line(ctx)) if x
    )

    system = f"""\
你是代码图谱问答系统的意图分类器。用户会问关于一个 Java 项目的问题，你要判断他到底想干什么。

可选的 intent（只能是这六个之一，不要发明新的）：
{intents}

{_PRONOUN_RULES}
判定要求：
- confidence 是你对自己判断的把握，0~1。**拿不准就给低分**，不要硬凑高分 ——
  系统会在低分时改问用户一句，那比拉出一张错误的图好得多。
- slots.entityRef 填用户指代的类（有锚点时通常就是它）；拿不准就留 null。
- slots.direction 只在明确问上下游时填 "DOWN" 或 "UP"。
- rewrittenQuery 是把代词消解掉之后的完整问句。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段：
{{"intent": "...", "confidence": 0.0, "slots": {{"entityRef": null, "direction": null, "depth": null}}, "rewrittenQuery": "..."}}"""

    user = f"""{context_block}

用户这次说：{message}"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


#: 每个 intent 的触发语义 —— 与设计 §6 的表一致。
#: 拆成 dict 而不是写死在 f-string 里，是为了让「加一个 intent」只需改一处。
_TRIGGERS: dict[Intent, str] = {
    Intent.EXPLORE_OVERVIEW: "问这个项目/模块整体是干嘛的，概览类",
    Intent.EXPLORE_FLOW: "问业务流程、调用链、一个请求怎么走的（核心场景）",
    Intent.EXPLORE_RELATION: "问某个类的作用、谁调用它、它依赖谁",
    Intent.EXPLORE_CODE: "问具体某个方法是怎么实现的、想看代码",
    Intent.NAVIGATE: "只是想移动视图：展开、跳过去、退回去",
    Intent.OUT_OF_SCOPE: "要写代码、改代码、评价代码质量、问与这个项目无关的事",
}


# ==================== 讲解（LLM #2）====================

#: gap 原因 → 说给用户听的话。
#: **必须逐条讲清「为什么断」** —— 一句笼统的「链路不完整」等于没讲，
#: 而跨服务调用恰恰是微服务项目里最该被讲出来的信息（9月21 call-chain 结论）。
_GAP_TEXT: dict[GapReason, str] = {
    GapReason.REMOTE_SERVICE: "跨服务调用（本图里没有下游，属正常边界）",
    GapReason.INTERFACE_NO_IMPLEMENTATION: "接口没有找到实现类",
    GapReason.NO_DOWNSTREAM_EDGE: "没有解析到下游调用",
    GapReason.EXTERNAL_DEPENDENCY: "外部依赖（第三方库）",
}


def _render_chain(data: CallChain) -> str:
    lines = [f"调用链起点：{data.start.qualified_name}"]
    if data.nodes:
        lines.append(f"共 {len(data.nodes)} 个下游类节点：")
        for n in data.nodes:
            mark = "（经由接口扇出到实现类）" if n.via_fanout else ""
            lines.append(f"  - depth={n.depth} {n.uid}{mark}  模块={n.module or '未知'}")
    else:
        lines.append("没有解析到任何下游节点。")

    if data.gaps:
        lines.append(f"断点 {len(data.gaps)} 处（**必须如实告诉用户**）：")
        for g in data.gaps:
            lines.append(f"  - {g.at}：{_GAP_TEXT.get(g.reason, g.reason.value)}")
    return "\n".join(lines)


def _render_overview(data: ProjectOverview) -> str:
    lines = []
    if data.project_type:
        lines.append(f"项目类型：{data.project_type}")
    if data.summary:
        lines.append(f"概述：{data.summary}")
    if data.description:
        lines.append(f"详细：{data.description}")
    if data.tech_stack:
        lines.append("技术栈：" + "、".join(filter(None, (t.name for t in data.tech_stack))))
    if data.architecture and data.architecture.style:
        lines.append(f"架构风格：{data.architecture.style}")
    if data.modules:
        lines.append(f"模块：{'、'.join(m.name for m in data.modules if m.name)}")
    return "\n".join(lines) or "（项目概况为空）"


def _render_relations(data: ClassRelations) -> str:
    lines = [f"中心类：{data.center.qualified_name}"]

    def _side(title: str, items) -> None:
        if not items:
            return
        lines.append(f"{title}：")
        for it in items:
            lines.append(f"  - {it.clazz.qualified_name}（{it.relation_type.value}）")

    if data.upstream:
        _side("上游（谁指向它）", (data.upstream.callers or []) + (data.upstream.dependents or []))
    if data.downstream:
        _side(
            "下游（它指向谁）",
            (data.downstream.callees or [])
            + (data.downstream.dependencies or [])
            + (data.downstream.parents or []),
        )
    return "\n".join(lines)


def _render_method_body(data: MethodBody) -> str:
    if not data.methods:
        return "（没有取到方法源码）" + (f"：{data.note}" if data.note else "")
    lines: list[str] = []
    for m in data.methods:
        if m.error:
            lines.append(f"--- {m.uid}  读取失败：{m.error}")
            continue
        lines.append(f"--- {m.uid}  (第 {m.start_line}-{m.end_line} 行)")
        if m.resolved_from:
            # 请求的是接口方法、实际切了实现类 —— 这层跳转必须让 LLM 知道，
            # 否则它会把实现类的代码当成接口的代码讲
            lines.append(f"    （接口 {m.resolved_from} 自动跳到了实现类）")
        lines.append(m.code or "")
    return "\n".join(lines)


def _render_envelope(env: ToolEnvelope) -> str:
    """把工具数据渲染成 LLM 能读的一段文字。

    CallChain / ProjectOverview 两个有专门渲染 —— 它们是 P2 就接线的两个工具，
    措辞调过。其余三个走 `model_dump()` 的兜底渲染：能用、不好看，
    等真有人用它们提问时再各自调措辞，现在写精了也是猜。
    """
    data = env.data
    if isinstance(data, CallChain):
        return _render_chain(data)
    if isinstance(data, ProjectOverview):
        return _render_overview(data)
    if isinstance(data, ClassRelations):
        return _render_relations(data)
    if isinstance(data, MethodBody):
        return _render_method_body(data)
    # 兜底：list[ClassNode] 等
    return json_dumps(data)


def json_dumps(obj) -> str:
    """兜底渲染。单独拆成函数是为了让上面那段读起来不被序列化细节打断。"""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(by_alias=True)
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _render_evidence(envelopes: list[ToolEnvelope]) -> tuple[str, list[str]]:
    """证据块 + **可引用 uid 白名单**。

    白名单是 prompt 里最重要的一件东西：LLM 只能从它里面挑 `citedUids`。
    注意它来自 `evidence`（Java 从图里捞的真实凭据），不是从 `data` 里现推 ——
    这两者偶尔会不一致，而 evidence 才是判「有没有出处」的基准。
    """
    lines: list[str] = []
    uids: list[str] = []
    seen: set[str] = set()
    for env in envelopes:
        for e in env.evidence:
            rel = f" -[{e.rel.value}]-> " if e.rel else " "
            lines.append(f"  {e.from_}{rel}{e.to or '（节点自身）'}")
            for u in (e.from_, e.to):
                if u and u not in seen:
                    seen.add(u)
                    uids.append(u)
    return "\n".join(lines) or "  （本轮没有图上的凭据）", uids


def explain_messages(intent: Intent, envelopes: list[ToolEnvelope], question: str) -> list[dict[str, str]]:
    """讲解（LLM #2）的输入。"""
    blocks: list[str] = []
    for env in envelopes:
        if env.ok and env.data is not None:
            blocks.append(_render_envelope(env))
        elif not env.ok:
            blocks.append(f"（工具没答上来：{env.note or '未知原因'}）")

    truncated = [e for e in envelopes if getattr(e.data, "truncated", False)]
    evidence_block, uids = _render_evidence(envelopes)

    system = f"""\
你在讲解一个 Java 项目的代码图谱。用户刚问了一个问题，系统查了图，下面是**全部可用信息**。

硬性规则（违反任何一条，这条回答就是错的）：
1. **只能使用下面给出的事实**，绝不用常识补齐 —— 图里没有的调用关系就是没有，
   哪怕你「知道」真实项目里应该有。这个项目的图有已知边界（不解析依赖注入和多态）。
2. **断点必须如实说**。下面如果列了「断点」，你必须在回答里提到「这些地方没解析到」，
   不能说成「到此结束」。把跨服务调用讲成「没有了」是必须避免的错误。
3. 下面如果标了「结果被截断」，你必须在回答里点破「以上不是全部」。
4. `citedUids` 只能从「可引用 uid」列表里挑，**一个都不能编**。这个字段会被后端逐条校验，
   编的会被丢掉并记警告。
5. 说人话：中文，直接回答用户的问题，不要复述这些规则、不要输出 markdown 标题。

只输出一个 json 对象：
{{"message": "给用户看的讲解，一段话", "citedUids": ["全限定名", ...]}}"""

    user = f"""\
用户的问题：{question}
判定的意图：{intent.value}

=== 查询到的信息 ===
{chr(10).join(blocks) or '（没有查到数据）'}

=== 图上的凭据 ===
{evidence_block}

=== 可引用 uid（citedUids 只能从这里挑）===
{uids}
{"（注意：本轮结果被截断，不是全部）" if truncated else ""}"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
