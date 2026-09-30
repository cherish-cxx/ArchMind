"""Java → Python 的请求契约。

对应设计 §3.5 里 Python 侧对外的两个接口：

    POST /internal/agent/ask            执行一轮问答
    POST /internal/agent/admin/evict    项目删除时清 Context（P4 才有实现）

`projectId` 一律由 Java 注入，**Python 不产生、LLM 不可见** —— 租户边界靠这个字段守。
"""

from app.models.base import CamelModel
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


class EvictRequest(CamelModel):
    """项目删除时清掉它的 Context。P2 只留形状。"""

    project_id: int
