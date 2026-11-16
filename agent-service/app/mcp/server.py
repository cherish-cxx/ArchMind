"""FastMCP 实例。Task 5 只搭骨架，工具在 Task 6+ 注册。"""

from mcp.server.fastmcp import FastMCP


def build_mcp() -> FastMCP:
    return FastMCP("archmind")
