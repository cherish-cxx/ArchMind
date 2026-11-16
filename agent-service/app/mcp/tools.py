"""MCP 只读工具：项目列表与项目概况。

两个工具都遵守同一条约定：**预期失败返回 `{"ok": false, "error": ...}`**，
不抛 MCP 协议错误；意外异常照常上抛（那是 bug，不该被包装成业务失败）。

`projectId` 永远当作不可信输入：归属校验由 Java 的 `/internal/mcp/context`
完成，这里只负责把 403/404 翻译成工具错误码。
"""

from app.mcp import deps
from app.mcp.format import index_meta, meta, tool_error
from app.mcp.principal import McpAuthError, ProjectContext, current_principal


class McpToolError(Exception):
    """工具层可预期的失败。携带对外错误码与提示。"""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


async def ensure_project(project_id: int) -> ProjectContext:
    """校验当前 token 对该项目的归属，返回项目上下文。

    403 → not_authorized、404 → not_found；其余状态码（401/5xx）不在这里
    翻译 —— 那是鉴权/服务问题，继续上抛由中间件或调用方处理。
    """
    try:
        return await deps.services().context.context(current_principal(), project_id)
    except McpAuthError as e:
        if e.status == 403:
            raise McpToolError(
                "not_authorized",
                e.message or "无权访问该项目",
                "请确认该项目属于当前 token 的用户",
            ) from e
        if e.status == 404:
            raise McpToolError(
                "not_found",
                e.message or "项目不存在",
                "请先用 archmind_list_projects 确认 projectId",
            ) from e
        raise


async def archmind_list_projects() -> dict:
    """列出当前 PAT 用户的项目，并附带每个项目的索引状态。"""
    try:
        principal = current_principal()
        svc = deps.services()
        items = await svc.context.projects(principal.user_id)
        for item in items:
            state = await svc.index_store.get_state(item["projectId"])
            # 保留 Java 的 camelCase 键；只追加 index 字段。
            item["index"] = index_meta(state)
        return {"ok": True, "data": items}
    except McpToolError as e:
        return tool_error(e.code, e.message, e.hint)


async def archmind_overview(projectId: int) -> dict:
    """项目概况（分析阶段生成）。未分析完的项目返回 tool_failed。"""
    try:
        ctx = await ensure_project(projectId)
        env = await deps.services().java.overview(projectId)
        if not env.ok:
            return tool_error(
                "tool_failed",
                env.note or "项目概况暂不可用",
                "该项目可能尚未完成分析",
            )
        return {
            "ok": True,
            "data": env.data.model_dump(by_alias=True),
            "meta": meta(ctx, truncated=env.truncated),
        }
    except McpToolError as e:
        return tool_error(e.code, e.message, e.hint)
