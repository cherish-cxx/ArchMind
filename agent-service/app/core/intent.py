"""Intent 判定 —— LLM #1（设计 §6）。

**为什么判定用 LLM 而不是关键词匹配**：中文口语里「它呢？」「再往上看看」这类表达，
关键词表必然失效 —— 要么写不全，要么一写就误伤。这是设计里明确选的路线。

本模块做三件事，且只做这三件：
  1. 组装 prompt（委托给 `core/prompt.py`）
  2. 调 LLM 拿结构化输出
  3. 把文本变成 `IntentResult`

**不在这里决定「要不要执行工具」** —— `confidence < 0.6` 只是打上标记，
真正的分支在 `core/loop.py`。分开是为了能分别测：这里测「判得对不对」，
那里测「判不准时的处置对不对」。
"""

import json
import logging

from pydantic import ValidationError

from app.core.llm import Completer
from app.core.prompt import intent_messages
from app.models.context import AgentContext
from app.models.intent import Intent, IntentResult

logger = logging.getLogger(__name__)

#: LLM 输出里我们认识的 key。多出来的记 warn —— 方便发现「prompt 改了但模型还在按老格式答」。
_KNOWN_KEYS = {"intent", "confidence", "slots", "rewrittenQuery", "rewritten_query"}


class IntentParseError(Exception):
    """LLM 返回的文本没能变成一个合法的 `IntentResult`。"""


def _fallback() -> IntentResult:
    """解析失败时的兜底。

    用 `confidence=0.0` 而不是抛异常：0 必然低于阈值 → 循环会走进「回澄清」那条路，
    用户看到的是「我没太听懂，你是想问…？」，而不是一个 500。
    **兜底走的是和低置信同一条路，不是另开一条** —— 少一条分支就少一处会烂的地方。

    `intent` 填 `OUT_OF_SCOPE` 只是为了让字段有个合法值（此刻它不会被用到，
    因为 `needs_clarification` 为真会先拦截）；log 里那条 error 才是给人看的。
    """
    return IntentResult(intent=Intent.OUT_OF_SCOPE, confidence=0.0)


def parse_intent(text: str) -> IntentResult:
    """纯函数：文本 → `IntentResult`。单独拆出来是为了不调 LLM 也能测解析。"""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise IntentParseError(f"不是合法 JSON：{e}；原文前 200 字：{text[:200]!r}") from e

    if not isinstance(raw, dict):
        raise IntentParseError(f"顶层不是 JSON 对象，而是 {type(raw).__name__}")

    extra = set(raw) - _KNOWN_KEYS
    if extra:
        logger.warning("Intent 输出有未预期的 key %s —— prompt 和模型格式可能对不上了", sorted(extra))

    try:
        return IntentResult.model_validate(raw)
    except ValidationError as e:
        # 最常见的就是 intent 不在六个枚举里（模型现场发明了一个）
        raise IntentParseError(f"字段校验失败：{e}") from e


async def classify_intent(ctx: AgentContext, message: str, llm: Completer) -> IntentResult:
    """判定一轮问答的意图。

    ⚠️ `LLMError` **故意不在这里接住**：`LLMNotConfiguredError`（配置错，该 500）与
    `LLMTransportError`（外部抖动，该降级成模板讲解）的处置方式完全不同，
    而这个区别只有循环那一层知道该怎么处理。在这里吞掉就等于把两者混为一谈 ——
    那正是 P2 在 java_client 里为 401 特意避免的坑。
    """
    raw = await llm.complete(intent_messages(ctx, message), json_mode=True)

    try:
        return parse_intent(raw.text)
    except IntentParseError as e:
        logger.error("Intent 解析失败，回落到澄清：%s", e)
        return _fallback()
