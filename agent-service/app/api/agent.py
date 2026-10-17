"""Python 侧对外暴露的接口（Java → Python）。

Python **不是**给前端直接调的 —— 它是 Java 后面的内部服务。
所以这里和 Java 的 `/internal/**` 一样：只认共享密钥，不认用户 JWT。

**路由层有意保持很薄**：取 Context → 交给 `core/loop.run_turn` → 返回。
所有分支都不在这里 —— 编排在 loop，判定在 core/intent，讲解在 core/explain。
路由里如果出现 `if`，多半是编排漏到了这一层。
"""

import secrets
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.core.llm import LLMClient
from app.core.loop import navigate as apply_navigation
from app.core.loop import run_turn
from app.models.context import AgentContext, Viewport
from app.models.protocol import (
    AskRequest,
    EvictRequest,
    NavigateOp,
    NavigateRequest,
    ViewportReport,
)
from app.models.response import AgentResponse, Meta
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
    if req.conversation_id is None:
        # 没带 cid = 新会话。这是**唯一**该走「创建」的路径。
        ctx = AgentContext(conversation_id=conversation_id, project_id=req.project_id)
    else:
        # 带了 cid，就去 Redis 取那份 Context。
        ctx = await store.load_or_none(req.project_id, conversation_id)
        if ctx is None:
            # 带了 cid 却找不到 → **会话已过期**（TTL 2h 是既定语义，必然发生）。
            # 判据是「有没有带 cid」，不是「上下文在不在」——
            # cid 是后端发的，带了就说明这是一个**曾经存在**的会话。
            # **不静默开一个新的**：那样用户会在上文丢失的情况下收到一个看似正常的答案。
            return _session_expired(conversation_id)
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


def _session_expired(conversation_id: str) -> AgentResponse:
    """带了 cid 但 Context 已不在 —— 会话过期。**如实说，绝不悄悄重开一个。**

    它和 `store.load_or_none` / viewport 的拒绝是**同一件事的三个出口**，
    所以 reason 统一用 `SESSION_GONE`，前端只用认这一个值。
    """
    return AgentResponse(
        conversation_id=conversation_id,
        message="这段对话已经过期（超过 2 小时没有活动），内容不再保留。请开始新的对话。",
        session_expired=True,
        # intent 是个裸 str（不是那六个的封闭枚举）：这里**根本没有 LLM 参与**，
        # 是路由层直接拒绝的。那条「封闭枚举」规则约束的是 **LLM 不许发明意图**，
        # 不是不许路由层表达自己的状态。
        meta=Meta(intent="SESSION_GONE", confidence=1.0, graph_rev=0, latency_ms=0),
    )


# ==================== 零 LLM 导航（P4.3）====================
#
# 这条路是「前端**点按钮**，直接声明意图」—— 设计 §3.3 的决策，
# 目的只有一个：**点击变成零 token**（E2：用户点一下展开就烧一次 token 是要避免的）。
#
# ⚠️ `/ask` 那条路的 NAVIGATE 判定**保留**（loop.py 的 `run_turn` 里那个分支不动）——
#    用户完全可能在输入框里**打字**说「退回去」，那就还是得靠 LLM #1 判出来。
#    两条路殊途同归到 `loop.navigate` 这一个函数。


@router.post("/navigate", response_model=AgentResponse,
             dependencies=[Depends(require_internal_token)])
async def navigate(
    req: NavigateRequest,
    store: ContextStore = Depends(get_context_store),
) -> AgentResponse:
    """导航：只动焦点，**一次 LLM 都不调**。

    注意这里**没有 `llm` 依赖** —— 这不是省事，是**物理上保证**它调不了 LLM：
    参数里没有 llm，就没有误调的可能。测「零 LLM」时也就不需要注入假 LLM。
    """
    if req.op is not NavigateOp.BACK:
        # EXPAND 的语义在 loop 里**从来不存在**（navigate() 只做出栈/清空）。
        # 如实回一句「还没做」，**不静默当成 BACK 执行** —— 那会让用户以为展开成功了。
        return AgentResponse(
            conversation_id=req.conversation_id,
            message="「展开」这个操作还没做，目前只支持「退回去」。",
            meta=Meta(intent="NAVIGATE_UNSUPPORTED", confidence=1.0,
                      graph_rev=0, latency_ms=0),
        )

    # 同 /viewport：用 load_or_none **不用 load**。
    # 同一个 `raw is None`，在 /ask 是「新会话」，在这里是「**会话已过期**」——
    # 导航针对的是一个已存在的会话（栈在它的 Context 里），不建空的。
    ctx = await store.load_or_none(req.project_id, req.conversation_id)
    if ctx is None:
        return _session_expired(req.conversation_id)

    started = time.perf_counter()
    # steps 传空列表、started 现取：这条路没有 INTENT 步（没判意图），
    # 计时也只从这一刻算 —— 那些都是 /ask 那条路才有的东西。
    resp = apply_navigation(ctx, [], started)

    # ★ 必须 save：apply_navigation 的副作用是 pop 焦点栈 + 改 ctx.focus，
    #   不存的话这次出栈就丢了，下次「退回去」会退到同一个地方。
    await store.save(ctx)
    resp.conversation_id = req.conversation_id
    return resp


# ==================== 屏幕状态上报 + 会话读回（P4.2）====================
#
# 这两条是**反向通道**：上面的 ask / evict 是「Java 让 Python 干活」，
# 这两条是「前端告诉后端屏幕上发生了什么」和「前端把会话要回去」。
#
# ⚠️ 它们用 `store.load_or_none`，而不是 `store.load`。语义差别见那两处注释：
#    在 `/ask` 上「会话不存在」是**新会话**，在这里是**会话已过期**。
#    同一个 `raw is None`，含义完全不同 —— 所以要用两个方法，而不是一个带开关的。


@router.post("/viewport", dependencies=[Depends(require_internal_token)])
async def viewport(req: ViewportReport, store: ContextStore = Depends(get_context_store)) -> dict:
    """前端上报屏幕状态。后端**只接受比当前更大的 rev**，快照直接覆盖。

    **不创建会话** —— 理由见 `ViewportReport` 的类注释。

    响应里**永远回带当前 rev**：前端据此跳到最新。
    没有它，前端被拒之后只会一直重发自己那个旧 rev，永远同步不上。
    """
    ctx = await store.load_or_none(req.project_id, req.conversation_id)
    if ctx is None:
        # 会话已过期。**不建** —— 见上面那段「同一个 raw is None」。
        return {"accepted": False, "reason": "SESSION_GONE", "rev": None}

    if req.rev <= ctx.viewport.rev:
        # 旧快照：乱序到达（网络重传 / 用户开了两个标签页）。
        return {"accepted": False, "reason": "STALE_REV", "rev": ctx.viewport.rev}

    # 截断（硬上限 60）在模型里做 —— 前端的报告是**不可信输入**，它说「我没截断」不算数。
    ctx.viewport = Viewport.from_report(
        rev=req.rev,
        visible_nodes=req.visible_nodes,
        visible_relations=req.visible_relations,
        focused_node=req.focused_node,
    )
    await store.save(ctx)
    return {"accepted": True, "reason": None, "rev": ctx.viewport.rev}


@router.get("/conversations/{conversation_id}", dependencies=[Depends(require_internal_token)])
async def get_conversation(
    conversation_id: str,
    project_id: int = Query(alias="projectId"),
    store: ContextStore = Depends(get_context_store),
) -> dict:
    """读回会话 —— **刷新页面时恢复屏幕用**。

    只给恢复屏幕必需的**两样**：

      - `viewport`：照它把图重画出来（快照驱动，不重放任何命令）
      - `focus`：当前锚点在哪

    **不给 `history`** —— 那是 Agent 的内部记忆（而且存的是摘要），
    前端恢复屏幕用不上，给了只是白白扩大接口面。

    `project_id` 走 query 参数（由 Java 注入），和 body 里那个字段同义。
    会话不存在时 `exists: false`，前端据此把那个会话窗口置灰。
    """
    ctx = await store.load_or_none(project_id, conversation_id)
    if ctx is None:
        return {"exists": False, "reason": "SESSION_GONE"}
    return {
        "exists": True,
        "reason": None,
        # mode="json"：显式转成 JSON 安全的原始类型（枚举 → 字符串）
        # by_alias：显式写死，理由同 store.save 那处
        "viewport": ctx.viewport.model_dump(mode="json", by_alias=True),
        "focus": (ctx.focus.model_dump(mode="json", by_alias=True)
                  if ctx.focus is not None else None),
    }


@router.post("/admin/evict", dependencies=[Depends(require_internal_token)])
async def evict(req: EvictRequest, store: ContextStore = Depends(get_context_store)) -> dict:
    """项目删除时清掉它的全部会话 Context。

    P4.1 起走 Redis 前缀删（`store.evict_project`）。
    **响应形状与 P3 完全一致** —— 这样 P4.3 的 Java 网关不用跟着改。
    """
    n = await store.evict_project(req.project_id)
    return {"ok": True, "note": f"已清理 {n} 个会话 Context"}
