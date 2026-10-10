"""Java → Python 的请求契约。

Python 侧对外暴露的接口（全在 `/internal/**`，只认共享密钥）：

    POST /internal/agent/ask            执行一轮问答
    POST /internal/agent/viewport       前端上报屏幕状态（P4.2）
    POST /internal/agent/admin/evict    项目删除时清 Context

`projectId` 一律由 Java 注入，**Python 不产生、LLM 不可见** —— 租户边界靠这个字段守。
"""

from app.models.base import CamelModel
from app.models.context import RelationRef
from app.models.response import NodeRef


class AskRequest(CamelModel):
    """一轮问答的请求。

    `focus` 是用户当前在图上的锚点。有它，代词「它」才能消解（9月20 §问答边界：
    给了锚点是 V1 主战场，5 个工具全用得上）。
    """

    project_id: int
    message: str
    conversation_id: str | None = None   # P4 才有；P2 可不传
    focus: NodeRef | None = None         # 可为 null：用户直接问「这个项目是干嘛的」


class ViewportReport(CamelModel):
    """前端上报屏幕状态（P4.2）。**真相在前端，后端只存不推断。**

    能走到这条路由，说明前端**手里已经有一个 conversationId 了** ——
    它只可能来自之前某轮 `/ask` 的响应（前端自己不产生 cid）。
    所以后端**收到 cid 却在 Redis 里找不到会话**时，那不是「新会话」而是「**会话已过期**」。

    ⚠️ 这里**没有 `truncated`** —— 那是后端截断后自己置的。
    前端说「我没截断」不可信，不能让它声明。

    ⚠️ **这是「整屏快照」，不是「增量」** —— 后端拿到就**整体覆盖**，
    不做字段合并。所以前端每次都必须报**屏幕上当前的全部**：
    少报一个字段，那个字段就被当成「没有」。
    （例：第二次上报忘了带 `focusedNode`，存下来的焦点就变成 `None` —— 不是 bug，是契约。）
    """

    project_id: int
    conversation_id: str
    rev: int                                  # 单调递增；后端只接受比当前更大的
    visible_nodes: list[str] = []
    visible_relations: list[RelationRef] = []
    focused_node: NodeRef | None = None


class EvictRequest(CamelModel):
    """项目删除时清掉它的 Context。P2 只留形状。"""

    project_id: int
