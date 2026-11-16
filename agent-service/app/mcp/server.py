"""FastMCP 实例。Task 5 只搭骨架，工具在 Task 6+ 注册。"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def build_mcp() -> FastMCP:
    # 服务挂在反向代理后面，`Host` 是外部域名。FastMCP 对 127.0.0.1 默认开启
    # DNS rebinding 校验，会把这类请求在鉴权之前直接 421 掉。
    # `/mcp` 已由 PAT 中间件把关，这里关掉 Host/Origin 校验。
    return FastMCP(
        "archmind",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
