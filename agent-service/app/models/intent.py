"""Intent 判定的输出契约 —— LLM #1 只能吐出这个形状。

设计来源：[9月19 Agent v1设计] §6「Intent 分类（V1 六个，封闭枚举）」，形状取自该节示例：

    { "intent": "EXPLORE_FLOW", "confidence": 0.86,
      "slots": { "entityRef": "IT", "direction": "DOWN", "depth": 2 },
      "rewrittenQuery": "OrderService 的下游调用链" }

**为什么单独成文件，而不是塞进 `response.py`**：`response.py` 要 import 这里的 `Intent`
（`Suggestion.intent` 的类型），反过来就成了循环 import。契约文件之间只允许单向依赖 ——
依赖方向是 `base → tool → intent → response → context`。
"""

from enum import StrEnum

from app.models.base import CamelModel
from app.models.tool import ChainDirection

#: 低于这个置信度就不执行工具，回一句澄清（设计 §6「低置信度处置」）。
#:
#: 写成模块常量而不是模型字段：它是**策略**不是**数据**，改它应该是一个显式的代码改动，
#: 而不是某处传参顺手带进来的一个数字。
CONFIDENCE_THRESHOLD = 0.6


class Intent(StrEnum):
    """V1 六个，**封闭枚举** —— LLM 只能在这六个里选。

    「封闭」是刻意的：开放枚举等于把「能答什么」的决定权交给 LLM，
    遇到「帮我写个登录接口」它就能现场发明一个 `EXPLORE_WRITE_CODE` 出来。
    六个不够用是**设计缺陷**，应该改代码加一个，而不是让 LLM 兜底。
    """

    EXPLORE_OVERVIEW = "EXPLORE_OVERVIEW"    # 这个项目/模块干嘛     → overview
    EXPLORE_FLOW = "EXPLORE_FLOW"            # 业务流程 / 调用链      → call-chain（核心场景）
    EXPLORE_RELATION = "EXPLORE_RELATION"    # 这个类干嘛 / 谁调它    → class-relations
    EXPLORE_CODE = "EXPLORE_CODE"            # 具体怎么实现的         → method-body
    NAVIGATE = "NAVIGATE"                    # 跳过去/展开/退回去     → 只出 UICommand
    OUT_OF_SCOPE = "OUT_OF_SCOPE"            # 写代码 / 评价代码质量   → 拒绝模板


#: 不需要 LLM #2 讲解的 intent。
#:
#: `NAVIGATE` 只产 UICommand、`OUT_OF_SCOPE` 走固定拒绝模板 —— 两者都不需要再调一次 LLM。
#: 单独拎出来而不是散在 if 里，是因为这条短路直接对应 **E2「用户点一下展开就烧一次 token」**，
#: 是一个成本决策，不该被埋进流程代码里看不见。
NO_EXPLAIN_INTENTS = frozenset({Intent.NAVIGATE, Intent.OUT_OF_SCOPE})


class Slots(CamelModel):
    """Intent 的槽位。

    **全部可空**，且刻意不设 `extra="forbid"`：LLM 多吐一个 key 就整轮失败，
    代价远大于收益。未知 key 由 `core/intent.py` 记一条 warn —— 让它可见，但不致命。
    """

    entity_ref: str | None = None    # 用户指代的那个实体（类名或全限定名）
    direction: ChainDirection | None = None   # 上游 / 下游。复用 call-chain 的枚举，不另起一个
    depth: int | None = None         # 想看几层


class IntentResult(CamelModel):
    """LLM #1 的结构化输出。"""

    intent: Intent
    confidence: float
    slots: Slots = Slots()
    rewritten_query: str | None = None   # 消解代词后的完整问句，喂给后面的工具选择

    @property
    def needs_clarification(self) -> bool:
        """置信度不够就别猜 —— 「宁可多问一句，也不要基于猜错的 Intent 拉出一张错的图」。

        错的图比不回答伤害大得多：用户看到一张看起来对、实际无关的图，
        既不知道自己错了，也没法纠正。
        """
        return self.confidence < CONFIDENCE_THRESHOLD
