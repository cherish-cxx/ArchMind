"""一轮对话的上下文 —— `AgentContext` 及其零件。

**第一原则**（设计 §7）：**Context 存状态与引用，不存知识。**
所有实体引用一律用图的 uid 字符串（Class = 全限定名 / Method = `owner#signature`），
不存类对象、不存代码片段、不存整个 Java 响应。

✅ **P4.1 已搬进 Redis db1**（TTL 2h）—— 存取全在 `store/context_store.py`，本文件仍只定义形状。

⚠️ **相对设计 §7 的三处收敛**（都是有意的，不是遗漏）：
  1. `anchor` 拆掉了：设计里 `anchor.target`（用户最初点的类）与 `anchor.focus`（当前焦点）
     是两个字段，但 P3 里 focus 只由 Java 从请求注入、且没有「回到最初那个类」的需求，
     多存一个 target 就是死字段。等真需要了再加回。
  2. `focusStack` 从 `[{uid, at}]` 收敛成 `[uid]`：栈上唯一被用到的操作是 `pop()`，
     `at` 没有任何读取方。审计时间戳在 `history` 里已经有了。
  3. `viewport` P3 时整个不在（没有读写方，提前定义只会让 P4 误以为「已经接好了」）。
     **P4.2 补上了**，但形状与原设计不同：**删掉 `lastUiCommands`、加 `focusedNode`** ——
     刷新恢复改走**快照驱动**而非命令重放（理由见 P4 设计 §3.5）。
"""

from pydantic import Field

from app.models.base import CamelModel
from app.models.intent import Intent
from app.models.response import NodeRef
from app.models.tool import RelationType


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


#: `viewport.visibleNodes` / `visibleRelations` 的硬上限。
#:
#: 为什么要有：Context 每轮要序列化进 Redis，前端报一个几万节点的列表会把它胀爆。
#: 截断可以接受，但**必须诚实标记**（置 `truncated`），否则后端会误以为屏幕上就这些。
VIEWPORT_MAX_ITEMS = 60


class RelationRef(CamelModel):
    """屏幕上一条可见的关系。三要素：从谁、到谁、什么关系。

    和 `tool.Evidence` 的区别：Evidence 是**工具从图里捞出来的凭据**（带 count、承担
    grounding 责任），这个只是「前端报上来的我画了这条边」，够画出箭头就行。
    """

    from_: str = Field(alias="from")   # ← 显式定别名：`to_camel("from_")` 去不掉下划线，会发成 "from_"
    to: str             # uid
    rel: RelationType


class Viewport(CamelModel):
    """前端屏幕状态的**快照**。真相在前端，这里只是它的镜像（P4 设计 §3.5）。

    **为什么是快照而不是命令重放**：重放是「重新走一遍路」——
    图数据一变（项目可重新分析）或只存最后一批命令（多轮累积会丢），就到不了原来的终点。
    快照是「直接站在终点」，免疫这两件事。代价是**语义必须自己补全**，
    所以有 `focused_node` 这个字段 —— `visible_nodes` 是个集合，表达不了「谁在前台」。
    """

    rev: int = 0                              # 单调递增；0 = 从未上报过
    visible_nodes: list[str] = []             # uid
    visible_relations: list[RelationRef] = []
    focused_node: NodeRef | None = None       # FOCUS_NODE 的「选中」语义
    truncated: bool = False                   # 超上限被截断时置 true
    mode: str = "cumulative"                  # cumulative | focus
    center_uid: str | None = None             # 焦点视图的中心
    hops: int = 0                             # 焦点视图固定 1；累积视图 0
    folded_count: int = 0                     # 被折进框、不在 visible_nodes 里的类数

    @classmethod
    def from_report(
        cls,
        *,
        rev: int,
        visible_nodes: list[str],
        visible_relations: list[RelationRef],
        focused_node: NodeRef | None,
        mode: str = "cumulative",
        center_uid: str | None = None,
        hops: int = 0,
        folded_count: int = 0,
    ) -> "Viewport":
        """把前端上报的原始列表收进快照，**超上限就截断并置 `truncated`**。

        截断必须在后端做 —— 前端的报告是**不可信输入**，它说「我没截断」不算数。
        放在模型里而不是路由里，是为了能单独测。
        """
        nodes = visible_nodes[:VIEWPORT_MAX_ITEMS]
        rels = visible_relations[:VIEWPORT_MAX_ITEMS]
        return cls(
            rev=rev,
            visible_nodes=nodes,
            visible_relations=rels,
            focused_node=focused_node,
            mode=mode,
            center_uid=center_uid,
            hops=hops,
            folded_count=folded_count,
            truncated=(
                len(visible_nodes) > VIEWPORT_MAX_ITEMS
                or len(visible_relations) > VIEWPORT_MAX_ITEMS
            ),
        )


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

    #: 源码大纲里锁定的方法（`owner#signature`）。**不是 focus**：
    #: focus 指向画布上的类，这个只回答「用户正在看哪个方法」。
    #: 只被 EXPLORE_CODE 的 method-body 消费；其余工具一律按类走。
    method_uid: str | None = None

    history: list[HistoryItem] = []
    budget: Budget = Budget()
    viewport: Viewport = Viewport()   # 前端屏幕状态的镜像（P4.2）

    def reset_turn_budget(self) -> None:
        """每轮开头调一次。`turns` 和限额不重置 —— 只有本轮计数归零。"""
        self.budget.steps_used = 0
        self.budget.tool_calls_this_turn = 0
