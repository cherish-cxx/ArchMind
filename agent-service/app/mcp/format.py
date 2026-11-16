"""MCP 工具的响应格式。

所有工具共用的三块：项目级 meta、索引状态摘要、失败信封。
放在这里是为了让每个工具返回的 `meta` 形状只有一个来源 ——
外部 agent 靠 `graphRev` 判断快照新旧，字段飘了就失去意义。
"""

from app.mcp.principal import ProjectContext


def index_meta(state: dict | None) -> dict:
    """把 Redis 里的索引状态压成对外摘要。

    没有状态（从未索引）也返回合法结构：`status="absent"`、`rev=0`，
    让调用方不必区分「没有键」和「状态为 null」。
    `coverage` 只在 actual/expected 都存在时给出 —— 缺失时省略，
    比编一个 `0/0` 更诚实。
    """
    if not state:
        return {"status": "absent", "rev": 0}
    out: dict = {
        "status": state.get("status") or "absent",
        "rev": state.get("activeRev") or 0,
    }
    actual = state.get("actual")
    expected = state.get("expected")
    if actual is not None and expected is not None:
        out["coverage"] = f"{actual}/{expected}"
    return out


def meta(
    ctx: ProjectContext, index: dict | None = None, truncated: bool = False
) -> dict:
    """项目级元信息；数据工具必带 `graphRev`。"""
    out: dict = {"projectId": ctx.project_id, "graphRev": ctx.graph_rev}
    if index is not None:
        out["index"] = index
    out["truncated"] = truncated
    return out


def tool_error(code: str, message: str, hint: str = "") -> dict:
    """预期失败的信封。**不抛协议错误** —— 让 agent 读到原因并自行纠偏。"""
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}
