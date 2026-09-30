"""Python 侧对外暴露的接口（Java → Python）。

Python **不是**给前端直接调的 —— 它是 Java 后面的内部服务。
所以这里和 Java 的 `/internal/**` 一样：只认共享密钥，不认用户 JWT。
"""

import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.models.protocol import AskRequest, EvictRequest
from app.models.response import AgentResponse, Meta

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


@router.post("/ask", response_model=AgentResponse, dependencies=[Depends(require_internal_token)])
async def ask(req: AskRequest) -> AgentResponse:
    """执行一轮问答。

    ⚠️ **P2 是桩**：形状用最终的 `AgentResponse`，但**不调工具、不调 LLM**。
    T4 在这里接上 overview + call-chain；P3 再把 `message` 的来源换成 LLM。
    """
    started = time.perf_counter()

    focus_desc = f"{req.focus.type} {req.focus.uid}" if req.focus else "无锚点"
    message = (
        f"【P2 桩】收到 message={req.message!r}、focus={focus_desc}。"
        "工具尚未接线（T4 接 overview + call-chain）。"
    )

    return AgentResponse(
        conversation_id=req.conversation_id,
        message=message,
        meta=Meta(
            intent="P2_STUB",
            confidence=0.0,
            graph_rev=0,   # 没调工具拿不到版本号；0 = 未知，是合法值不是错误
            latency_ms=int((time.perf_counter() - started) * 1000),
        ),
    )


@router.post("/admin/evict", dependencies=[Depends(require_internal_token)])
async def evict(req: EvictRequest) -> dict:
    """项目删除时清 Context。**P4 才有实现** —— P2 的桩是无状态的，没有 Context 可清。"""
    return {"ok": True, "note": f"P2 无状态，projectId={req.project_id} 无 Context 需清理"}
