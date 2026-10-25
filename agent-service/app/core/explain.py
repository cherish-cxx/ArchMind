"""讲解（LLM #2）+ **降级到模板**。

## 为什么 P2 的模板函数没有随桩一起删
P2 在 `api/agent.py` 里写的 `_explain_chain` / `_explain_overview` 是「用工具结果拼人话」，
在 P3 之前它只是占位。但接上 LLM 之后它**恰好就是设计 §8-C2 要求的降级路径**：
「LLM 不可用时，图查询照常工作，只有讲解退化成模板化输出」。
所以它们被搬到这里而不是删掉 —— 那不是历史包袱，是预案。

## citations 的来源：一条被削弱的保证（要如实知道）
P2 的写法是**先选 citations，再用 citations 拼 message** —— 结构上不可能引用没出处的东西。
接上 LLM 之后这条保证**被削弱了**：message 是自由文本，我们没法从结构上拦住它提及图外的类。
现在剩下的机制是：
  1. prompt 硬规则 4：citedUids 只能从白名单挑
  2. `guard.split_citations`：越界的 citation 被丢掉并**记 error**
换言之，citations 仍严格受控，message 只能靠 prompt + 事后告警 —— 这是接 LLM 必然的代价，
不是疏忽。设计 §8-A2 的完全体（Java 侧再校验一遍 uiCommands）在 P4。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.guard import split_citations
from app.core.llm import Completer, LLMError, LLMNotConfiguredError
from app.core.prompt import explain_messages
from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.response import Citation, NodeRefType, Section
from app.models.tool import CallChain, GapReason, ProjectOverview, ToolEnvelope

logger = logging.getLogger(__name__)


@dataclass
class ExplainResult:
    message: str
    citations: list[Citation]
    sections: list[Section] = field(default_factory=list)
    headline: str | None = None
    degraded: bool = False      # True = LLM 没用上，走了模板。要如实体现在 steps 里


def _simple(uid: str) -> str:
    """全限定名 → 短名。methodUid 形如 `a.B#m(...)`，先把 `#` 之后砍掉。"""
    return uid.split("#", 1)[0].rsplit(".", 1)[-1]


# ==================== 主入口 ====================


async def explain(
    ctx: AgentContext,
    intent: Intent,
    envelopes: list[ToolEnvelope],
    evidenced: set[str],
    question: str,
    llm: Completer,
    *,
    rewritten: str | None = None,
) -> ExplainResult:
    """讲解这一轮的查询结果。

    `ctx` 只用于组装**会话记忆**（完整 history + 当前锚点），不参与 evidence 判定 ——
    citedUids 的真相仍然只有本轮工具返回的 evidence。

    `question` 是用户原话；`rewritten` 是 LLM #1 消解代词后的版本，作为提示附带。

    ⚠️ `LLMNotConfiguredError` **原样抛**：没配 key 是配置错误（该 500），
    不是「LLM 抖了一下」—— 把它降级成模板，就等于让一个配置问题伪装成正常运行。
    这个区分和 P2 里 java_client 对 401 的处理是同一条原则。
    """
    try:
        raw = await llm.complete(
            explain_messages(intent, envelopes, question, ctx, rewritten=rewritten),
            json_mode=True,
        )
        return _parse_and_ground(raw.text, evidenced)
    except LLMNotConfiguredError:
        raise
    except LLMError as e:
        logger.warning("LLM 讲解不可用，退化为模板输出（C2 降级）：%s", e)
    except ExplainParseError as e:
        logger.warning("LLM 讲解输出不可解析，退化为模板输出：%s", e)

    citations, message = _template(intent, envelopes, evidenced)
    return ExplainResult(message=message, citations=citations, degraded=True)


class ExplainParseError(Exception):
    """讲解输出既不是 {headline, sections} 也不是旧的 {message, citedUids}。"""


def _parse_and_ground(text: str, evidenced: set[str]) -> ExplainResult:
    """解析讲解输出，并**逐段**做 evidence 校验。

    兼容两种形状：新的 `{headline, sections[]}` 与旧的 `{message, citedUids}`。
    兼容不是为了向后兼容而兼容 —— LLM 偶尔退回旧格式，退回一次就让整轮降级到模板，
    代价比多写这十行大得多。
    """
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise ExplainParseError(f"{type(e).__name__}: {e}；原文前 200 字：{text[:200]!r}") from e
    if not isinstance(raw, dict):
        raise ExplainParseError(f"顶层不是对象：{type(raw).__name__}")

    headline, raw_sections = _read_sections(raw)

    sections: list[Section] = []
    citations: list[Citation] = []
    cited: set[str] = set()
    for i, item in enumerate(raw_sections, 1):
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            # 空段不致命，但它说明 prompt 与模型格式在漂移 —— 必须留痕
            logger.warning("第 %d 段正文为空，已丢弃", i)
            continue

        raw_uids = item.get("citedUids")
        proposed = [
            Citation(uid=u, kind=NodeRefType.CLASS)
            for u in (raw_uids if isinstance(raw_uids, list) else [])
            if isinstance(u, str)
        ]
        grounded, ungrounded = split_citations(proposed, evidenced)
        if ungrounded:
            # error 级：幻觉引用是这个系统最该被盯住的信号，不能只 warn。
            # 带上段标题，否则日志只能说「这条回答引用了不存在的东西」，定位不到是哪个论断编的。
            logger.error(
                "第 %d 段《%s》引用了 %d 个不存在的 uid，已剔除：%s",
                i, item.get("title") or "-", len(ungrounded), [c.uid for c in ungrounded][:5],
            )

        title = item.get("title")
        sections.append(Section(
            title=title.strip() if isinstance(title, str) else "",
            body=body.strip(),
            cited_uids=[c.uid for c in grounded],
        ))
        for c in grounded:
            if c.uid not in cited:
                cited.add(c.uid)
                citations.append(c)

    if not sections:
        raise ExplainParseError("没有任何可用段落（全部为空或格式不对）")

    return ExplainResult(
        message=_assemble_message(headline, sections),
        citations=citations,
        sections=sections,
        headline=headline,
    )


def _read_sections(raw: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """取出 headline 与段落列表，顺带兼容旧的 `{message, citedUids}`。

    旧形状被当成**一个没有标题的段落** —— 之后所有校验、拼装都走同一条路，
    不需要在别处再分支一次。
    """
    headline = raw.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        headline = None

    raw_sections = raw.get("sections")
    if raw_sections is None:
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ExplainParseError("既没有 sections，也没有可用的 message")
        return headline, [{"body": message, "citedUids": raw.get("citedUids") or []}]

    if not isinstance(raw_sections, list) or not raw_sections:
        raise ExplainParseError(f"sections 不是非空数组：{type(raw_sections).__name__}")
    if not all(isinstance(s, dict) for s in raw_sections):
        raise ExplainParseError("sections 里有不是对象的元素")
    return headline, raw_sections


def _assemble_message(headline: str | None, sections: list[Section]) -> str:
    """把结构化讲解拼成一段 Markdown。

    **`message` 是派生物，不是第二份真相** —— 让 LLM 自己再写一遍 message，
    两处必然不一致，而且没法判断以哪个为准。
    """
    blocks: list[str] = []
    if headline:
        blocks.append(f"## {headline}")
    for s in sections:
        if s.title:
            blocks.append(f"### {s.title}")
        blocks.append(s.body)
    return "\n\n".join(blocks)


# ==================== 降级模板（P2 搬过来，逻辑未改）====================


def _template(
    intent: Intent, envelopes: list[ToolEnvelope], evidenced: set[str]
) -> tuple[list[Citation], str]:
    """没有 LLM 时，用工具结果拼一段干巴巴但**句句有出处**的话。"""
    parts: list[str] = []
    citations: list[Citation] = []
    cited: set[str] = set()

    for env in envelopes:
        if not env.ok:
            parts.append(f"这个我答不了：{env.note or '工具没有给出原因'}")
            continue
        data: Any = env.data
        if isinstance(data, CallChain):
            cits, text = _template_chain(env, evidenced)
        elif isinstance(data, ProjectOverview):
            cits, text = [], _template_overview(data)
        else:
            cits, text = _template_generic(data)
        parts.append(text)
        for c in cits:
            if c.uid not in cited:
                cited.add(c.uid)
                citations.append(c)

    body = " ".join(p for p in parts if p) or "这轮没有取到数据。"
    return citations, f"（讲解服务暂不可用，以下是原始查询结果）{body}"


def _template_chain(
    env: ToolEnvelope[CallChain], evidenced: set[str]
) -> tuple[list[Citation], str]:
    assert env.data is not None
    data = env.data

    # ★ 先选 citations（只留 evidence 里出现过的），再拼 message —— 顺序不能反
    nodes = [n for n in data.nodes if n.uid in evidenced]
    if len(nodes) != len(data.nodes):
        logger.error(
            "call-chain 有 %d 个节点不在 evidence 里（Java 侧不一致），已剔除",
            len(data.nodes) - len(nodes),
        )
    citations = [Citation(uid=n.uid, kind=NodeRefType.CLASS) for n in nodes]

    start_name = _simple(data.start.qualified_name)
    impls = [_simple(n.uid) for n in nodes if n.via_fanout]
    downstream = [n for n in nodes if n.depth > 0]

    head = start_name + (f"（接口，实现类 {'、'.join(impls)}）" if impls else "")
    if downstream:
        by_module: dict[str, list[str]] = {}
        for n in downstream:
            by_module.setdefault(n.module or "（模块未知）", []).append(_simple(n.uid))
        groups = "；".join(f"{m}: {'、'.join(v)}" for m, v in by_module.items())
        body = f"{head} 往下调用了 {len(downstream)} 个类 —— {groups}。"
    else:
        body = f"{head} 下面没有解析到调用。"

    # ★ gaps 必须提及：把「跨服务调用」讲成「没找到」，等于丢掉了最有价值的信息
    remote = [g for g in data.gaps if g.reason is GapReason.REMOTE_SERVICE]
    if remote:
        named = [_simple(g.at) for g in remote if g.at in {c.uid for c in citations}]
        body += f" 注意：{len(remote)} 处调用跑到别的服务去了{'（' + '、'.join(named) + '）' if named else ''}。"
    others = [g for g in data.gaps if g.reason is not GapReason.REMOTE_SERVICE]
    if others:
        body += f" 注意：另有 {len(others)} 处未解析。"

    # ★ truncated 必须点破：不说的话，「只看到 3 个」会被读成「只有 3 个」
    if data.truncated:
        body += " 注意：结果被截断，以上不是全部。"

    return citations, body


def _template_overview(d: ProjectOverview) -> str:
    lead: list[str] = []
    if d.project_type:
        lead.append(f"这是一个{d.project_type}项目")
    if d.summary:
        lead.append(d.summary)
    body = "。".join(lead) if lead else "没有拿到项目概况"

    tail: list[str] = []
    if d.tech_stack:
        tail.append(f"技术栈 {len(d.tech_stack)} 项")
    if d.architecture and d.architecture.style:
        tail.append(f"架构 {d.architecture.style}")
    if d.modules:
        tail.append(f"{len(d.modules)} 个模块")
    if tail:
        body += f"（{'，'.join(tail)}）"
    return body


def _template_generic(data: Any) -> tuple[list[Citation], str]:
    """其余三个工具的兜底。

    **不编内容**：只报告「取到了多少条」并诚实说明讲解能力此刻不可用。
    与其用模板硬凑一段读起来像模像样的话，不如让用户知道现在是降级状态。
    """
    if isinstance(data, list):
        return [], f"取到 {len(data)} 条结果。"
    return [], "已取到数据。"
