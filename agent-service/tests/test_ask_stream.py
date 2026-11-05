"""`/internal/agent/ask/stream` 的三条出口 + 事件顺序。

不碰真 Redis / 真 Java：依赖全部覆盖，红只代表流协议漂了。
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

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
from app.main import app
from app.tools.java_client import InternalAuthError
from fakes import FakeContextStore, FakeJava, FakeLLM
from test_agent_loop import ANSWER_ARGS, reply, tool_call

HEADERS = {INTERNAL_TOKEN_HEADER: settings.internal_token}


def parse_sse(lines):
    """把 `iter_lines()` 拼回 `(事件名, data)`。"""
    name, data = None, []
    for line in lines:
        if line == "":
            if name is not None or data:
                yield name or "message", json.loads("\n".join(data) or "{}")
            name, data = None, []
        elif line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].lstrip())


@contextmanager
def stream_client(llm, java=None, store=None):
    java = java or FakeJava()
    store = store or FakeContextStore()
    app.dependency_overrides[get_java_client] = lambda: java
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_context_store] = lambda: store
    app.dependency_overrides[get_retriever] = lambda: None
    app.dependency_overrides[get_memory_service] = lambda: SimpleNamespace(enabled=False)
    try:
        with TestClient(app) as c:
            yield c, store
    finally:
        app.dependency_overrides.clear()


def _payload(**kw):
    return {"projectId": 7, "userId": 1, "message": "订单流程", **kw}


def test_stream_emits_start_steps_then_answer():
    llm = FakeLLM([
        reply(tool_call("t1", "overview")),
        reply(tool_call("a1", "answer", ANSWER_ARGS)),
    ])
    with stream_client(llm) as (client, _):
        with client.stream("POST", "/internal/agent/ask/stream",
                           json=_payload(), headers=HEADERS) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            events = list(parse_sse(r.iter_lines()))

    names = [n for n, _ in events]
    assert names[0] == "start"
    assert names[-1] == "answer"
    assert "step" in names

    start = events[0][1]
    assert start["conversationId"].startswith("c_")

    step = next(d for n, d in events if n == "step")
    assert {"n", "type", "detail", "ms"} <= set(step)

    answer = events[-1][1]
    assert answer["conversationId"] == start["conversationId"]
    assert answer["sections"]


def test_stream_session_expired_is_an_answer_event():
    store = FakeContextStore()          # 空：带 cid 必定过期
    llm = FakeLLM([])
    with stream_client(llm, store=store) as (client, _):
        with client.stream(
            "POST", "/internal/agent/ask/stream",
            json=_payload(conversationId="c_gone"), headers=HEADERS,
        ) as r:
            events = list(parse_sse(r.iter_lines()))

    assert [n for n, _ in events] == ["start", "answer"]
    assert events[-1][1]["sessionExpired"] is True
    assert llm.call_count == 0, "过期分支一次模型都不该调"


def test_stream_auth_error_emits_error_event():
    llm = FakeLLM([reply(tool_call("t1", "overview"))])
    java = FakeJava(raises=InternalAuthError("token mismatch"))
    with stream_client(llm, java=java) as (client, _):
        with client.stream("POST", "/internal/agent/ask/stream",
                           json=_payload(), headers=HEADERS) as r:
            events = list(parse_sse(r.iter_lines()))

    assert events[-1][0] == "error"
    assert events[-1][1]["message"]


@pytest.mark.parametrize("bad", [{}, {"projectId": 7, "userId": 1}])
def test_stream_rejects_bad_payload(bad):
    with stream_client(FakeLLM([])) as (client, _):
        r = client.post("/internal/agent/ask/stream", json=bad, headers=HEADERS)
    assert r.status_code == 422
