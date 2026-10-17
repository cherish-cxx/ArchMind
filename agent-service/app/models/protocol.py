"""Java → Python 的请求契约。

Python 侧对外暴露的接口（全在 `/internal/**`，只认共享密钥）：

    POST   /internal/agent/ask            执行一轮问答
    POST   /internal/agent/navigate       前端点按钮导航（P4.3，零 LLM）
    POST   /internal/agent/viewport       前端上报屏幕状态（P4.2）
    GET    /internal/agent/conversations/{id}   读回会话（P4.2）
    POST   /internal/agent/admin/evict    项目删除时清 Context

`projectId` 一律由 Java 注入，**Python 不产生、LLM 不可见** —— 租户边界靠这个字段守。
"""

from enum import StrEnum

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


class NavigateOp(StrEnum):
    """导航操作（设计 §3.3）。

    ⚠️ **枚举里有两个，路由目前只支持 `BACK`** —— `EXPAND` 需要先定义「展开什么、
    展开几层、展开后焦点落哪」，那个语义在 loop 里**从来不存在**（`navigate()` 只做
    出栈与清空）。让它留在枚举里而不是直接删掉，是为了让**契约完整**：
    前端传 EXPAND 拿到的是明确的「暂不支持」业务答复，而不是一个 422 校验错误。
    """

    BACK = "BACK"       # 退回到上一个焦点（焦点栈出栈）
    EXPAND = "EXPAND"   # 展开邻居 —— P5 再做


class NavigateRequest(CamelModel):
    """前端**点按钮**时的导航请求（P4.3）。

    和 `AskRequest` 的区别是**谁判的意图**：`/ask` 靠 LLM #1 从自然语言里判出 NAVIGATE，
    这里前端**直接声明**，因此一轮下来**零 token**（设计 §3.3 / E2）。

    `conversation_id` 是**必填**（与 `AskRequest` 不同）—— 导航作用于一个**已经存在**的
    会话：栈在它的 Context 里。刚打开页面还没有会话时，前端本来也没有「退回去」可按。
    """

    project_id: int
    conversation_id: str
    op: NavigateOp
    target: NodeRef | None = None   # 目前 BACK 用不到；留给 EXPAND


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
