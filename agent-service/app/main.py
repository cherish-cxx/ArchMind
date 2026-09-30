from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.config import settings
from app.tools.java_client import JavaClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """建一次 JavaClient，整个服务共用它的连接池；关停时释放。

    在这里建而不是在每个路由里建：连接复用和连接池的并发上限都是**实例级**的，
    每次调用新建就等于两条都放弃了。另外 token 只在建实例时设一次，不会漏带。
    """
    app.state.java_client = JavaClient()
    yield
    await app.state.java_client.aclose()


app = FastAPI(title="ArchMind Agent Service", version="0.1.0", lifespan=lifespan)
app.include_router(agent_router)


@app.get("/health")
def health() -> dict:
    """存活探针。P6 容器化后 compose 靠它判断服务起没起。"""
    return {"status": "ok", "javaBaseUrl": settings.java_base_url}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.agent_port, reload=True)
