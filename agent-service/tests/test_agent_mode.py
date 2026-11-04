"""env 级开关（R6）：同一个 `/ask` 入口，按 `settings.agent_mode` 走两条链路。

只钉「分派」这一次判断 —— 两条链路各自的行为已经由 `test_loop.py` 与
`test_agent_loop.py` 覆盖，这里再验一遍只会让失败定位变慢。
"""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.agent import (
    get_context_store,
    get_java_client,
    get_llm_client,
    get_memory_service,
    get_retriever,
)
from app.config import INTERNAL_TOKEN_HEADER, Settings, settings
from app.core.llm import LLMResult
from app.main import app
from app.models.context import ToolCall
from fakes import (
    FakeContextStore,
    FakeJava,
    FakeLLM,
    FakeMemory,
    FakeRetriever,
    ok_overview,
)

_H = {INTERNAL_TOKEN_HEADER: settings.internal_token}
_BODY = {"projectId": 1, "userId": 2, "message": "这个项目是干嘛的"}


@pytest.fixture
def wire():
    """把 `/ask` 的五个依赖全换成替身。真实 lifespan 照跑（它不联网）。"""
    def install(llm: FakeLLM) -> FakeContextStore:
        store = FakeContextStore()
        app.dependency_overrides[get_context_store] = lambda: store
        app.dependency_overrides[get_java_client] = lambda: FakeJava(ok_overview())
        app.dependency_overrides[get_llm_client] = lambda: llm
        app.dependency_overrides[get_retriever] = lambda: FakeRetriever()
        app.dependency_overrides[get_memory_service] = lambda: FakeMemory()
        return store

    try:
        yield install
    finally:
        app.dependency_overrides.clear()


def _answer() -> LLMResult:
    return LLMResult(content=None, tool_calls=(
        ToolCall(id="a1", name="answer", arguments=json.dumps({
            "headline": "电商系统",
            "sections": [{"title": "结论", "body": "这是一个电商项目。"}],
        }, ensure_ascii=False)),
    ))


def test_tools_mode_uses_agent_loop(wire, monkeypatch):
    monkeypatch.setattr(settings, "agent_mode", "tools")
    llm = FakeLLM([_answer()])
    wire(llm)

    with TestClient(app) as client:
        body = client.post("/internal/agent/ask", headers=_H, json=_BODY).json()

    assert body["meta"]["intent"] == "AGENT"
    assert body["headline"] == "电商系统"
    # 循环才会带工具清单；legacy 两次调用都不带
    assert llm.calls[0]["tools"] is not None
    assert llm.calls[0]["json_mode"] is False


def test_legacy_mode_is_the_default(wire, monkeypatch):
    monkeypatch.setattr(settings, "agent_mode", "legacy")
    llm = FakeLLM([
        '{"intent": "EXPLORE_OVERVIEW", "confidence": 0.9}',
        '{"headline": "电商系统", "sections": [{"title": "结论", "body": "这是一个电商项目。"}]}',
    ])
    wire(llm)

    with TestClient(app) as client:
        body = client.post("/internal/agent/ask", headers=_H, json=_BODY).json()

    assert body["meta"]["intent"] == "EXPLORE_OVERVIEW"
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["json_mode"] is True


def test_invalid_mode_fails_at_startup():
    """拼错取值必须炸在启动时 —— 静默落回 legacy 会让 A/B 的两份数据是同一条链路。"""
    with pytest.raises(ValidationError):
        Settings(agent_mode="tool")
