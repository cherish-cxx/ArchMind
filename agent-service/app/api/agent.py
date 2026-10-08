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


# ==================== 会话 Context（P3：内存版）====================
#
# ⚠️ **P3 的 Context 只活在进程内存里**，进程一重启就没了。
# 这是有意的：Redis 持久化（db1，TTL 2h）是 P4 的事，届时只需把这两个函数换成
# `store/context_store.py` 的读写，**模型与循环一行都不用改**。
#
# key 用 (projectId, conversationId)：projectId 参与做 key 是防御性的 ——
# 万一 Java 侧串了 projectId，也不会让两个项目的对话互相污染 Context。

# 变量名，开头下划线约定表示私有变量，意思是，这个变量只允许当前py文件内部使用，别的文件精良不要直接到哦如修改它
# dict[]类型注解，代表这个是一个字典，Key的类型：tuple[int,str]元组，由证书+字符串组成
# value的类型：agentContext（一个自定义的类实例
# ={}：初始化，创建一个空字典
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
) -> AgentResponse:#返回值类型注解，返回agentResponse对象
    """执行一轮问答。

    P3 起 `message` 由 LLM 生成（不再是 P2 的模板桩），
    但**讲解不可用时会自动退化成 P2 那套模板**（设计 §8-C2），不会整轮失败。
    """
    #看请求里有没有传conversation_id,如果传了，就用传过来的id，否则（or）就调用new，生成全新会话id
    conversation_id = req.conversation_id or _new_conversation_id()
    #加载会话上下文，用projectId➕conversation_id作为key，去前面那个contexts字典里取出agentContext
    ctx = _load_context(req.project_id, conversation_id)
    # 把请求里的focus更新到会话上下文ctx里面
    _apply_focus(ctx, req.focus)

    """
    执行一轮Agent回答，把上下文，用户消息，java客户端，llm客户端传给run_turn函数
    run_turn内部会调用LLM生成回答，如果LLM调用失败，会自动降级到旧版本P2模板，不会直接崩掉整个请求
    """
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
