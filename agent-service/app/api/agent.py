"""Python 侧对外暴露的接口（Java → Python）。

Python **不是**给前端直接调的 —— 它是 Java 后面的内部服务。
所以这里和 Java 的 `/internal/**` 一样：只认共享密钥，不认用户 JWT。

**路由层有意保持很薄**：取 Context → 交给 `core/loop.run_turn` → 返回。
所有分支都不在这里 —— 编排在 loop，判定在 core/intent，讲解在 core/explain。
路由里如果出现 `if`，多半是编排漏到了这一层。
"""

import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.core.llm import LLMClient
from app.core.loop import run_turn
from app.models.context import AgentContext
from app.models.protocol import AskRequest, EvictRequest
from app.models.response import AgentResponse
from app.tools.java_client import JavaClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/agent", tags=["agent"])


# ==================== 依赖注入 ====================
#
# 为什么要 Depends 而不是在路由里直接 `request.app.state.java_client`：
# 那样测试就只能拿到真的 JavaClient 和真的 LLMClient，于是「低置信度会不会误调工具」
# 这类分支根本没法测。改成 Depends 之后，测试用 `app.dependency_overrides` 塞假对象进去。
#
# 也就是说：这两个函数的存在，是 P3 那一整层单元测试的前提。


def get_java_client(request: Request) -> JavaClient:
    return request.app.state.java_client


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm_client


# ==================== 会话 Context（P3：内存版）====================
#
# ⚠️ **P3 的 Context 只活在进程内存里**，进程一重启就没了。
# 这是有意的：Redis 持久化（db1，TTL 2h）是 P4 的事，届时只需把这两个函数换成
# `store/context_store.py` 的读写，**模型与循环一行都不用改**。
#
# key 用 (projectId, conversationId)：projectId 参与做 key 是防御性的 ——
# 万一 Java 侧串了 projectId，也不会让两个项目的对话互相污染 Context。

_CONTEXTS: dict[tuple[int, str], AgentContext] = {}


def _new_conversation_id() -> str:
    return f"c_{uuid.uuid4().hex[:16]}"


def _load_context(project_id: int, conversation_id: str) -> AgentContext:
    key = (project_id, conversation_id)
    ctx = _CONTEXTS.get(key)
    if ctx is None:
        ctx = AgentContext(conversation_id=conversation_id, project_id=project_id)
        _CONTEXTS[key] = ctx
        logger.info("新建 Context projectId=%s conversationId=%s", project_id, conversation_id)
    return ctx


def _apply_focus(ctx: AgentContext, focus) -> None:
    """把请求里的锚点应用到这个会话上。

    焦点变化时**旧的压栈** —— 这正是「回到上一个」能工作的原因（设计 §7 决定④：栈不是数组）。
    同一个类重复点是日常操作（用户点了又点），不该往栈里塞重复项。
    """
    if focus is None:
        return
    if ctx.focus is not None and ctx.focus.uid == focus.uid:
        return
    if ctx.focus is not None:
        ctx.focus_stack.append(ctx.focus.uid)
    ctx.focus = focus


# ==================== 鉴权 ====================


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
async def ask(
    req: AskRequest,
    java: JavaClient = Depends(get_java_client),
    llm: LLMClient = Depends(get_llm_client),
) -> AgentResponse:
    """执行一轮问答。

    P3 起 `message` 由 LLM 生成（不再是 P2 的模板桩），
    但**讲解不可用时会自动退化成 P2 那套模板**（设计 §8-C2），不会整轮失败。
    """
    conversation_id = req.conversation_id or _new_conversation_id()
    ctx = _load_context(req.project_id, conversation_id)
    _apply_focus(ctx, req.focus)

    response = await run_turn(ctx, req.message, java, llm)
    # loop 不知道 conversation_id 是新生成的还是回传的（那是路由层的事），所以在这里补上
    response.conversation_id = conversation_id
    return response


@router.post("/admin/evict", dependencies=[Depends(require_internal_token)])
async def evict(req: EvictRequest) -> dict:
    """项目删除时清掉它的全部会话 Context。

    P3 的 Context 在内存里，所以这里能真正清干净；
    P4 换成 Redis 后，这里改成按 key 前缀删 —— 接口形状不变。
    """
    doomed = [k for k in _CONTEXTS if k[0] == req.project_id]
    for k in doomed:
        del _CONTEXTS[k]
    logger.info("清掉 projectId=%s 的 %d 个 Context", req.project_id, len(doomed))
    return {"ok": True, "note": f"已清理 {len(doomed)} 个会话 Context"}
