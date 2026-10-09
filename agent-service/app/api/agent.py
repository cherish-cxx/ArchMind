"""Python 侧对外暴露的接口（Java → Python）。

Python **不是**给前端直接调的 —— 它是 Java 后面的内部服务。
所以这里和 Java 的 `/internal/**` 一样：只认共享密钥，不认用户 JWT。

**路由层有意保持很薄**：取 Context → 交给 `core/loop.run_turn` → 返回。
所有分支都不在这里 —— 编排在 loop，判定在 core/intent，讲解在 core/explain。
路由里如果出现 `if`，多半是编排漏到了这一层。
"""

import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.core.llm import LLMClient
from app.core.loop import run_turn
from app.models.context import AgentContext
from app.models.protocol import AskRequest, EvictRequest
from app.models.response import AgentResponse
from app.store.context_store import ContextStore
from app.tools.java_client import JavaClient


# 新建一个子路由“收纳盒”，何止里所有接口自动带上前缀internal/agent，在接口文档中归类到agent分组
# prefix="/internal/agent" 使得所有接口能够自动拼装上该路径
# tags=["agent"]只作用于swagger文档，把这一组接口归到名字叫agent的折叠分组里
router = APIRouter(prefix="/internal/agent", tags=["agent"])


# ==================== 依赖注入 ====================
#
# 为什么要 Depends 而不是在路由里直接 `request.app.state.java_client`：
# 那样测试就只能拿到真的 JavaClient 和真的 LLMClient，于是「低置信度会不会误调工具」
# 这类分支根本没法测。改成 Depends 之后，测试用 `app.dependency_overrides` 塞假对象进去。
#
# 也就是说：这两个函数的存在，是 P3 那一整层单元测试的前提。

# 定义一个Python函数
# request: Request 参数注解，代表传入的request变量类型是request，不是强制校验，只是给IDE和FastAPI看的提示
# `-> JavaClient` / `-> LLMClient`：返回值注解，表示这个函数返回的对象是`JavaClient` / `LLMClient`实例
# .属于访问语法 request.app.state.llm_client 拿request对象里的app属性的实例上的state属性中存的客户端对象
def get_java_client(request: Request) -> JavaClient:
    return request.app.state.java_client


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm_client

def get_context_store(request:Request) ->ContextStore:
    return request.app.state.context_store


# ==================== 会话 Context（P4.1：已搬进 Redis db1）====================
#
# P3 时这里是进程级 dict（`_CONTEXTS`）：重启即丢，多 worker 时各存一份 ——
# 第二轮到别的 worker 就当成新会话，**静默失忆**。P4.1 搬进 `store/context_store.py`。
#
# key 不变：`agent:ctx:{projectId}:{conversationId}`。
# projectId 参与做 key 是防御性的 —— 万一 Java 侧串了 projectId，
# 也不会让两个项目的对话互相污染 Context。
#
# ⚠️ 这里**不再有** `_CONTEXTS` / `_load_context`：存取全走 store，
# 路由层只多 `await store.load(...)` / `await store.save(...)` 两行，
# **run_turn 与 Context 模型一行都没改**。


def _new_conversation_id() -> str:
    return f"c_{uuid.uuid4().hex[:16]}"


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

    用 `compare_digest` 而不是 `==`：定长比较，避免逐字符短路带来的时序侧信道，防止的是利用访问响应时间差异猜密钥
    和 Java 侧 `InternalTokenFilter` 的做法对齐。
    """
    # 判断请求头里是否有token，与配置的钥匙是否一致，失败抛出401，请求直接终止，不会执行后面的ask接口代码
    if x_internal_token is None or not secrets.compare_digest(x_internal_token,settings.internal_token):
        """
        secrets.compare_digest(a,b)：安全的字符串对比
        不管前面字符对不对，固定消耗一样时间
        """
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="内部接口鉴权失败")


# ==================== 主入口 ====================

"""
response_model = agentResponse返回结果自动校验格式，并生成接口文档
dependencies=[Depends(require_internal_token)：进入这个接口函数之前，先执行这个依赖：
    校验内部调用token，token不对直接拒绝请求，进不到下面的代码
"""
@router.post("/ask", response_model=AgentResponse, dependencies=[Depends(require_internal_token)])
async def ask(
    req: AskRequest, #接收前端POST传过来的请求体，自动校验请求参数是否符合AskRequest模型
    java: JavaClient = Depends(get_java_client),#自动执行之前那个依赖函数，拿到全局的java客户端，变量名java
    llm: LLMClient = Depends(get_llm_client),#自动执行一阿里，拿到全局LLM客户端，变量名llm
    store: ContextStore = Depends(get_context_store),
) -> AgentResponse:#返回值类型注解，返回agentResponse对象
    """执行一轮问答。

    P3 起 `message` 由 LLM 生成（不再是 P2 的模板桩），
    但**讲解不可用时会自动退化成 P2 那套模板**（设计 §8-C2），不会整轮失败。
    """
    #看请求里有没有传conversation_id,如果传了，就用传过来的id，否则（or）就调用new，生成全新会话id
    conversation_id = req.conversation_id or _new_conversation_id()
    #加载会话上下文：用 projectId + conversation_id 拼 key 从 Redis 取；取不到就是新会话
    ctx = await store.load(req.project_id,conversation_id)
    # 把请求里的focus更新到会话上下文ctx里面
    _apply_focus(ctx, req.focus)
    """
    执行一轮Agent回答，把上下文，用户消息，java客户端，llm客户端传给run_turn函数
    run_turn内部会调用LLM生成回答，如果LLM调用失败，会自动降级到旧版本P2模板，不会直接崩掉整个请求
    """
    response = await run_turn(ctx, req.message, java, llm)
    await store.save(ctx)
    # loop 不知道 conversation_id 是新生成的还是回传的（那是路由层的事），所以在这里补上
    response.conversation_id = conversation_id
    return response


@router.post("/admin/evict", dependencies=[Depends(require_internal_token)])
async def evict(req: EvictRequest, store: ContextStore = Depends(get_context_store)) -> dict:
    """项目删除时清掉它的全部会话 Context。

    P4.1 起走 Redis 前缀删（`store.evict_project`）。
    **响应形状与 P3 完全一致** —— 这样 P4.3 的 Java 网关不用跟着改。
    """
    n = await store.evict_project(req.project_id)
    return {"ok": True, "note": f"已清理 {n} 个会话 Context"}
