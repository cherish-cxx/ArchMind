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

from app.core.memory import render_memories
from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.memory import MemoryHit
from app.models.retrieval import Candidate
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
        base = "当前没有锚点（用户没点任何类）—— 代词无处消解，遇到「它」应回澄清。"
    else:
        base = f"当前锚点 focus = {ctx.focus.uid}"
    if ctx.method_uid:
        # 方法锁是「用户正在源码大纲里看哪个方法」，和画布焦点是两件事。
        # 不告诉意图判定，「这个方法」就会被消解成整个类。
        base += f"\n当前锁定的方法 = {ctx.method_uid}（用户正在源码大纲里看它；「这个方法 / 它」通常指它）"
    return base


def _prior_conversation(ctx: AgentContext) -> str:
    """把**本轮之前**的对话记录渲染成文本。

    当前这轮的用户问题已经由 `ctx.begin_turn` 追加进 messages，
    这里把它排除掉 —— 调用方会单独用「用户这次说」再给一次，
    否则同一句话会出现两遍。
    """
    prior = ctx.messages[:-1] if ctx.messages else []
    if not prior:
        return ""
    body = ctx.render_messages(prior)
    return "之前的对话：\n" + body if body else ""


def _screen_line(ctx: AgentContext) -> str:
    """前端最后一次上报的屏幕状态（一行摘要）。

    只给数量、页面类型和当前选中，**不给全部 uid** —— 讲解不需要把屏幕上每个节点
    都背下来，给了反而会诱导模型去提图外的东西。`rev = 0` 表示从未上报，不渲染。
    """
    if ctx.viewport.rev <= 0:
        return ""
    vp = ctx.viewport
    focused = vp.focused_node.uid if vp.focused_node else "无"
    truncated = "（已截断）" if vp.truncated else ""
    if vp.mode == "focus":
        scope = f"焦点视图（中心 = {vp.center_uid or focused}，{vp.hops or 1} 跳）"
    else:
        scope = "累积视图"
    folded = f"，有 {vp.folded_count} 个类被折叠进框" if vp.folded_count else ""
    return (f"前端屏幕快照：{scope}，可见节点 {len(vp.visible_nodes)} 个"
            f"{folded}，当前选中 = {focused}{truncated}")


def _candidates_block(candidates: list[Candidate] | None) -> str:
    """把检索候选渲染成 LLM1 能读的一段。

    **候选为空时返回空串** —— 那样 intent prompt 与 R4 之前逐字相同。
    这条"没检索就完全不变"的性质很重要：它是回归测试的基准。
    """
    if not candidates:
        return ""
    lines = ["本项目检索到的候选类（slots.entityRef 只能从下面选，一个字符都不要改）："]
    for i, c in enumerate(candidates, 1):
        desc = (c.description or "（无描述）").strip().replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:60] + "…"
        line = f"{i}. {c.uid} | {c.kind_label} | {desc}"
        if c.methods:
            line += " | 方法：" + "、".join(c.methods[:5])
        lines.append(line)
    return "\n".join(lines)


def render_candidates(candidates: list[Candidate] | None) -> str:
    """检索候选渲染成文本 —— 供 `core/loop.py` 写进 retrieval 消息。"""
    return _candidates_block(candidates)


def intent_messages(
    ctx: AgentContext,
    message: str,
    candidates: list[Candidate] | None = None,
    memories: list[MemoryHit] | None = None,
) -> list[dict[str, str]]:
    """Intent 判定（LLM #1）的输入。

    历史**不截断**：判断「这句话在接什么」同样需要整段对话，
    只给最近几轮会让第 4 轮之后的承接失效。
    """
    intents = "\n".join(f"- {i.value}：{_TRIGGERS[i]}" for i in Intent)
    context_block = "\n".join(
        x for x in (_focus_line(ctx), _prior_conversation(ctx)) if x
    )

    # 没有候选时这一段是空串 —— prompt 与 R4 之前逐字相同，老行为不受影响
    candidate_block = _candidates_block(candidates)
    candidate_section = ""
    entity_rule = "- slots.entityRef 填用户指代的类（有锚点时通常就是它）；拿不准就留 null。"
    if candidate_block:
        # 有候选时，entityRef 的语义变了：不是"这句话在指代谁"，而是"这轮从哪儿开始查"。
        # 不写清楚这一条，模型会把流程类问题判成"不指向具体类"而留 null，
        # 「不点类也能问」就永远走不通。
        candidate_section = f"""
{candidate_block}

候选使用规则（这一轮的锚点就靠它）：
- slots.entityRef 是这轮查图的**起点**，不要求"整个问题只关于这个类"。
  问流程、问实现、问某个模块怎么走的，同样要选一个起点。
- **先判断问题类型，再按类型选**，不要一律选 Controller/Service：
  - 问配置、加密、开关、阈值、过滤器、安全策略 → 配置类优先（*Config、*Properties）
  - 问返回什么结构/字段/凭证/参数 → DTO / Response / Request 优先
  - 问流程、调用链、一个请求怎么走 → Controller / Service 实现类优先
  - 问“怎么查/从哪查/有没有接口/怎么获取关系” → 查询服务优先；有实现类时选实现类，
    不要选“解析/推断”类的类（那是构建图时的逻辑，不是查询入口）
  - 问“怎么解析/怎么推断/怎么构建” → 解析器优先
  - 问谁调用它、被谁依赖、和谁有关系 → 用户问的那个类本身优先，不要替换成更泛的 Service 或接口
  - 问具体怎么实现 → 实现类优先于接口
- 选完后必须给 slots.anchorScore 打分：3=就是问它，2=高度相关，1=只是沾边，0=不匹配。
  如果最高只有 0 或 1，slots.entityRef 留 null —— 系统会带着候选去问用户，不要硬选。
- 照抄候选里的全限定名，不要改大小写、不要自己拼包名。
"""
        entity_rule = ("- slots.entityRef 从上面候选里选一个（见候选使用规则），"
                       "并填 slots.anchorScore；没有把握就留 null。")

    # 记忆块：只在真的有命中时出现。没有命中时拼出来是空串，
    # prompt 与 R5 之前逐字相同 —— 这条"没记忆就完全不变"的基线要一直保住。
    memory_section = ""
    memory_rule = ""
    if memories:
        lines = ["\n过去会话里与此相关的结论（可帮助消解指代，但不等于本轮已查证）："]
        for m in memories[:3]:
            subject = f"（实体：{m.subject_uids[0]}）" if m.subject_uids else ""
            lines.append(f"- {m.text}{subject}")
        memory_section = "\n".join(lines)
        memory_rule = (
            "\n- 如果用户这句话在指代上面记忆里的实体，slots.entityRef 填记忆给出的完整 uid；"
            "记忆只是线索，拿不准仍然留 null。"
        )

    system = f"""\
你是代码图谱问答系统的意图分类器。用户会问关于一个 Java 项目的问题，你要判断他到底想干什么。

可选的 intent（只能是这六个之一，不要发明新的）：
{intents}
{candidate_section}{memory_section}
{_PRONOUN_RULES}
判定要求：
- confidence 是你对自己判断的把握，0~1。**拿不准就给低分**，不要硬凑高分 ——
  系统会在低分时改问用户一句，那比拉出一张错误的图好得多。
{entity_rule}{memory_rule}
- slots.direction 只在明确问上下游时填 "DOWN" 或 "UP"。
- rewrittenQuery 是把代词消解掉之后的完整问句。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段：
{{"intent": "...", "confidence": 0.0, "slots": {{"entityRef": null, "anchorScore": null, "direction": null, "depth": null}}, "rewrittenQuery": "..."}}"""

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


def render_tool_result(env: ToolEnvelope) -> str:
    """把工具结果渲染成一段文本 —— 供循环写进 `tool` 消息。

    ⚠️ `ok:false` 必须**原样讲清原因**：Java 的业务失败是「200 + ok:false」，
    这时 `data` 是 None —— 只渲染 data 的话模型收到的是字面量 `null`，
    既不知道失败了、也不知道为什么，于是会拿同样的调用再试一遍。
    2026-11 的 A/B 实测就撞上了：`method-body` 连续三次读不到文件，
    模型每次都只拿到 `null`，八次工具预算全烧在这里。
    """
    if not env.ok:
        return json.dumps(
            {"ok": False, "note": env.note or "工具没有给出原因"},
            ensure_ascii=False,
        )
    return _render_envelope(env)


def json_dumps(obj) -> str:
    """兜底渲染。单独拆成函数是为了让上面那段读起来不被序列化细节打断。

    用 `default=` 而不是只处理顶层对象：`classes` 的 data 是 **list[ClassNode]**，
    顶层是 list（没有 `model_dump`），每个元素才有 —— 只转顶层会直接抛
    `not JSON serializable`。legacy 从没调过 `classes`，这条路一直没被走到过。
    """
    def fallback(value: object) -> object:
        if hasattr(value, "model_dump"):
            return value.model_dump(by_alias=True)
        raise TypeError(f"{type(value).__name__} 不是可序列化的工具结果")

    return json.dumps(obj, ensure_ascii=False, indent=2, default=fallback)


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


def _reference_block(candidates: list[Candidate] | None) -> str:
    """检索候选渲染成讲解侧的参考块。没有候选就返回空串。"""
    if not candidates:
        return ""
    head = candidates[0]
    desc = (head.description or "").strip().replace("\n", " ")
    others = "、".join(c.simple_name for c in candidates[1:])
    lines = [
        "=== 检索参考（未查证，不是证据）===",
        f"用户这句话最可能指向：{head.simple_name}（{head.uid}）"
        + (f" —— {desc[:60]}" if desc else ""),
        f"同批候选还有：{others or '无'}",
    ]
    return "\n".join(lines)


def explain_messages(
    intent: Intent,
    envelopes: list[ToolEnvelope],
    question: str,
    ctx: AgentContext,
    rewritten: str | None = None,
    candidates: list[Candidate] | None = None,
    memories: list[MemoryHit] | None = None,
) -> list[dict[str, str]]:
    """讲解（LLM #2）的输入。

    **会话记忆在这里进入 prompt**：`ctx.history` 全量注入（不截断），
    外加当前锚点与方法锁。没有它，第二轮开始 LLM #2 就是一张白纸 ——
    「它呢」「刚才那个」都无处消解。

    `question` 用**用户原话**，`rewritten` 只作为消歧提示。两者都给，
    才既不失真、又能处理代词。
    """
    truncated = [e for e in envelopes if getattr(e.data, "truncated", False)]
    evidence_block, uids = _render_evidence(envelopes)

    # 检索参考只在本轮真的检索过时注入。带进来的目的是让讲解能说
    # 「你说的可能是 X，我查的是 Y」—— 但它**不是证据**，不能进 citedUids。
    reference_block = _reference_block(candidates)
    reference_rule = ""
    if reference_block:
        reference_rule = """
12. 「检索参考」**不是证据**：它只说明检索觉得像哪个类，这些类**没有**被查过图。
    你可以在正文里提「同批候选里还有 X」这类信息，但**绝不能**把它们写进 citedUids，
    也不能声称它们有什么调用关系或实现细节 —— 那部分只能来自下面的证据。
"""

    # 记忆同理，但理由不同：检索参考是"同轮的猜测"，记忆是"上一轮的结论"。
    # 后者过期的方式是图变了，所以措辞里要带"可能基于旧版本"。
    memory_block = render_memories(memories)
    memory_rule = ""
    if memory_block:
        rule_no = 13 if reference_block else 12
        memory_rule = f"""
{rule_no}. 「记忆参考」来自过去的会话，可能基于旧版本的图。它只能帮你理解用户在延续什么，
    不能当作本轮证据：其中的类名与结论**不能**写进 citedUids，也不能声称图里一定还有它。
"""

    system = f"""\
你在讲解一个 Java 项目的代码图谱。用户刚问了一个问题，系统查了图，下面是**全部可用信息**。

受众：**刚接手这个项目的同学** —— 会 Java，但完全不熟悉这个项目的结构、命名和业务。
你写的每句话，都应该是一个新人读完能接上话的。

硬性规则（违反任何一条，这条回答就是错的）：
1. **只能使用下面给出的事实**，绝不用常识补齐 —— 图里没有的调用关系就是没有，
   哪怕你「知道」真实项目里应该有。这个项目的图有已知边界（不解析依赖注入和多态）。
2. **断点必须如实说**。下面如果列了「断点」，你必须在回答里提到「这些地方没解析到」，
   不能说成「到此结束」。把跨服务调用讲成「没有了」是必须避免的错误。
3. 下面如果标了「结果被截断」，你必须在回答里点破「以上不是全部」。
4. `citedUids` 只能从「可引用 uid」列表里挑，**一个都不能编**。这个字段会被后端逐条校验，
   编的会被丢掉并记警告。
5. **顺序固定：先结论，再路径，再证据。** 第一段用一两句话直接回答用户的问题；
   第二段讲它怎么发生（谁触发、按什么顺序、关键判断在哪）；第三段以后才展开细节和边界。
6. **禁止只罗列类名。** 「A 调用了 B、C、D」不是讲解，是清单 ——
   每提到一个类或方法，都要说清它在这里**干了什么**、**为什么是它**。
7. 新人不懂这个项目的术语：模块名、业务词、框架用法第一次出现时解释一句。
   不要假设他知道 `OrderServiceImpl` 和 `CartClient` 各自负责什么。
8. 正文用 Markdown 写：结论用**粗体**，并列信息用列表。
   **类名、方法名、泛型、代码片段一律用反引号包起来** —— `List<String>` 这样裸着写，
   会被前端当成 HTML 标签吞掉。
9. 正文**不要自己写 # 标题**（标题由 title 字段给），也不要复述这些规则。写 2~4 段。
10. **「之前的对话」是背景，不是证据**：它只用来理解用户这句话在承接什么。
    历史里出现过的类名 / uid **不能**当作本轮 `citedUids` 的来源 ——
    本轮可引用的只有下面那张列表，越界的引用会被后端剔除。
11. 用户这句话如果是承接上文（「它」「刚才那个」「继续说」），先用历史把指代解开再回答；
    但如果历史和本题无关，**不要强行扯上关系**，更不要为了显得有记忆而复述历史。
{reference_rule}{memory_rule}
只输出一个 json 对象，不要任何解释、不要 markdown 代码块。字段：
{{"headline": "一句话结论（不超过 40 字）",
 "sections": [{{"title": "这一段的标题", "body": "Markdown 正文", "citedUids": ["全限定名"]}}]}}

关于 citedUids：只填**给这一段提供依据**的 uid。如果这一段是在讲「图里没解析到」
这类边界，就留空数组 —— 空数组是合法的，不要为了填满而硬塞 uid。"""

    conversation = ctx.render_messages() or "（这是本会话的第一轮）"
    rewritten_line = ""
    if rewritten and rewritten.strip() and rewritten.strip() != question.strip():
        rewritten_line = f"系统对这句话的理解：{rewritten.strip()}\n"

    state_lines = [_focus_line(ctx)]
    screen = _screen_line(ctx)
    if screen:
        state_lines.append(screen)
    state_lines.append(f"判定的意图：{intent.value}")
    state_block = "\n".join(state_lines)
    # 为空时连空行都不留，保证「没检索」的 prompt 与老版本一致
    reference_section = f"{reference_block}\n\n" if reference_block else ""
    memory_section = f"{memory_block}\n\n" if memory_block else ""

    user = f"""\
=== 当前会话状态 ===
{state_block}
{rewritten_line}
=== 对话与工具记录（历史与工具返回是背景，不是本轮证据）===
{conversation}

{reference_section}{memory_section}=== 图上的凭据 ===
{evidence_block}

=== 可引用 uid（citedUids 只能从这里挑）===
{uids}
{"（注意：本轮结果被截断，不是全部）" if truncated else ""}"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ==================== 多步循环（R6 第二步）====================


def agent_system(
    ctx: AgentContext, memories: list[MemoryHit] | None = None
) -> str:
    """循环的系统提示。**工具清单不在这里** —— 它在请求的 `tools` 参数里。

    只放「怎么干」和「什么算错」：工具的用途写在各自的 `description` 里，
    这里再抄一遍就是两处定义，改一处必漏一处。

    记忆块与 `explain_messages` 同一套：**没有命中时逐字不变**，
    这条基线要一直保住。
    """
    state = "\n".join(x for x in (_focus_line(ctx), _screen_line(ctx)) if x)
    memory_block = render_memories(memories)
    memory_section = ""
    if memory_block:
        memory_section = f"""
{memory_block}
上面这些是**过去的结论，可能基于旧版本的图**：它只能帮你理解用户在延续什么，
里面的类名与结论不能写进 citedUids，也不能声称图里一定还有它。
"""
    return f"""\
你在协助一个刚接手这个 Java 项目的同学理解代码。你可以调用工具查图，最后用 `answer` 收口。

工作方式：
- 一次只查一件事：先确定起点（`classes` 或用户已选中的类），再 `call-chain` 看流程，
  需要实现细节再 `method-body`。
- 工具结果里有断点（gap）时，你可以继续往下查，也可以如实说明「这里是边界」。
- 证据够了就调 `answer`，不要为了显得勤快而重复查同一个东西。
- 问题含糊、缺少起点，或者超出这个项目能回答的范围时，用 `clarify` 反问一句并结束 ——
  不要硬猜。
- 想让画布跟着你走时用 `navigate`（focus / back / clear）。**查图本身不会改用户画布。**
- 工具的 uid 参数**只能用在工具结果里见过的全限定名**，不要自己拼包名。没见过就先 `classes`。
- 方法 uid 必须带完整签名（`owner#方法名(参数类型)`）。**不要用「类名 + 方法名」自己拼** ——
  检索候选里列的方法名只是提示，不是 uid。只想读源码时，直接给 `method-body` 传类的 uid，
  让它自己挑方法。

回答的硬性规则（违反任何一条，这条回答就是错的）：
1. 只能使用工具结果里的事实，绝不用常识补齐 —— 图里没有的调用关系就是没有。
2. 断点必须如实说「这些地方没解析到」，不能讲成「到此结束」。
3. 结果标了截断就必须点破「以上不是全部」。
4. 每段的 citedUids 只能从工具结果出现过的 uid 里挑，**一个都不能编** —— 后端会逐条校验，
   编的会被丢掉。这一段在讲图的边界时留空数组，不要为了填满而硬塞。
5. 顺序固定：先结论，再路径，再证据。
6. 禁止只罗列类名 —— 每提到一个类或方法，说清它在这里干了什么、为什么是它。
7. 新人不懂这个项目的术语：模块名、业务词第一次出现时解释一句。
8. 正文用 Markdown；类名、方法名、泛型、代码片段一律用反引号包起来。
9. 不要自己写 # 标题，标题走 section 的 title 字段。

当前状态：
{state}{memory_section}"""
