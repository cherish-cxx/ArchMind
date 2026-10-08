from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.config import settings
from app.core.llm import LLMClient
from app.tools.java_client import JavaClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """服务启动时建两个客户端，整个进程共用；关停时释放。

    生命周期分三段，`yield` 就是分界线：
      - yield 之前：初始化资源（这里建连接池）
      - yield 那一刻：把控制权交回给 FastAPI，**开始接受用户请求**
      - yield 之后：服务关停，清理资源

    为什么在这里建而不是在每个路由里建：连接复用和连接池的并发上限都是**实例级**的，
    每次调用新建就等于两条都放弃了。另外 token / api-key 只在建实例时设一次，不会漏带。

    P3 新增 `llm_client` —— 和 java_client 同样的理由，同理同理，挂在同一个地方。
    """
    # 服务器刚启动先跑这里：创建java客户端，llm客户端，并塞到app.state全局储物盒，此时还没有对发开放接口，用户请求进不来
    app.state.java_client = JavaClient()
    app.state.llm_client = LLMClient()
    # 把控制权交给FastAPI，服务正式上线，开始监听端口，接收HTTP请求
    yield
    # 清理代码：异步调用客户端的关闭防范，释放网络连接，资源关闭
    await app.state.java_client.aclose()
    await app.state.llm_client.aclose()


app = FastAPI(title="ArchMind Agent Service", version="0.1.0", lifespan=lifespan)
# 把agent中写的所有接口注册到app里，让主服务能识别这些接口
app.include_router(agent_router)


@app.get("/health")
def health() -> dict:
    """存活探针。P6 容器化后 compose 靠它判断服务起没起。"""
    return {"status": "ok", "javaBaseUrl": settings.java_base_url}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.agent_port, reload=True)
