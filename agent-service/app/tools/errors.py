"""工具失败的统一形状（R6）。

这里只定义“错误长什么样、怎么变成一段给模型读的话”，不决定何时返回。
真正的转换点在后续的统一执行入口：它把参数错误、前置条件错误和传输错误
收敛成同一种结构化结果，作为一条 `role=tool` 的消息回喂。

**认证失败不在其中**：`InternalAuthError` 是配置错误，必须炸出来，
转成业务失败会被静默吞掉。
"""

import json
from enum import StrEnum


class ErrorReason(StrEnum):
    """失败原因。调用方按它分支，所以取值是固定集合而不是自由文本。"""

    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    MISSING_ANCHOR = "missing_anchor"
    TRANSPORT = "transport"


class ToolError(Exception):
    """一次工具调用的失败。`field` 只在参数类错误里有值。"""

    def __init__(
        self, reason: ErrorReason, hint: str, *, field: str | None = None
    ) -> None:
        super().__init__(hint)
        self.reason = reason
        self.hint = hint
        self.field = field

    @classmethod
    def invalid_arguments(cls, *, field: str, hint: str) -> "ToolError":
        return cls(ErrorReason.INVALID_ARGUMENTS, hint, field=field)

    @classmethod
    def unknown_tool(cls, name: str, available: list[str]) -> "ToolError":
        return cls(
            ErrorReason.UNKNOWN_TOOL,
            f"没有名为 {name} 的工具；可用工具：{', '.join(available)}",
            field="tool",
        )

    @classmethod
    def missing_anchor(cls, tool: str) -> "ToolError":
        return cls(
            ErrorReason.MISSING_ANCHOR,
            f"{tool} 需要一个类或方法作为起点，请先选择目标或换用不需要锚点的工具",
            field="uid",
        )

    def to_payload(self) -> dict[str, str | bool]:
        payload: dict[str, str | bool] = {
            "ok": False,
            "reason": self.reason.value,
            "hint": self.hint,
        }
        if self.field is not None:
            payload["field"] = self.field
        return payload

    def render(self) -> str:
        """渲染成 tool 消息的 content。模型要能直接读懂并改参数。"""
        return json.dumps(self.to_payload(), ensure_ascii=False)


def to_tool_message(err: ToolError) -> str:
    return err.render()
