"""`/ask` 只有多步循环一条链路（R6 第三步）。

只钉路由层的分派结果：`settings.agent_mode` 已删除，
a) 模型拿到工具清单（function calling，不是 json_mode）；
b) 响应里的 `meta.intent` 为 null —— 分发归模型后不再有意图标签。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.agent import (
    get_context_store,
    get_java_client,
    get_llm_client,
    get_memory_service,
    get_retriever,
)
from app.config import INTERNAL_TOKEN_HEADER, settings
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


def test_ask_always_uses_agent_loop(wire):
    llm = FakeLLM([_answer()])
    wire(llm)

    with TestClient(app) as client:
        body = client.post("/internal/agent/ask", headers=_H, json=_BODY).json()

    assert body["meta"]["intent"] is None
    assert body["headline"] == "电商系统"
    # 循环才会带工具清单，并且不走 json_mode
    assert llm.calls[0]["tools"] is not None
    assert llm.calls[0]["json_mode"] is False
