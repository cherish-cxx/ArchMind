"""FastMCP 实例。

每次都构造新的 `FastMCP`：`StreamableHTTPSessionManager.run()` 在 SDK 里
**只能用一次**，进程内共享实例会让第二个 ASGI app 的 lifespan 直接报
`RuntimeError`。工具在这里显式注册。
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp.tools import archmind_list_projects, archmind_overview


def build_mcp() -> FastMCP:
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

    return mcp
