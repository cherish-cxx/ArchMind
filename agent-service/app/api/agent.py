"""Python 侧对外暴露的接口（Java → Python）。

Python **不是**给前端直接调的 —— 它是 Java 后面的内部服务。
所以这里和 Java 的 `/internal/**` 一样：只认共享密钥，不认用户 JWT。
"""

import logging
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.models.protocol import AskRequest, EvictRequest
from app.models.response import AgentResponse, Citation, Meta, NodeRefType, Step, StepType
from app.models.tool import CallChain, ChainDirection, GapReason, ProjectOverview, ToolEnvelope
from app.tools.java_client import DEFAULT_CHAIN_DEPTH, JavaClient, ToolTransportError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/agent", tags=["agent"])


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
) -> None:
    """共享密钥校验。**不走 JWT** —— 调用方是 Java，按设计它拿不到用户 token。

    用 `compare_digest` 而不是 `==`：定长比较，避免逐字符短路带来的时序侧信道。
    和 Java 侧 `InternalTokenFilter` 的做法对齐。
    """
    if x_internal_token is None or not secrets.compare_digest(
        x_internal_token, settings.internal_token
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="内部接口鉴权失败")


# ==================== 主入口 ====================


@router.post("/ask", response_model=AgentResponse, dependencies=[Depends(require_internal_token)])
async def ask(req: AskRequest, request: Request) -> AgentResponse:
    """执行一轮问答。

    ⚠️ **仍然是 P2 的桩**：`message` 是**模板拼的**，不调 LLM —— 那是 P3。
    但**数据是真的**：这里确实去调了 Java 的工具接口。

    工具选择用的是**规则**（有没有锚点），不是意图判定。这样 P3 接 LLM 时，
    改动面只有「从 focus 判断」变成「问 LLM」，后面的执行逻辑完全不动。
    """
    client: JavaClient = request.app.state.java_client
    started = time.perf_counter()

    if req.focus is not None:
        # 有锚点 → 顺着这个类往下看
        label = f"call_chain({req.focus.uid}, DOWN, depth={DEFAULT_CHAIN_DEPTH})"
        explain = _explain_chain
        pending = client.call_chain(req.project_id, req.focus.uid, direction=ChainDirection.DOWN)
    else:
        # 没锚点 → 只能问「这项目是干嘛的」
        label = "overview()"
        explain = _explain_overview
        pending = client.overview(req.project_id)

    # 降级只接运输错误。**InternalAuthError 故意不接** —— 两边 token 对不上是配置错误，
    # 必须炸成 500，绝不能被当成「工具暂时答不了」而静默降级（那会把配置问题藏起来）。
    try:
        env = await pending
    except ToolTransportError as e:
        logger.warning("工具不可用，降级答复: %s", e)
        return _degraded(label, started)

    elapsed = _ms(started)
    step = Step(n=1, type=StepType.TOOL, detail=label, ms=elapsed)

    # ok:false 是**业务结果**（类不存在、项目未分析…），不是异常 —— 原样说给用户
    if not env.ok:
        return AgentResponse(
            conversation_id=req.conversation_id,
            message=f"【P2 桩】这个我答不了：{env.note or '工具没有给出原因'}",
            steps=[step],
            meta=Meta(intent="P2_STUB", confidence=0.0, graph_rev=env.graph_rev,
                      latency_ms=elapsed),
            debug_tool_results=[env.model_dump(by_alias=True)],
        )

    citations, message = explain(env, _evidenced_uids(env))

    return AgentResponse(
        conversation_id=req.conversation_id,
        message=message,
        citations=citations,
        steps=[step],
        meta=Meta(intent="P2_STUB", confidence=0.0, graph_rev=env.graph_rev, latency_ms=elapsed),
        # ⚠️ P2 临时字段，P3 删。by_alias=True 是必须的 ——
        # 否则 dump 出来是 snake_case，和 Java 实际发的对不上，就没法对拍了
        debug_tool_results=[env.model_dump(by_alias=True)],
    )


@router.post("/admin/evict", dependencies=[Depends(require_internal_token)])
async def evict(req: EvictRequest) -> dict:
    """项目删除时清 Context。**P4 才有实现** —— P2 的桩是无状态的，没有 Context 可清。"""
    return {"ok": True, "note": f"P2 无状态，projectId={req.project_id} 无 Context 需清理"}


# ==================== 讲解：把工具结果拼成人话 ====================


def _evidenced_uids(env: ToolEnvelope) -> set[str]:
    """本轮 evidence 里出现过的 uid —— **citations 只能从这里取**。

    evidence 是 Java 从图里捞出来的真实凭据，不是我们写死的。
    """
    uids: set[str] = set()
    for e in env.evidence:
        if e.from_:
            uids.add(e.from_)
        if e.to:
            uids.add(e.to)
    return uids


def _explain_chain(
    env: ToolEnvelope[CallChain], evidenced: set[str]
) -> tuple[list[Citation], str]:
    """★ 先选出 citations，再用 citations 拼 message —— 顺序不能反。

    反过来做就成了「先写讲解、再回头找出处」，那样必然出现引用了 evidence 之外的东西，
    只能靠事后校验去补。**这样反过来，「引用没出处」结构上不可能发生**，
    自检退化成一句断言。这正是 9/19 §8-A2「让禁止编造从 prompt 口号变成可校验机制」的落地。
    """
    assert env.data is not None
    data = env.data

    # 只保留在 evidence 里出现过的节点；不在的说明 Java 侧不一致，记 error 并从讲解里剔除
    nodes = [n for n in data.nodes if n.uid in evidenced]
    if len(nodes) != len(data.nodes):
        logger.error(
            "call-chain 有 %d 个节点不在 evidence 里（Java 侧不一致），已剔除",
            len(data.nodes) - len(nodes),
        )

    citations = [Citation(uid=n.uid, kind=NodeRefType.CLASS) for n in nodes]
    cited = {c.uid for c in citations}
    assert cited <= evidenced, "citations 必须全部来自 evidence"

    start_name = _simple(data.start.qualified_name)
    impls = [_simple(n.uid) for n in nodes if n.via_fanout]
    downstream = [n for n in nodes if n.depth > 0]

    head = start_name + (f"（接口，实现类 {'、'.join(impls)}）" if impls else "")

    if downstream:
        # 按模块分组 —— 「跨没跨模块」正是调用链最该讲出来的一件事
        by_module: dict[str, list[str]] = defaultdict(list)
        for n in downstream:
            by_module[n.module or "（模块未知）"].append(_simple(n.uid))
        groups = "；".join(f"{m}: {'、'.join(v)}" for m, v in by_module.items())
        body = f"{head} 往下调用了 {len(downstream)} 个类 —— {groups}。"
    else:
        body = f"{head} 下面没有解析到调用。"

    # ★ gaps 必须提及：把「跨服务调用」讲成「没找到」，等于丢掉了微服务项目里最有价值的信息
    remote_gaps = [g for g in data.gaps if g.reason is GapReason.REMOTE_SERVICE]
    if remote_gaps:
        named = [_simple(g.at) for g in remote_gaps if g.at in cited]
        suffix = f"（{'、'.join(named)}）" if named else ""
        body += f" 注意：{len(remote_gaps)} 处调用跑到别的服务去了{suffix}。"
    others = [g for g in data.gaps if g.reason is not GapReason.REMOTE_SERVICE]
    if others:
        body += f" 注意：另有 {len(others)} 处未解析。"

    # ★ truncated 必须点破：不说的话，「只看到 3 个」会被读成「只有 3 个」
    if data.truncated:
        body += " 注意：结果被截断，以上不是全部。"

    return citations, f"【P2 桩·模板拼】{body}"


def _explain_overview(
    env: ToolEnvelope[ProjectOverview], evidenced: set[str]
) -> tuple[list[Citation], str]:
    """项目概况。

    这里 citations **必然是空的**：数据来自 `project_overview` 表而不是图，
    Java 侧的 evidence 也是空的 —— 没有「图上的某个节点」可引用。
    这是诚实的空，不是漏填。
    """
    assert env.data is not None
    d = env.data

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

    return [], f"【P2 桩·模板拼】{body}"


# ==================== 小工具 ====================


def _simple(uid: str) -> str:
    """全限定名 → 短名。methodUid 形如 `a.B#m(...)`，先把 `#` 之后砍掉。"""
    return uid.split("#", 1)[0].rsplit(".", 1)[-1]


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _degraded(label: str, started: float) -> AgentResponse:
    """工具连不上时的最小降级 —— 给一句诚实话，而不是 500。"""
    elapsed = _ms(started)
    return AgentResponse(
        message=f"【P2 桩】工具 {label} 暂时连不上（Java 后端不可用），这轮没取到数据。",
        steps=[Step(n=1, type=StepType.TOOL, detail=label, ms=elapsed)],
        meta=Meta(intent="P2_STUB", confidence=0.0, graph_rev=0, latency_ms=elapsed),
    )
