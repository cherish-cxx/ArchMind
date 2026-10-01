"""一轮对话的上下文 —— `AgentContext` 及其零件。

**第一原则**（设计 §7）：**Context 存状态与引用，不存知识。**
所有实体引用一律用图的 uid 字符串（Class = 全限定名 / Method = `owner#signature`），
不存类对象、不存代码片段、不存整个 Java 响应。

⚠️ **P3 的边界**：这里只定义形状，且**只活在内存里**（`api/agent.py` 里一个进程级 dict）。
   Redis 持久化（db1，TTL 2h）是 P4 的事 —— 届时只加 `store/context_store.py`，**模型不动**。

⚠️ **相对设计 §7 的三处收敛**（都是有意的，不是遗漏）：
  1. `anchor` 拆掉了：设计里 `anchor.target`（用户最初点的类）与 `anchor.focus`（当前焦点）
     是两个字段，但 P3 里 focus 只由 Java 从请求注入、且没有「回到最初那个类」的需求，
     多存一个 target 就是死字段。等真需要了再加回。
  2. `focusStack` 从 `[{uid, at}]` 收敛成 `[uid]`：栈上唯一被用到的操作是 `pop()`，
     `at` 没有任何读取方。审计时间戳在 `history` 里已经有了。
  3. `viewport` 整个不在这里 —— 它属于 P4（前端上报 + `rev` 乐观锁），
     P3 没有任何读写方。提前定义一堆没人碰的字段，只会让 P4 误以为「已经接好了」。
"""

from app.models.base import CamelModel
from app.models.intent import Intent
from app.models.response import NodeRef


class Budget(CamelModel):
    """本轮的预算。循环靠它刹车（设计 §8-C3）。

    `steps` 与 `toolCalls` 是**两层**刹车，不是重复：一次工具调用算一个 step，
    但 LLM 可能在两次调用之间插一步「我信息够了」的判断。`stepsLimit=4` 限的是整轮步数，
    `toolCallsLimit=6` 限的是真正打 Java 的次数。
    """

    turns: int = 0                    # 这个会话总共聊了几轮（跨轮累计）
    steps_used: int = 0               # **本轮**已用步数 —— 每轮开头清零
    tool_calls_this_turn: int = 0     # **本轮**已打 Java 次数 —— 每轮开头清零
    tool_calls_limit: int = 6
    steps_limit: int = 4


class HistoryItem(CamelModel):
    """一轮的历史。**存摘要不存原文**（设计 §7 决定⑤）。

    十轮之后 Context 就爆了 —— 把每轮回答压成一句，是 Context 硬上限的解法。
    """

    turn: int
    intent: Intent
    q: str                            # 用户原话（短，照存）
    a: str                            # 助手回答**摘要**（长，压成一句）
    focus_after: str | None = None    # 这轮结束后焦点落在哪个 uid 上


class AgentContext(CamelModel):
    """一个会话的全部状态。

    `focus` 有值是 V1 的主战场：有锚点，代词「它」才能消解，5 个工具才全用得上；
    没有锚点就只能猜类名（9月20 §问答边界）。
    """

    version: int = 1
    conversation_id: str
    project_id: int

    focus: NodeRef | None = None
    focus_stack: list[str] = []       # 「退回去」= pop()，纯 NAVIGATE，不花 LLM

    history: list[HistoryItem] = []
    budget: Budget = Budget()

    def reset_turn_budget(self) -> None:
        """每轮开头调一次。`turns` 和限额不重置 —— 只有本轮计数归零。"""
        self.budget.steps_used = 0
        self.budget.tool_calls_this_turn = 0
