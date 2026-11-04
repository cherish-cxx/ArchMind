"""LLM 层（R6 第二步）：请求体组装与响应解析。

这两个纯函数决定「模型到底会不会调工具」—— 把 `tools` 写错、把 `tool_calls`
解析漏掉，接口都不会报错，只是模型永远只说话。所以它们必须脱离网络被测死。

**不测 HTTP**：连不上/超时/非 200 那几条是 `httpx` 的行为了，测它等于测别人的库。
"""

import pytest

from app.core.llm import (
    LLMResponseError,
    LLMResult,
    completion_payload,
    parse_completion,
)

MESSAGES = [{"role": "user", "content": "订单流程是什么"}]
TOOLS = [{
    "type": "function",
    "function": {
        "name": "overview",
        "description": "看项目概况",
        "parameters": {"type": "object", "properties": {}},
    },
}]


def envelope(message: dict) -> dict:
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }


def tool_call(call_id: str = "c1", name: str = "overview", arguments: str = "") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


# ==================== 请求体 ====================


def test_payload_has_no_tools_by_default():
    """老的两次调用（intent / explain）不能凭空多一个字段。"""
    payload = completion_payload(MESSAGES)
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_payload_includes_tools_and_defaults_choice_to_auto():
    payload = completion_payload(MESSAGES, tools=TOOLS)
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"


def test_payload_keeps_explicit_tool_choice():
    forced = {"type": "function", "function": {"name": "answer"}}
    assert completion_payload([], tools=TOOLS, tool_choice=forced)["tool_choice"] == forced
    assert completion_payload([], tools=TOOLS, tool_choice="required")["tool_choice"] == "required"


def test_tool_choice_without_tools_is_rejected():
    """没有工具还指定怎么选 —— 这是调用方的 bug，不该等到一次 400。"""
    with pytest.raises(ValueError):
        completion_payload(MESSAGES, tool_choice="auto")


def test_json_mode_adds_response_format():
    assert completion_payload([], json_mode=True)["response_format"] == {"type": "json_object"}


def test_payload_carries_messages_and_temperature():
    payload = completion_payload(MESSAGES, temperature=0.3)
    assert payload["messages"] is MESSAGES
    assert payload["temperature"] == 0.3


# ==================== 响应解析 ====================


def test_plain_text_response():
    result = parse_completion(envelope({"role": "assistant", "content": "你好"}))
    assert result.text == "你好"
    assert result.tool_calls == ()
    assert (result.tokens_in, result.tokens_out) == (7, 3)


def test_tool_calls_are_parsed_and_text_is_empty():
    result = parse_completion(envelope({
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call("c9", "call-chain", '{"uid":"a.B"}')],
    }))
    # 调工具时 content 是 null —— `text` 必须是空串，不能是 "None"
    assert result.text == ""
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("c9", "call-chain", '{"uid":"a.B"}')


def test_empty_arguments_are_allowed():
    """`overview` 这类无参函数，模型按规矩会传空串。"""
    result = parse_completion(envelope({
        "role": "assistant", "content": "", "tool_calls": [tool_call()],
    }))
    assert result.tool_calls[0].arguments == ""


def test_several_tool_calls_are_kept_in_order():
    """一次返回多个调用时顺序不能乱 —— 循环按顺序执行并按顺序回填。"""
    result = parse_completion(envelope({
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call("c1", "classes"), tool_call("c2", "overview")],
    }))
    assert [c.name for c in result.tool_calls] == ["classes", "overview"]


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": None},   # 既没说话，也没调工具
    {"role": "assistant", "content": 42},     # content 不是字符串
])
def test_empty_or_malformed_response_is_rejected(message):
    with pytest.raises(LLMResponseError):
        parse_completion(envelope(message))


@pytest.mark.parametrize("broken", [
    {"type": "function", "function": {"name": "overview"}},                  # 缺 id
    {"id": "c1", "type": "function", "function": {}},                        # 缺 name
    {"id": "c1", "type": "function",
     "function": {"name": "x", "arguments": {"uid": "a.B"}}},                 # arguments 不是字符串
    {"id": "c1", "type": "function"},                                        # 缺 function
])
def test_malformed_tool_call_is_rejected(broken):
    with pytest.raises(LLMResponseError):
        parse_completion(envelope({
            "role": "assistant", "content": None, "tool_calls": [broken],
        }))


def test_missing_choices_is_rejected():
    with pytest.raises(LLMResponseError):
        parse_completion({"choices": []})


# ==================== 回填 ====================


def test_to_message_round_trips_tool_calls():
    """解析 → 还原必须逐字段一致，否则循环下一轮就是 400。"""
    original = {
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call("c1", "call-chain", '{"uid":"a.B"}')],
    }
    assert parse_completion(envelope(original)).to_message() == original


def test_to_message_omits_tool_calls_for_plain_text():
    assert LLMResult(content="你好").to_message() == {
        "role": "assistant", "content": "你好",
    }
