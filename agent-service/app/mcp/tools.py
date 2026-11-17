"""MCP 只读工具：项目列表、项目概况与语义检索。

所有工具都遵守同一条约定：**预期失败返回 `{"ok": false, "error": ...}`**，
不抛 MCP 协议错误；意外异常照常上抛（那是 bug，不该被包装成业务失败）。

`projectId` 永远当作不可信输入：归属校验由 Java 的 `/internal/mcp/context`
完成，这里只负责把 403/404 翻译成工具错误码。
"""

from app.mcp import deps
from app.mcp.format import index_meta, meta, tool_error
from app.mcp.principal import McpAuthError, ProjectContext, current_principal
from app.models.base import CamelModel

#: 检索结果为空时的提示。要指向「去 ArchMind 建索引」这个可执行动作。
_NOT_INDEXED_HINT = "该项目尚未建立索引或索引不可用，请先在 ArchMind 完成分析并建索引"


class SearchCandidate(CamelModel):
    """对外暴露的语义检索候选。

    只给 agent 需要的字段，不把 `rev` 之类内部字段带出去；`score` 是 RRF
    融合分，**只能排序**，不设阈值。`methods` 用于消歧，不是证据。
    """

    uid: str
    simple_name: str
    package_name: str
    kind: str
    description: str | None = None
    score: float
    methods: list[str] = []


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


async def _index_state_for_meta(
    index_store, project_id: int, partial: str | None
) -> dict:
    """给 meta 读一次索引状态。读失败退化成 absent，不拖垮检索本身。

    `partial` 来自检索（`103/104`）；状态里没带 actual/expected 时也要能
    让人看到覆盖不全，所以显式补上。
    """
    try:
        state = await index_store.get_state(project_id)
    except Exception:
        state = None
    index = index_meta(state)
    if partial:
        index["coverage"] = partial
    return index


async def archmind_search_entities(
    projectId: int, query: str, k: int = 10
) -> dict:
    """语义检索项目内的类，返回按相关度排序的候选。

    **空结果不是错误**：项目有索引、查询成功、只是没命中，返回 `ok=true, data=[]`。
    真正该报错的是「索引不可用」——那种情况下空列表会被误读成「项目里没有」，
    所以返回 `not_indexed` 并带上 reason 与处理提示。
    """
    try:
        if not query or not query.strip():
            return tool_error("invalid_argument", "query 不能为空")
        if not isinstance(k, int) or k < 1 or k > 50:
            return tool_error("invalid_argument", "k 必须在 1..50")

        ctx = await ensure_project(projectId)
        svc = deps.services()
        retrieval = await svc.retriever.search(projectId, query, k)

        if not retrieval.candidates:
            if not retrieval.attempted:
                # 索引缺失/未就绪，或 embed 失败 —— 都不是「没命中」。
                return tool_error(
                    "not_indexed",
                    retrieval.reason or "index_unavailable",
                    _NOT_INDEXED_HINT,
                )
            if retrieval.reason:
                # 真发过查询但 Qdrant 失败了：同样不能伪装成「没命中」。
                return tool_error(
                    "tool_failed",
                    retrieval.reason,
                    "检索服务暂不可用，请稍后重试",
                )

        index = await _index_state_for_meta(
            svc.index_store, projectId, retrieval.partial
        )
        data = [
            SearchCandidate(
                uid=c.uid,
                simple_name=c.simple_name,
                package_name=c.package_name,
                kind=c.kind,
                description=c.description,
                score=c.score,
                methods=list(c.methods),
            ).model_dump(by_alias=True)
            for c in retrieval.candidates
        ]
        return {"ok": True, "data": data, "meta": meta(ctx, index=index)}
    except McpToolError as e:
        return tool_error(e.code, e.message, e.hint)
