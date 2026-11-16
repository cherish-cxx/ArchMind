"""FastMCP 实例。

工具在这里显式注册。实例**缓存复用**：反复调 `build_mcp()` 返回同一个
实例，注册只发生一次 —— 否则重复注册会让 FastMCP 覆盖同名工具并打警告。

一个例外：`StreamableHTTPSessionManager.run()` 在 SDK 里**只能用一次**
（进程内同一个 manager 不能跑第二个 lifespan）。测试会为每个 TestClient
建一个新的 ASGI app；此时必须换一个新 FastMCP，否则第二个 app 启动即炸。
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp.tools import archmind_list_projects, archmind_overview

_mcp: FastMCP | None = None


def _already_started(mcp: FastMCP) -> bool:
    """缓存实例的 session manager 是否已经跑过 lifespan。

    `session_manager` 在 `streamable_http_app()` 之前访问会抛 RuntimeError，
    那说明实例还没被任何 app 用过，可以安全复用。
    """
    try:
        manager = mcp.session_manager
    except RuntimeError:
        return False
    return bool(getattr(manager, "_has_started", False))


def build_mcp() -> FastMCP:
    global _mcp
    if _mcp is not None and not _already_started(_mcp):
        return _mcp

    # 服务挂在反向代理后面，`Host` 是外部域名。FastMCP 对 127.0.0.1 默认开启
    # DNS rebinding 校验，会把这类请求在鉴权之前直接 421 掉。
    # `/mcp` 已由 PAT 中间件把关，这里关掉 Host/Origin 校验。
    mcp = FastMCP(
        "archmind",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    mcp.tool(name="archmind_list_projects")(archmind_list_projects)
    mcp.tool(name="archmind_overview")(archmind_overview)

    _mcp = mcp
    return mcp
