"""MCP 进程的 ASGI 入口：`/health` + 受 PAT 保护的 `/mcp`。

Streamable HTTP 的 session manager 靠**内层 Starlette 的 lifespan** 启动。
Mount 不会自动跑子应用 lifespan（Starlette 不传播），所以这里显式转发一次；
否则真正用 uvicorn 起服务时，/mcp 会在第一次会话时失败。
"""

import contextlib

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.mcp import deps
from app.mcp.auth import PatAuthMiddleware
from app.mcp.server import build_mcp


async def health(request):
    return JSONResponse({"status": "ok", "service": "archmind-mcp"})


def create_app(services=None) -> Starlette:
    mcp = build_mcp()
    # 保留 SDK 默认的 `/mcp`：若改成 "/" 再挂到 /mcp，外部请求 /mcp 会被
    # Starlette 的尾斜杠规则 307 到 /mcp/ —— 重定向发生在鉴权之前。
    inner = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with inner.router.lifespan_context(inner):
            yield

    svc = deps.configure(services)
    routes = [
        Route("/health", health, methods=["GET"]),
        # 内层已经有 `/mcp` 路由，所以挂在 "/"；放在 /health 之后，
        # 让健康检查不被根挂载吞掉。
        Mount("/", app=PatAuthMiddleware(inner, svc.context, svc.limiter)),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


app = create_app()
