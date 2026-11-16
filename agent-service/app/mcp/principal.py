"""MCP 请求身份与项目上下文。

`Principal` 只活在内存与 contextvar 里：**raw_token 不写日志、不进 repr**。
需要再发请求给 Java 时才从 `raw_token` 取，工具层只读 `user_id` / `pat_id`。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


@dataclass(frozen=True)
class Principal:
    pat_id: int
    user_id: int
    bound_project_id: int | None = None
    expires_at: datetime | None = None
    #: 仅用于后续内部调用；repr=False 防止异常/日志顺手把它带出去。
    raw_token: str = field(default="", repr=False)


@dataclass(frozen=True)
class ProjectContext:
    user_id: int
    pat_id: int
    project_id: int
    project_name: str
    analysis_status: str
    graph_rev: int


class McpAuthError(Exception):
    """鉴权失败。`status` 决定 MCP 层返给客户端的 HTTP 状态码。

    消息必须是**固定的、不含 token** 的文本 —— 它会原样进入错误响应。
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_principal: ContextVar[Principal | None] = ContextVar("archmind_mcp_principal", default=None)


def current_principal() -> Principal | None:
    """当前请求的 Principal；不在请求作用域内返回 None。"""
    return _principal.get()


@contextmanager
def principal_scope(principal: Principal) -> Iterator[Principal]:
    """把 Principal 放进 contextvar，退出时恢复 —— 工具层不必层层传参。"""
    token = _principal.set(principal)
    try:
        yield principal
    finally:
        _principal.reset(token)
