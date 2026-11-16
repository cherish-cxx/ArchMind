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
    # 必须在创建 app 之前设置：路由是在 streamable_http_app() 里按这个值建的。
    mcp.settings.streamable_http_path = "/"
    inner = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with inner.router.lifespan_context(inner):
            yield

    svc = deps.configure(services)
    routes = [
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=PatAuthMiddleware(inner, svc.context, svc.limiter)),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


app = create_app()
