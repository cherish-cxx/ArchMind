"""MCP 只读工具：项目列表、项目概况与语义检索。

所有工具都遵守同一条约定：**预期失败返回 `{"ok": false, "error": ...}`**，
不抛 MCP 协议错误；意外异常照常上抛（那是 bug，不该被包装成业务失败）。

`projectId` 永远当作不可信输入：归属校验由 Java 的 `/internal/mcp/context`
完成，这里只负责把 403/404 翻译成工具错误码。
"""

from app.mcp import deps
from app.mcp.format import index_meta, meta, shape_flow, tool_error
from app.mcp.principal import McpAuthError, ProjectContext, current_principal
from app.models.base import CamelModel
from app.models.tool import ChainDirection

#: 检索结果为空时的提示。要指向「去 ArchMind 建索引」这个可执行动作。
_NOT_INDEXED_HINT = "该项目尚未建立索引或索引不可用，请先在 ArchMind 完成分析并建索引"

#: 检索失败时的通用提示。基础设施抖动，重试即可，不要误导用户去重建索引。
_RETRY_HINT = "检索服务暂不可用，请稍后重试"

#: 索引本身缺失/未就绪 —— 用户可执行的动作是去 ArchMind 建索引。
_INDEX_NOT_READY_REASONS = frozenset({"absent", "not_ready", "failed"})

#: 基础设施故障（Redis / embed 服务 / Qdrant）—— 用户能做的只有稍后重试。
_SERVICE_FAILURE_REASONS = frozenset(
    {"state_unavailable", "embed_failed", "qdrant_failed"}
)


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


def _empty_retrieval_error(retrieval) -> dict | None:
    """把「空候选」翻译成对外错误；真正只是没命中时返回 None。

    `reason` 必须区分两类：索引没建好（用户该去建索引）和基础设施挂了
    （Redis / embed / Qdrant，用户只能重试）。混成一个码会把后者误导成
    「你的项目没索引」。
    """
    if retrieval.candidates:
        return None
    reason = retrieval.reason
    if not reason:
        # 真发过查询、也真没命中 —— 不是错误。
        if retrieval.attempted:
            return None
        reason = "index_unavailable"

    if reason in _INDEX_NOT_READY_REASONS:
        return tool_error("not_indexed", reason, _NOT_INDEXED_HINT)
    if reason in _SERVICE_FAILURE_REASONS:
        return tool_error("tool_failed", reason, _RETRY_HINT)
    # 未知 reason：同样当服务问题，别臆断成索引缺失。
    return tool_error("tool_failed", reason, _RETRY_HINT)


async def archmind_search_entities(
    projectId: int, query: str, k: int = 10
) -> dict:
    """语义检索项目内的类，返回按相关度排序的候选。

    **空结果不是错误**：项目有索引、查询成功、只是没命中，返回 `ok=true, data=[]`。
    真正该报错的是「检索没跑成」——那种情况下空列表会被误读成「项目里没有」。
    错误码按 `retrieval.reason` 分流：索引缺失/未就绪 → `not_indexed`，
    Redis / embed / Qdrant 故障 → `tool_failed`。
    """
    try:
        if not query or not query.strip():
            return tool_error("invalid_argument", "query 不能为空")
        if not isinstance(k, int) or k < 1 or k > 50:
            return tool_error("invalid_argument", "k 必须在 1..50")

        ctx = await ensure_project(projectId)
        svc = deps.services()
        retrieval = await svc.retriever.search(projectId, query, k)

        failure = _empty_retrieval_error(retrieval)
        if failure is not None:
            return failure

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


async def archmind_trace_flow(
    projectId: int, uid: str, maxDepth: int = 3, maxNodes: int = 60
) -> dict:
    """从 `uid` 出发向下追踪调用链，按深度分层返回。

    层标签是**按包名/类名推断**的（见 `format.infer_layer`），不是图上的事实；
    每个节点都标 `inferred: true` / `layerBasis: "package-name"`。
    `breaks` 把 gap 翻译成带中文提示的断点 —— 跨服务断点正是这套工具的重点。

    参数校验先于归属校验，非法输入不该打 Java。
    """
    try:
        if not isinstance(uid, str) or not uid.strip():
            return tool_error(
                "invalid_argument",
                "uid 不能为空",
                "先用 archmind_search_entities 找到类，再传入 uid",
            )
        # bool 是 int 的子类：True/False 会混过 isinstance 与范围检查，
        # 必须先显式拒绝，否则 `maxDepth=True` 会被当成 1。
        if (
            isinstance(maxDepth, bool)
            or not isinstance(maxDepth, int)
            or maxDepth < 1
            or maxDepth > 5
        ):
            return tool_error("invalid_argument", "maxDepth 必须在 1..5")
        if (
            isinstance(maxNodes, bool)
            or not isinstance(maxNodes, int)
            or maxNodes < 1
            or maxNodes > 200
        ):
            return tool_error("invalid_argument", "maxNodes 必须在 1..200")

        ctx = await ensure_project(projectId)
        env = await deps.services().java.call_chain(
            projectId,
            uid,
            direction=ChainDirection.DOWN,
            method_uid=None,
            max_depth=maxDepth,
            max_nodes=maxNodes,
        )
        if not env.ok or env.data is None:
            return tool_error(
                "tool_failed",
                env.note or "调用链暂不可用",
                "请确认 uid 是该项目的类，或先用 archmind_search_entities 查找",
            )
        return {
            "ok": True,
            **shape_flow(env.data),
            "evidence": [e.model_dump(by_alias=True) for e in env.evidence],
            "meta": meta(ctx, truncated=env.truncated),
        }
    except McpToolError as e:
        return tool_error(e.code, e.message, e.hint)
