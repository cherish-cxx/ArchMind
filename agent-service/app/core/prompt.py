"""Prompt 组装 —— 把 Context、工具结果和用户问题拼成 LLM 能读的消息。

**这里只发生字符串拼接，不发生任何 IO**：prompt 是这个系统里最需要反复调的东西，
把它和「调 LLM」「解析结果」分开，才能单独读、单独改、单独 diff。

R6 第三步之后只剩两条 prompt：
  - `agent_system`：多步循环的系统提示（工具清单在请求的 `tools` 参数里，不在这里）；
  - `render_tool_result` / `render_candidates`：工具结果与检索候选的文本渲染。

六选一的 `intent_messages` 与单步讲解的 `explain_messages` 已随查表分发一起删除。
"""

import json

from app.core.memory import render_memories
from app.models.context import AgentContext
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
    """把检索候选渲染成一段给模型看的文本。

    **候选为空时返回空串** —— 没有候选时消息与 R4 之前逐字相同，
    这条"没检索就完全不变"的性质是回归基准。
    """
    if not candidates:
        return ""
    lines = ["本项目检索到的候选类（uid 只能照抄，一个字符都不要改）："]
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
    """检索候选渲染成文本 —— 供循环写进 retrieval 消息。"""
    return _candidates_block(candidates)


# ==================== 工具结果渲染 ====================

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


# ==================== 多步循环（R6 第二步）====================


def agent_system(
    ctx: AgentContext, memories: list[MemoryHit] | None = None
) -> str:
    """循环的系统提示。**工具清单不在这里** —— 它在请求的 `tools` 参数里。

    只放「怎么干」和「什么算错」：工具的用途写在各自的 `description` 里，
    这里再抄一遍就是两处定义，改一处必漏一处。

    记忆块：**没有命中时逐字不变**，这条基线要一直保住。
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
