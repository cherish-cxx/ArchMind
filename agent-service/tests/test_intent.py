"""Intent 判定（LLM #1）的单元测试 —— 用 FakeLLM，不打网络。

三层断言：
  1. `parse_intent` 纯解析：合法/非法 JSON、非法枚举、多余 key
  2. `classify_intent` 的兜底：LLM 吐垃圾 → 回落到澄清，而不是抛异常
  3. prompt 的**防漂移**：六个 intent 和 "json" 字样必须在 —— 后者是 DeepSeek 的硬要求
"""

import json

import pytest

from app.core.intent import IntentParseError, classify_intent, parse_intent
from app.core.llm import LLMNotConfiguredError, LLMTransportError
from app.core.prompt import intent_messages
from app.models.context import AgentContext
from app.models.intent import CONFIDENCE_THRESHOLD, Intent
from app.models.response import NodeRef, NodeRefType
from fakes import FakeLLM


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(
        conversation_id="c_test",
        project_id=1,
        focus=NodeRef(type=NodeRefType.CLASS, uid="com.hmall.trade.controller.OrderController"),
    )


def _reply(intent: Intent, confidence: float) -> str:
    return json.dumps({"intent": intent.value, "confidence": confidence})


# ==================== 1. parse_intent：纯解析 ====================


def test_parse_intent_happy_path():
    r = parse_intent('{"intent": "EXPLORE_FLOW", "confidence": 0.86, "slots": {"depth": 2}}')
    assert r.intent is Intent.EXPLORE_FLOW
    assert r.confidence == 0.86
    assert r.slots.depth == 2


def test_parse_intent_rejects_non_json():
    with pytest.raises(IntentParseError, match="不是合法 JSON"):
        parse_intent("好的，我判断这是查询调用链的意图。")


def test_parse_intent_rejects_non_object():
    with pytest.raises(IntentParseError, match="顶层不是 JSON 对象"):
        parse_intent('["EXPLORE_FLOW"]')


def test_parse_intent_rejects_invented_intent():
    """★ LLM 现场发明一个枚举值 —— 必须在模型校验层被拒，而不是流到下游变成未知分支。"""
    with pytest.raises(IntentParseError, match="字段校验失败"):
        parse_intent('{"intent": "EXPLORE_WRITE_CODE", "confidence": 0.9}')


def test_parse_intent_warns_on_unknown_key(caplog):
    """多余 key 不致命，但必须留下痕迹 —— 它是「prompt 与模型格式脱节」的早期信号。"""
    with caplog.at_level("WARNING"):
        parse_intent('{"intent": "NAVIGATE", "confidence": 0.8, "why": "用户点了展开"}')
    assert any("未预期的 key" in r.message for r in caplog.records)


def test_parse_intent_allows_extra_slot_keys():
    """slots 里的多余字段不该让整轮失败（Slots 没开 extra=forbid）。"""
    r = parse_intent('{"intent": "NAVIGATE", "confidence": 0.8, "slots": {"zoom": 1.5}}')
    assert r.intent is Intent.NAVIGATE


# ==================== 2. classify_intent：兜底与传播 ====================


async def test_classify_intent_returns_parsed_result(ctx):
    llm = FakeLLM([_reply(Intent.EXPLORE_FLOW, 0.95)])

    r = await classify_intent(ctx, "订单创建的业务流程是什么", llm)

    assert r.intent is Intent.EXPLORE_FLOW
    assert r.needs_clarification is False
    assert llm.call_count == 1
    assert llm.calls[0]["json_mode"] is True, "Intent 判定必须走 json 模式"


async def test_classify_intent_falls_back_to_clarification_on_garbage(ctx):
    """★ LLM 吐了一段人话而不是 JSON —— 不能 500，要回落到「回澄清」。"""
    llm = FakeLLM(["我觉得用户是想看调用链"])

    r = await classify_intent(ctx, "它呢", llm)

    assert r.needs_clarification is True, "兜底的 confidence 必须低于阈值，否则会拿垃圾去查图"
    assert r.confidence < CONFIDENCE_THRESHOLD


async def test_classify_intent_low_confidence_is_marked(ctx):
    """低置信只是**打标记**，判定层不做处置 —— 处置在 loop 里（分层可测）。"""
    llm = FakeLLM([_reply(Intent.EXPLORE_RELATION, 0.4)])

    r = await classify_intent(ctx, "那个啥", llm)

    assert r.needs_clarification is True


async def test_classify_intent_does_not_swallow_config_error(ctx):
    """⚠️ 没配 key 是**配置错误**，必须原样抛出去变成 500 —— 绝不能被当成「LLM 抖动」降级。"""
    llm = FakeLLM([LLMNotConfiguredError("没配 key")])

    with pytest.raises(LLMNotConfiguredError):
        await classify_intent(ctx, "订单流程", llm)


async def test_classify_intent_does_not_swallow_transport_error(ctx):
    """运输错误也原样抛 —— 判「该降级还是该炸」是 loop 的职责，判定层不替它决定。"""
    llm = FakeLLM([LLMTransportError("超时")])

    with pytest.raises(LLMTransportError):
        await classify_intent(ctx, "订单流程", llm)


# ==================== 3. prompt 防漂移 ====================


def test_intent_prompt_lists_all_six_enums(ctx):
    """prompt 里必须列全六个 intent —— 少一个，LLM 就永远判不出那个意图。"""
    system = intent_messages(ctx, "随便问问")[0]["content"]
    missing = [i.value for i in Intent if i.value not in system]
    assert not missing, f"prompt 漏了这些 intent：{missing}"


def test_intent_prompt_contains_json_keyword(ctx):
    """★ DeepSeek 的 json_object 模式要求 prompt 里出现 "json" 字样，否则 400。

    这条测试存在的唯一理由，就是防止有人「顺手把提示词润色一下」时删掉那个词 ——
    删了之后报的错会指向别处，很难查。
    """
    system = intent_messages(ctx, "随便问问")[0]["content"]
    assert "json" in system.lower()


def test_intent_prompt_includes_focus_uid(ctx):
    """有锚点时，全限定名必须进 prompt —— LLM 猜不出 `.impl` 这种子包（9月22 实测坑）。"""
    messages = intent_messages(ctx, "它的调用方呢")
    assert ctx.focus.uid in messages[1]["content"]


def test_intent_prompt_says_so_when_no_focus():
    """没锚点时要明说，否则 LLM 会硬着头皮编一个类名出来。"""
    bare = AgentContext(conversation_id="c", project_id=1)
    messages = intent_messages(bare, "这个项目干嘛的")
    assert "没有锚点" in messages[1]["content"]
